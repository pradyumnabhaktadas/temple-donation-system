"""Durable, privacy-limited Zoho Forms receipt reconciliation.

Entries are pulled from Zoho, queued briefly, and matched only by the exact
Razorpay ``pay_`` transaction id.  No phone/amount/date inference is ever
used.  Pending payload is erased immediately after a receipt or after the
configured retention period; the small remaining tombstone prevents a later
API replay from making a duplicate receipt.
"""
import datetime
import hashlib
import json

from flask import current_app

from extensions import db
from models import Donation, ZohoEntryQueue, ZohoForm


def _fingerprint(form_key, entry):
    """Stable, non-reversible identity for a Zoho record replay."""
    canonical = json.dumps(entry or {}, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256((form_key + "\0" + canonical).encode("utf-8")).hexdigest()


def _entry_id(entry):
    for key in ("ID", "id", "Record ID", "RecordID", "Entry ID", "entry_id"):
        value = (entry or {}).get(key)
        if value not in (None, ""):
            return str(value)[:150]
    return None


def stage_entries(config, max_pages=2, page_size=100):
    """Fetch a bounded recent page set from every configured active form.

    Each source record is written once.  Network/API errors are reported in
    the summary and do not masquerade as an empty form.
    """
    import zoho_api
    import zoho_sync

    summary = {"forms": 0, "staged": 0, "already_known": 0, "errors": []}
    forms = ZohoForm.query.filter_by(is_active=True).order_by(ZohoForm.id).all()
    for form in forms:
        summary["forms"] += 1
        start = 1
        try:
            for _ in range(max(1, max_pages)):
                records, _ = zoho_api.entries(config, form.api_link_name, start_index=start, limit=page_size)
                if not records:
                    break
                for entry in records:
                    fingerprint = _fingerprint(form.form_key, entry)
                    if ZohoEntryQueue.query.filter_by(entry_fingerprint=fingerprint).first():
                        summary["already_known"] += 1
                        continue
                    payment_id, order_id = zoho_sync.payment_ids(entry)
                    payload = zoho_sync.entry_payload(entry)
                    db.session.add(ZohoEntryQueue(
                        form_key=form.form_key,
                        entry_fingerprint=fingerprint,
                        zoho_entry_id=_entry_id(entry),
                        razorpay_payment_id=payment_id,
                        razorpay_order_id=order_id,
                        full_name=payload.get("full_name"), phone=payload.get("phone"),
                        email=payload.get("email"), pan=payload.get("pan"),
                        amount=payload.get("amount"), received_at=zoho_sync.parse_added_time(entry),
                    ))
                    summary["staged"] += 1
                db.session.commit()
                if len(records) < page_size:
                    break
                start += len(records)
        except Exception as exc:  # Zoho must never be read as silently empty
            db.session.rollback()
            current_app.logger.exception("Zoho queue staging failed for %s", form.form_key)
            summary["errors"].append(f"{form.label}: {exc}")
    return summary


def _existing_donation(payment_id):
    return Donation.query.filter(db.or_(
        Donation.razorpay_payment_id == payment_id,
        Donation.bank_transaction_id == payment_id,
    )).first()


def process_entries(config, limit=None):
    """Process only a bounded number of staged entries, fail-closed.

    In shadow mode all validation occurs but no receipt is issued.  This
    makes live enablement an explicit, reversible configuration decision.
    """
    from public import _create_zoho_donation, _zoho_payment_is_captured

    mode = (config.get("ZOHO_AUTOMATION_MODE") or "off").lower()
    limit = limit or int(config.get("ZOHO_SYNC_MAX_ENTRIES_PER_RUN") or 40)
    summary = {"checked": 0, "created": 0, "already_recorded": 0, "pending": 0,
               "shadow_matches": 0, "skipped": 0, "errors": []}
    if mode == "off":
        return summary

    forms = {f.form_key.lower(): f for f in ZohoForm.query.filter_by(is_active=True).all()}
    # ``validated`` rows are exact captured matches found during shadow
    # mode. Keep them until live mode is explicitly approved; they are not
    # treated as unmatched and therefore are not part of five-day expiry.
    entries = (ZohoEntryQueue.query.filter(ZohoEntryQueue.status.in_(("pending", "validated")))
               .order_by(ZohoEntryQueue.stored_at, ZohoEntryQueue.id).limit(limit).all())
    for item in entries:
        summary["checked"] += 1
        item.checked_at = datetime.datetime.utcnow()
        payment_id = item.razorpay_payment_id
        if not payment_id:
            item.last_reason = "Waiting for a Razorpay pay_ transaction ID"
            summary["pending"] += 1
            continue
        existing = _existing_donation(payment_id)
        if existing:
            item.purge_payload("already_recorded", "Donation already exists", existing.id)
            summary["already_recorded"] += 1
            continue
        form = forms.get((item.form_key or "").lower())
        if not form or not form.can_receipt:
            item.last_reason = "Form needs an active non-test campaign mapping"
            summary["pending"] += 1
            continue
        captured, error = _zoho_payment_is_captured(payment_id)
        if error or not captured:
            item.last_reason = (error or "Razorpay does not report this payment as captured")[:500]
            summary["pending"] += 1
            continue
        if mode == "shadow":
            item.status = "validated"
            item.last_reason = "Exact captured match found; shadow mode did not issue a receipt"
            summary["shadow_matches"] += 1
            continue
        payload = {"full_name": item.full_name or "", "phone": item.phone or "",
                   "email": item.email or "", "pan": item.pan or "", "amount": item.amount or ""}
        donation, create_error = _create_zoho_donation(
            payload, form.campaign, payment_id, item.razorpay_order_id,
        )
        if create_error:
            body, _status = create_error
            item.last_reason = str(body.get("error") or "Unable to create donation")[:500]
            summary["errors"].append(f"{payment_id}: {item.last_reason}")
            continue
        item.purge_payload("receipted", "Receipt issued after exact captured Razorpay match", donation.id)
        summary["created"] += 1
    db.session.commit()
    return summary


def purge_expired(config):
    """Erase unmatched PII after the agreed retention period (five days)."""
    days = max(1, int(config.get("ZOHO_ENTRY_RETENTION_DAYS") or 5))
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    rows = (ZohoEntryQueue.query.filter_by(status="pending")
            .filter(ZohoEntryQueue.stored_at < cutoff).all())
    for item in rows:
        item.purge_payload("expired", f"Unmatched entry expired after {days} days")
    if rows:
        db.session.commit()
    return len(rows)


def run(config):
    """One safe scheduled pass: collect, reconcile, then purge old payload."""
    mode = (config.get("ZOHO_AUTOMATION_MODE") or "off").lower()
    if mode not in {"off", "shadow", "live"}:
        return {"error": "ZOHO_AUTOMATION_MODE must be off, shadow, or live"}
    staged = stage_entries(config) if mode != "off" else {"forms": 0, "staged": 0, "already_known": 0, "errors": []}
    processed = process_entries(config) if mode != "off" else {"checked": 0, "created": 0}
    return {"mode": mode, "staged": staged, "processed": processed,
            "expired": purge_expired(config)}
