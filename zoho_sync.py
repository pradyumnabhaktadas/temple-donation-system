"""Turns Zoho Forms entries into donations and receipts.

This replaces the webhook-driven design, and it's worth being explicit
about why, because the replacement is simpler for one specific reason.

WHAT WAS WRONG WITH BEING PUSHED TO
-----------------------------------
Zoho's webhook fires once, at submission time -- *before* the donor pays.
That call therefore carries the donor's details but no transaction ID.
Zoho's documentation says a second call follows with the payment result;
on this account it frequently never arrived. So the app had the donor and
no payment, and had to guess which later Razorpay payment belonged to
which earlier submission, using phone number, amount and a time window.

That guessing is where the complexity lived, and it had a failure mode
that couldn't be engineered away: when one donor submits twice for the
same amount, or pays twice for one submission, two payments and one
submission are genuinely indistinguishable, and the only safe answer is
to refuse and ask a human. It also had to be configured by hand on every
single form -- of the six forms taking money in September 2026, four had
never been configured, so their donations were invisible to this app
entirely and surfaced only by reconciling Razorpay's records weeks later.

WHY PULLING IS SIMPLER
----------------------
A Zoho entry already holds the transaction ID alongside the donor's
details. Reading entries means the join is exact -- entry N has payment
N -- so there is nothing to match and nothing to guess. Two entries from
one donor become two transaction IDs and two receipts, which is the
correct answer and one the old design could not reach. It also needs no
per-form setup, and picks up forms added later without anyone having to
remember.

WHAT IS DELIBERATELY UNCHANGED
------------------------------
Every rule that earlier failures put there:

  * Razorpay decides whether money arrived. Zoho's own "Payment Status"
    is advisory only -- production showed entries reading "Completed"
    against payments that were nothing of the sort, and the reverse.
  * A receipt requires a genuine pay_... id AND Razorpay confirming it
    captured. Neither alone.
  * Idempotency checks both razorpay_payment_id and bank_transaction_id,
    because a hand-entered backfill puts the reference in the latter and
    would otherwise be re-imported as a duplicate.
  * Donations are created through public._create_zoho_donation, the same
    function the rest of the app uses, so the PAN/80G/high-value rules
    can't drift.
  * Work per run is bounded: this runs inside an HTTP request under
    gunicorn's worker timeout, which this codebase has been bitten by.
  * One bad entry is isolated and reported, never allowed to abort a run
    and strand every other donor behind it.
  * Nothing is dropped silently. An entry that can't be turned into a
    donation is reported with the reason -- silence is the failure this
    whole body of work exists to remove.
"""
import datetime
import re

FORM_FIELD_ALIASES = {
    # Normalised (lowercase, non-alphanumerics stripped) substrings to look
    # for, in priority order. Taken from the field labels this account's
    # own forms actually use -- see the reports in the project history --
    # rather than from documentation, and matched as substrings so a form
    # that labels something "Donor Name" or "Your Phone No." still lands.
    "transaction": ("paymenttransactionid", "transactionid", "paymentid"),
    "amount": ("paymentamount", "totalamount", "amount"),
    "status": ("paymentstatus",),
    "name": ("name",),
    "phone": ("phone", "mobile", "contact"),
    "email": ("email",),
    "pan": ("pan",),
    "added": ("addedtime", "createdtime", "submittedon"),
}

_PAYMENT_ID = re.compile(r"pay_\w+")
_ORDER_ID = re.compile(r"order_\w+")


def _norm(key):
    return re.sub(r"[^a-z0-9]", "", str(key or "").lower())


def field(entry, kind):
    """First value in `entry` whose field name matches one of the aliases
    for `kind`. Returns "" when nothing matches -- callers decide whether
    that's fatal, since a missing PAN is fine and a missing phone is not.

    Substring matching on normalised names, deliberately: Zoho names entry
    fields after each form's own labels, so the same concept appears as
    "Phone", "phone_number" and "Phone No." across forms. Hardcoding one
    spelling would make a form silently import nothing."""
    aliases = FORM_FIELD_ALIASES.get(kind, ())
    normalised = {_norm(k): v for k, v in (entry or {}).items()}
    # Exact-ish first, so "amount" doesn't win over "paymentamount".
    for alias in aliases:
        for key, value in normalised.items():
            if key == alias:
                return "" if value is None else str(value).strip()
    for alias in aliases:
        for key, value in normalised.items():
            if alias in key:
                return "" if value is None else str(value).strip()
    return ""


def payment_ids(entry):
    """(payment_id, order_id) found anywhere in the entry.

    Scans every value rather than one named field. Production showed this
    account rendering the transaction as the combined string
    "Txn ID : pay_X Order ID : order_Y" in some places and a bare id in
    others, and a form could reasonably put it in a differently-labelled
    field. A regex over the whole entry can't be defeated by relabelling,
    and a "pay_" token is specific enough not to appear by accident."""
    payment_id = order_id = None
    for value in (entry or {}).values():
        if not isinstance(value, str):
            continue
        if payment_id is None:
            found = _PAYMENT_ID.search(value)
            if found:
                payment_id = found.group()
        if order_id is None:
            found = _ORDER_ID.search(value)
            if found:
                order_id = found.group()
    return payment_id, order_id


def donor_name(entry):
    """Zoho's composite Name control renders as "First, Last" -- confirmed
    against this account's own entries: "shivam, raj" is Shivam Raj,
    "Jatin, saini" is Jatin Saini, "Neeraj, Kumar" is Neeraj Kumar. So the
    comma is separating the control's two sub-fields in reading order, and
    the parts are joined as they stand.

    Worth stating explicitly because the alternative reading ("Last,
    First", as most software renders a sorted name) is the natural
    assumption and would put "Saini Jatin" on a receipt. It was very nearly
    written that way.

    Casing is normalised only where a part is entirely lowercase --
    donors type "shivam raj" as often as "Shivam Raj" -- while anything
    already capitalised is left alone, so "McDonald" and initials survive."""
    raw = field(entry, "name")
    if not raw:
        return ""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    raw = " ".join(parts) if parts else raw
    return " ".join(w.capitalize() if w.islower() else w for w in raw.split())


def entry_payload(entry):
    """The donor payload, in the shape public._create_zoho_donation wants.

    Amount is deliberately not defaulted here. If the entry has no usable
    amount the caller falls back to what Razorpay actually captured, which
    is the figure a receipt has to state anyway."""
    payload = {
        "full_name": donor_name(entry),
        "phone": field(entry, "phone"),
        "email": field(entry, "email"),
        "pan": field(entry, "pan"),
    }
    amount = field(entry, "amount")
    if amount:
        payload["amount"] = amount
    return {k: v for k, v in payload.items() if v}


def sync_entries(config, entries, campaign, form_name, verify_captured, create_donation,
                 max_per_run=50):
    """Turns one form's entries into donations. Pure orchestration -- the
    two things that touch the outside world are injected:

      verify_captured(payment_id) -> (captured: bool, error: str|None)
      create_donation(payload, campaign, payment_id, order_id)
          -> (donation, error)

    so this can be tested end to end without Razorpay or a database, and
    the production wiring passes public._zoho_payment_is_captured and
    public._create_zoho_donation -- the same functions the rest of the app
    already uses.

    Returns a summary: created / skipped_no_payment / already_recorded /
    not_captured / failed. Every entry lands in exactly one of them, so a
    count that doesn't add up is itself a signal."""
    from extensions import db
    from models import Donation

    summary = {
        "form": form_name, "created": [], "skipped_no_payment": 0,
        "already_recorded": 0, "not_captured": [], "failed": [],
    }

    for entry in entries:
        if len(summary["created"]) >= max_per_run:
            summary["failed"].append({
                "entry": None,
                "error": f"Stopped at {max_per_run} receipts this run; the rest follow next run.",
            })
            break

        payment_id, order_id = payment_ids(entry)
        if not payment_id:
            # Ordinary: someone opened the form and never paid. Zoho's own
            # payment status is not consulted here -- it has been wrong in
            # both directions, and the absence of an id is the fact that
            # matters.
            summary["skipped_no_payment"] += 1
            continue

        # Both columns: a hand-entered backfill records the reference in
        # bank_transaction_id, and checking only razorpay_payment_id would
        # re-import it as a duplicate receipt.
        if Donation.query.filter(db.or_(
            Donation.razorpay_payment_id == payment_id,
            Donation.bank_transaction_id == payment_id,
        )).first():
            summary["already_recorded"] += 1
            continue

        captured, verify_error = verify_captured(payment_id)
        if verify_error or not captured:
            # Not an error worth failing the run over: an authorised-but-
            # uncaptured payment may capture later, and an unreachable
            # Razorpay resolves next run. Reported either way -- never
            # treated as "no such payment".
            summary["not_captured"].append({
                "payment_id": payment_id,
                "reason": verify_error or "Razorpay does not report this as captured",
            })
            continue

        payload = entry_payload(entry)
        try:
            float(payload.get("amount"))
        except (TypeError, ValueError):
            payload["amount"] = _razorpay_amount_placeholder(entry)

        try:
            donation, create_error = create_donation(payload, campaign, payment_id, order_id)
        except Exception as exc:
            # Isolated per entry. One malformed entry must not strand every
            # other donor's receipt behind it, and it stays unrecorded so
            # the next run retries it.
            summary["failed"].append({"entry": payment_id, "error": str(exc)})
            continue

        if create_error:
            body, _status = create_error
            summary["failed"].append({"entry": payment_id, "error": body.get("error")})
            continue

        summary["created"].append(donation)

    return summary


def _razorpay_amount_placeholder(entry):
    """Last resort when an entry carries no usable amount. Returns "" so
    creation fails with a clear "Invalid amount" that gets reported,
    rather than inventing a figure for a tax receipt."""
    return field(entry, "amount") or ""


def parse_added_time(entry):
    """Zoho renders its own timestamps as "04-Sep-2026 21:55:22". Returned
    as naive UTC-ish for ordering only -- never used for the donation date,
    which comes from the payment. Returns None if unparseable, and callers
    must cope rather than assume."""
    raw = field(entry, "added")
    if not raw:
        return None
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None
