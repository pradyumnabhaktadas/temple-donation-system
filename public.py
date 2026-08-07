"""The public donation flow: the form, order creation, and every path that
can confirm a payment succeeded.

Payment confirmation has three layers, from most to least reliable:

  1. Webhook (razorpay_webhook) -- Razorpay's own server calls this
     directly, entirely independent of the donor's browser. This is the
     source of truth. Configure it under Razorpay Dashboard -> Settings ->
     Webhooks -> https://<your-domain>/webhooks/razorpay, subscribed to
     payment.captured.
  2. Browser fast path (verify_payment) -- checkout.js's `handler` callback
     posts here immediately after payment, so most donors see their
     receipt within a second or two. Not guaranteed to fire in every
     browser (JS context can be interrupted, some browsers restrict a
     payment iframe's ability to call back into the page).
  3. Client polling (donation_status) -- donate.html polls this endpoint
     every few seconds after checkout as a fallback. It doesn't confirm
     anything itself; it just reports whatever the webhook or fast path
     already recorded, so the donor's tab finds out even if #2 never fires.

_finalize_success() is idempotent and shared by all three, so it's safe
for more than one of them to fire for the same donation.
"""
import datetime
import hmac
import hashlib
import json
import io
import os
import threading

from flask import (
    Blueprint, render_template, request, jsonify, redirect, url_for,
    send_file, current_app, flash,
)

from extensions import db, csrf, limiter
from models import Donor, Campaign, Donation, ReceiptCounter, BaceProperty, Festival, SevaType, LiveToGivePurpose
from pdf_utils import generate_receipt_pdf, receipt_pdf_path
from email_utils import send_receipt_email
from utils import is_valid_pan

bp = Blueprint("public", __name__)


def _org_cfg():
    cfg = current_app.config
    return {
        "ORG_NAME": cfg["ORG_NAME"],
        "ORG_ADDRESS": cfg["ORG_ADDRESS"],
        "ORG_PAN": cfg["ORG_PAN"],
        "ORG_80G_REG_NO": cfg["ORG_80G_REG_NO"],
        "ORG_PHONE": cfg.get("ORG_PHONE", ""),
        "ORG_EMAIL": cfg.get("ORG_EMAIL", ""),
        "ORG_REG_INFO": cfg.get("ORG_REG_INFO", ""),
        "ORG_CLOSING_MESSAGE": cfg.get("ORG_CLOSING_MESSAGE", ""),
        "ORG_PARENT_NAME": cfg.get("ORG_PARENT_NAME", ""),
        "ORG_FOUNDER_LINE": cfg.get("ORG_FOUNDER_LINE", ""),
        "ORG_HO_ADDRESS": cfg.get("ORG_HO_ADDRESS", ""),
        "ORG_HO_PHONE": cfg.get("ORG_HO_PHONE", ""),
        "ORG_HO_EMAIL": cfg.get("ORG_HO_EMAIL", ""),
        "ORG_BRANCH_SHORT_NAME": cfg.get("ORG_BRANCH_SHORT_NAME", ""),
        "ORG_BRANCH_TYPE": cfg.get("ORG_BRANCH_TYPE", ""),
        "ORG_LOGO_PATH": cfg.get("ORG_LOGO_PATH", ""),
    }


class _InvalidFkError(Exception):
    """Raised by _validated_fk_id() -- caught in create_order() and turned
    into a normal 400 JSON error rather than propagating as a 500."""


_FK_LABELS = {
    "bace_property_id": "BACE property", "festival_id": "festival", "seva_type_id": "seva type",
    "live_to_give_purpose_id": "donation purpose",
}


def _validated_fk_id(data, key, model):
    """Reads an optional integer id out of a JSON request body and checks
    it actually exists in `model`. Used for the BACE property / festival /
    seva type pickers on their respective dedicated forms -- all optional,
    all foreign keys a client could otherwise send a bogus value for."""
    raw = data.get(key)
    if not raw:
        return None
    label = _FK_LABELS.get(key, key)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise _InvalidFkError(f"Invalid {label}.")
    if not model.query.get(value):
        raise _InvalidFkError(f"Invalid {label}.")
    return value


def find_or_create_donor(data):
    """Dedup a donor by PAN first, then phone, then email. This is the
    single-donor-database mechanism that prevents duplicate donor records
    across separate campaign submissions."""
    donor = None
    pan = (data.get("pan") or "").strip().upper()
    phone = (data.get("phone") or "").strip()
    email = (data.get("email") or "").strip().lower()
    whatsapp_number = (data.get("whatsapp_number") or "").strip()

    if pan:
        donor = Donor.query.filter_by(pan=pan).first()
    if donor is None and phone:
        donor = Donor.query.filter_by(phone=phone).first()
    if donor is None and email:
        donor = Donor.query.filter_by(email=email).first()

    if donor is None:
        donor = Donor(
            full_name=data.get("full_name", "").strip(),
            email=email or None,
            phone=phone or None,
            whatsapp_number=whatsapp_number or None,
            pan=pan or None,
            address=data.get("address", "").strip() or None,
            city=data.get("city", "").strip() or None,
            state=data.get("state", "").strip() or None,
            pincode=data.get("pincode", "").strip() or None,
        )
        db.session.add(donor)
    else:
        # Backfill any missing fields on the existing record rather than
        # creating a duplicate.
        donor.full_name = donor.full_name or data.get("full_name", "").strip()
        donor.email = donor.email or (email or None)
        donor.phone = donor.phone or (phone or None)
        donor.whatsapp_number = donor.whatsapp_number or (whatsapp_number or None)
        donor.pan = donor.pan or (pan or None)
        donor.address = donor.address or data.get("address", "").strip() or None
        donor.city = donor.city or data.get("city", "").strip() or None
        donor.state = donor.state or data.get("state", "").strip() or None
        donor.pincode = donor.pincode or data.get("pincode", "").strip() or None

    db.session.flush()
    return donor


@bp.route("/")
def donate_form():
    campaigns = Campaign.query.filter_by(is_active=True).order_by(Campaign.is_80g.desc(), Campaign.name).all()
    return render_template(
        "donate.html",
        campaigns=campaigns,
        razorpay_enabled=current_app.config["RAZORPAY_ENABLED"],
        razorpay_key_id=current_app.config["RAZORPAY_KEY_ID"],
        org_name=current_app.config["ORG_NAME"],
    )


@bp.route("/bace-rent")
def bace_rent_form():
    """Dedicated collection form for BACE property contributions -- same
    underlying donation pipeline as the main form (create-order/webhook/
    polling/receipt), just fixed to the "BACE Contribution" campaign with
    an added "which property" field instead of a general campaign picker.
    The property list is managed at Admin -> BACE Properties."""
    campaign = Campaign.query.filter_by(name="BACE Contribution").first()
    if campaign is None or not campaign.is_active:
        flash("The BACE Contribution campaign isn't set up yet -- please contact the office.")
        return redirect(url_for("public.donate_form"))

    properties = BaceProperty.query.filter_by(is_active=True).order_by(BaceProperty.name).all()
    return render_template(
        "bace_rent.html",
        campaign=campaign,
        properties=properties,
        razorpay_enabled=current_app.config["RAZORPAY_ENABLED"],
        razorpay_key_id=current_app.config["RAZORPAY_KEY_ID"],
        org_name=current_app.config["ORG_NAME"],
    )


@bp.route("/festival-seva")
def festival_seva_form():
    """Dedicated collection form for festival donations -- same underlying
    donation pipeline as the main form, fixed to the "Festivals" campaign,
    with an occasion picker (Festival) and an optional seva/sponsorship
    tier picker (SevaType, which pre-fills a suggested amount). Both lists
    are managed at Admin -> Festivals / Admin -> Seva Types."""
    campaign = Campaign.query.filter_by(name="Festivals").first()
    if campaign is None or not campaign.is_active:
        flash("The Festivals campaign isn't set up yet -- please contact the office.")
        return redirect(url_for("public.donate_form"))

    festivals = Festival.query.filter_by(is_active=True).order_by(
        Festival.event_date.is_(None), Festival.event_date, Festival.name
    ).all()
    seva_types = SevaType.query.filter_by(is_active=True).order_by(SevaType.name).all()
    return render_template(
        "festival_seva.html",
        campaign=campaign,
        festivals=festivals,
        seva_types=seva_types,
        razorpay_enabled=current_app.config["RAZORPAY_ENABLED"],
        razorpay_key_id=current_app.config["RAZORPAY_KEY_ID"],
        org_name=current_app.config["ORG_NAME"],
    )


@bp.route("/live-to-give")
def live_to_give_form():
    """Dedicated collection form for the "Live To Give" (Nitya Seva)
    campaign -- same underlying donation pipeline as the main form, fixed
    to the "Live To Give" campaign, with a donation-purpose picker
    (LiveToGivePurpose, managed at Admin -> Live To Give Purposes) and a
    donor-chosen 80G/Non-80G receipt type per donation (unique to this
    form -- every other campaign's 80G status is fixed, see
    Donation.effective_is_80g)."""
    campaign = Campaign.query.filter_by(name="Live To Give").first()
    if campaign is None or not campaign.is_active:
        flash("The Live To Give campaign isn't set up yet -- please contact the office.")
        return redirect(url_for("public.donate_form"))

    purposes = LiveToGivePurpose.query.filter_by(is_active=True).order_by(LiveToGivePurpose.name).all()
    return render_template(
        "live_to_give.html",
        campaign=campaign,
        purposes=purposes,
        razorpay_enabled=current_app.config["RAZORPAY_ENABLED"],
        razorpay_key_id=current_app.config["RAZORPAY_KEY_ID"],
        org_name=current_app.config["ORG_NAME"],
    )


@bp.route("/api/create-order", methods=["POST"])
@limiter.limit("30 per hour")
def create_order():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid request."}), 400

    try:
        campaign_id = int(data["campaign_id"])
        amount = float(data["amount"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Missing or invalid campaign/amount."}), 400

    campaign = Campaign.query.get_or_404(campaign_id)
    if amount <= 0:
        return jsonify({"error": "Invalid amount"}), 400
    if campaign.name == "Live To Give" and amount < 101:
        return jsonify({"error": "Minimum contribution for Live To Give is Rs. 101."}), 400

    if not data.get("consent"):
        return jsonify({"error": "Please confirm the data-use consent to continue."}), 400

    pan = (data.get("pan") or "").strip()
    if pan and not is_valid_pan(pan):
        return jsonify({"error": "That PAN doesn't look right. It should be 10 characters like ABCDE1234F."}), 400

    # Optional fields only meaningful for the dedicated BACE Contribution /
    # Festival Seva / Live To Give forms -- which campaign a request claims
    # to be for is incidental (any campaign_id could technically send one),
    # so validate each against the database rather than trust the caller.
    try:
        bace_property_id = _validated_fk_id(data, "bace_property_id", BaceProperty)
        festival_id = _validated_fk_id(data, "festival_id", Festival)
        seva_type_id = _validated_fk_id(data, "seva_type_id", SevaType)
        live_to_give_purpose_id = _validated_fk_id(data, "live_to_give_purpose_id", LiveToGivePurpose)
    except _InvalidFkError as e:
        return jsonify({"error": str(e)}), 400

    # Only the Live To Give form sends this -- the donor's own choice of
    # 80G vs Non-80G receipt for this specific donation. Any other value
    # (missing, or anything other than "80g"/"non80g") is treated as "not
    # asked", so Donation.effective_is_80g falls back to the campaign's own
    # is_80g flag exactly as it always has for every other form.
    receipt_type = data.get("receipt_type")
    if receipt_type == "80g":
        is_80g_requested = True
    elif receipt_type == "non80g":
        is_80g_requested = False
    else:
        is_80g_requested = None

    remarks = (data.get("remarks") or "").strip()[:300] or None

    donor = find_or_create_donor(data)

    donation = Donation(
        donor_id=donor.id,
        campaign_id=campaign.id,
        amount=amount,
        payment_mode="online",
        status="pending",
        recorded_by="online",
        bace_property_id=bace_property_id,
        festival_id=festival_id,
        seva_type_id=seva_type_id,
        live_to_give_purpose_id=live_to_give_purpose_id,
        is_80g_requested=is_80g_requested,
        remarks=remarks,
        consent_given=True,
        consent_at=datetime.datetime.utcnow(),
        consent_version=current_app.config.get("CONSENT_VERSION"),
        # Not from Razorpay -- captured from the donor's own request to our
        # server, for the same fraud/audit-trail purpose. request.remote_addr
        # reflects the real client IP behind Render's proxy because
        # ProxyFix is wired up in app.py for production.
        donor_ip_address=(request.remote_addr or "")[:45] or None,
        donor_user_agent=(request.headers.get("User-Agent") or "")[:300] or None,
    )
    db.session.add(donation)
    db.session.flush()

    order_id = None
    if current_app.config["RAZORPAY_ENABLED"]:
        import razorpay
        client = razorpay.Client(
            auth=(current_app.config["RAZORPAY_KEY_ID"], current_app.config["RAZORPAY_KEY_SECRET"])
        )
        order_receipt = f"donation_{donation.id}"
        try:
            order = client.order.create(
                {
                    "amount": int(amount * 100),  # paise
                    "currency": "INR",
                    "receipt": order_receipt,
                    "notes": {"donation_id": str(donation.id), "campaign": campaign.name},
                }
            )
        except Exception:
            # Razorpay unreachable/misconfigured -- don't leave an orphaned
            # pending donation behind, and show the donor a normal "try
            # again" message instead of a crashed page.
            db.session.rollback()
            current_app.logger.exception("Razorpay order creation failed")
            return jsonify({"error": "Could not start payment right now. Please try again in a moment."}), 502
        order_id = order["id"]
        donation.razorpay_order_id = order_id
        donation.razorpay_order_receipt = order_receipt

    db.session.commit()

    return jsonify(
        {
            "donation_id": donation.id,
            "order_id": order_id,
            "amount": amount,
            "razorpay_enabled": current_app.config["RAZORPAY_ENABLED"],
            "key_id": current_app.config["RAZORPAY_KEY_ID"],
        }
    )


def _send_receipt_email_background(app, donation_id, pdf_bytes):
    """Runs in a background thread -- see the comment in _finalize_success()
    for why. Needs its own app context (and its own DB session, which a
    fresh app context gives it) since it's not running inside the request
    that spawned it anymore."""
    with app.app_context():
        donation = Donation.query.get(donation_id)
        if donation is not None:
            try:
                send_receipt_email(donation, donation.donor, _org_cfg(), pdf_bytes)
            except Exception:
                app.logger.exception("Background receipt email failed for donation %s", donation_id)


def _finalize_success(donation):
    """Marks a donation successful, issues its receipt number, generates
    and stores the receipt PDF, and emails it. Called from all three
    confirmation paths described in the module docstring above.

    Idempotency guard: a receipt number must be issued exactly once per
    donation. Without this, a double-submitted verify/simulate/webhook
    call (double click, browser retry, webhook redelivery, etc.) would burn
    a second serial number and overwrite the first receipt on a donation
    that already succeeded -- exactly the kind of gap/duplicate that
    shouldn't show up in data that ultimately goes into the Form 10BD
    filing to the Income Tax Department.
    """
    if donation.status == "success" and donation.receipt_number:
        return

    campaign = donation.campaign
    receipt_number, fy = ReceiptCounter.next_receipt_number(donation.effective_is_80g, donation.donation_date)
    donation.receipt_number = receipt_number
    donation.financial_year = fy
    donation.status = "success"
    db.session.commit()

    pdf_bytes = generate_receipt_pdf(donation, donation.donor, campaign, _org_cfg())
    donation.receipt_pdf = pdf_bytes
    db.session.commit()

    # Email in a background thread rather than blocking the request on it.
    # A slow/hanging SMTP connection stacking on top of PDF generation can
    # blow past gunicorn's worker timeout, which kills the whole worker
    # mid-response -- not a clean error, just the connection dropping
    # outright. The receipt is already saved to the database at this point
    # regardless of whether the email send succeeds.
    #
    # Synchronous under TESTING so the test suite can assert on the send
    # deterministically instead of racing a background thread.
    app = current_app._get_current_object()
    if app.config.get("TESTING"):
        _send_receipt_email_background(app, donation.id, pdf_bytes)
    else:
        threading.Thread(
            target=_send_receipt_email_background, args=(app, donation.id, pdf_bytes), daemon=True
        ).start()


@bp.route("/api/verify-payment", methods=["POST"])
@limiter.limit("30 per hour")
def verify_payment():
    """Browser fast path -- see module docstring, layer 2."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid request."}), 400

    try:
        donation_id = int(data["donation_id"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Missing or invalid donation_id."}), 400
    donation = Donation.query.get_or_404(donation_id)

    if "razorpay_order_id" not in data or "razorpay_payment_id" not in data:
        return jsonify({"error": "Missing payment verification fields."}), 400

    key_secret = current_app.config["RAZORPAY_KEY_SECRET"]
    payload = f"{data['razorpay_order_id']}|{data['razorpay_payment_id']}"
    expected_sig = hmac.new(key_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected_sig, data.get("razorpay_signature", "")):
        donation.status = "failed"
        db.session.commit()
        return jsonify({"error": "Signature verification failed"}), 400

    donation.razorpay_payment_id = data["razorpay_payment_id"]
    _finalize_success(donation)
    return jsonify({"ok": True, "receipt_number": donation.receipt_number})


@bp.route("/api/donation-status/<int:donation_id>", methods=["GET"])
@limiter.limit("60 per minute")
def donation_status(donation_id):
    """Client polling target -- see module docstring, layer 3. Doesn't
    confirm anything itself; just reports whatever the webhook or the
    browser fast path has already recorded, so a donor's tab finds out
    even when the fast path never fires."""
    donation = Donation.query.get_or_404(donation_id)
    return jsonify({"status": donation.status, "receipt_number": donation.receipt_number})


@bp.route("/api/simulate-payment", methods=["POST"])
@limiter.limit("30 per hour")
def simulate_payment():
    """Only meaningful when Razorpay keys are not configured (demo mode).
    Lets you exercise the full donor -> donation -> receipt pipeline
    without a live payment gateway."""
    if current_app.config["RAZORPAY_ENABLED"]:
        return jsonify({"error": "Live payments are enabled; simulate is disabled."}), 400

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid request."}), 400

    try:
        donation_id = int(data["donation_id"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Missing or invalid donation_id."}), 400
    donation = Donation.query.get_or_404(donation_id)
    donation.razorpay_payment_id = "SIMULATED"
    _finalize_success(donation)
    return jsonify({"ok": True, "receipt_number": donation.receipt_number})


@bp.route("/webhooks/razorpay", methods=["POST"])
@csrf.exempt
def razorpay_webhook():
    """Server-to-server payment confirmation -- see module docstring,
    layer 1, the source of truth. Called directly by Razorpay (Dashboard ->
    Settings -> Webhooks), entirely independent of the donor's browser.

    Verified using RAZORPAY_WEBHOOK_SECRET -- a separate secret from
    RAZORPAY_KEY_SECRET, chosen when you add the webhook in the Dashboard
    and never sent to the browser. There's no session/cookie on a
    server-to-server call, so this route is CSRF-exempt; the webhook
    signature check *is* its authentication.
    """
    webhook_secret = current_app.config.get("RAZORPAY_WEBHOOK_SECRET")
    if not webhook_secret:
        # Nothing configured to verify this against -- refuse rather than
        # trust an unverified request claiming to be Razorpay.
        return jsonify({"error": "Webhook not configured"}), 400

    raw_body = request.get_data()
    signature = request.headers.get("X-Razorpay-Signature", "")
    expected_sig = hmac.new(webhook_secret.encode(), raw_body, hashlib.sha256).hexdigest()

    if not signature or not hmac.compare_digest(expected_sig, signature):
        return jsonify({"error": "Invalid signature"}), 400

    event = request.get_json(silent=True) or {}
    event_type = event.get("event")

    if event_type not in ("payment.captured", "order.paid"):
        # Acknowledge (200) anything we don't act on, so Razorpay doesn't
        # keep retrying an event we're deliberately ignoring.
        return jsonify({"ok": True, "ignored": event_type}), 200

    payment_entity = event.get("payload", {}).get("payment", {}).get("entity", {})
    order_id = payment_entity.get("order_id")
    payment_id = payment_entity.get("id")

    if not order_id:
        return jsonify({"error": "Missing order_id in payload"}), 400

    donation = Donation.query.filter_by(razorpay_order_id=order_id).first()
    if donation is None:
        # No matching donation on our side (e.g. a stray event from a
        # different Razorpay account/test mode) -- acknowledge so Razorpay
        # stops retrying, but there's nothing to finalize.
        return jsonify({"ok": True, "matched": False}), 200

    if payment_id:
        donation.razorpay_payment_id = payment_id
    _apply_payment_details(donation, payment_entity)
    _finalize_success(donation)

    return jsonify({"ok": True, "receipt_number": donation.receipt_number}), 200


def _apply_payment_details(donation, payment_entity):
    """Pulls the useful reconciliation fields out of a Razorpay
    payment.entity payload and stores them on the donation, plus the full
    payload verbatim as JSON so nothing is lost even if a field below
    doesn't cover what you need later.

    Method-specific reference so you can match a donation to a bank
    statement line without opening the raw payload: UPI VPA, masked card
    (network + last 4), netbanking bank code, or wallet name.
    """
    method = payment_entity.get("method")
    donation.razorpay_method = method
    donation.razorpay_status = payment_entity.get("status")
    donation.razorpay_currency = payment_entity.get("currency")

    reference = None
    if method == "upi":
        upi = payment_entity.get("upi") or {}
        reference = payment_entity.get("vpa") or upi.get("vpa")
        donation.razorpay_upi_flow = upi.get("flow")
    elif method == "card":
        card = payment_entity.get("card") or {}
        network = card.get("network")
        last4 = card.get("last4")
        if network or last4:
            reference = f"{network or 'Card'} ****{last4 or ''}".strip()
        donation.razorpay_card_network = network
        donation.razorpay_card_type = card.get("type")
    elif method == "netbanking":
        reference = payment_entity.get("bank")
    elif method == "wallet":
        reference = payment_entity.get("wallet")
    donation.razorpay_reference = reference

    # Bank-side reference number for reconciliation -- present under
    # different keys depending on method/acquirer; store whichever shows up.
    acquirer_data = payment_entity.get("acquirer_data") or {}
    donation.razorpay_utr = (
        acquirer_data.get("rrn")
        or acquirer_data.get("upi_transaction_id")
        or acquirer_data.get("bank_transaction_id")
        or acquirer_data.get("transaction_id")
    )

    fee_paise = payment_entity.get("fee")
    donation.razorpay_fee = (fee_paise / 100) if isinstance(fee_paise, (int, float)) else None

    donation.razorpay_email = payment_entity.get("email")
    donation.razorpay_contact = payment_entity.get("contact")

    try:
        donation.razorpay_raw_payload = json.dumps(payment_entity)
    except (TypeError, ValueError):
        donation.razorpay_raw_payload = None


@bp.route("/donate/success/<int:donation_id>")
def donate_success(donation_id):
    donation = Donation.query.get_or_404(donation_id)
    return render_template("donate_success.html", donation=donation)


@bp.route("/receipt/<int:donation_id>")
def download_receipt(donation_id):
    donation = Donation.query.get_or_404(donation_id)
    if donation.status != "success" or not donation.receipt_number:
        flash("Receipt not available for this donation.")
        return redirect(url_for("public.donate_form"))

    if donation.receipt_pdf:
        pdf_bytes = donation.receipt_pdf
    else:
        # Legacy fallback: donations issued before receipts moved into the
        # database (see README "Receipt storage") were written to disk
        # instead. Read from there if it's still around, rather than
        # 404ing on a receipt that was genuinely issued.
        legacy_path = receipt_pdf_path(donation.receipt_number)
        if os.path.isfile(legacy_path):
            with open(legacy_path, "rb") as f:
                pdf_bytes = f.read()
        else:
            flash("This receipt needs to be regenerated -- please contact the office.")
            return redirect(url_for("public.donate_form"))

    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"{donation.receipt_number.replace('/', '_')}.pdf",
    )
