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
from models import Donor, Campaign, Donation, ReceiptCounter
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
        # Missing/non-numeric campaign_id or amount -- previously this
        # raised an unhandled ValueError straight out of int()/float(),
        # which the donor would see as a generic server error page instead
        # of a normal "something's wrong with your submission" message.
        return jsonify({"error": "Missing or invalid campaign/amount."}), 400

    campaign = Campaign.query.get_or_404(campaign_id)
    if amount <= 0:
        return jsonify({"error": "Invalid amount"}), 400

    if not data.get("consent"):
        return jsonify({"error": "Please confirm the data-use consent to continue."}), 400

    pan = (data.get("pan") or "").strip()
    if pan and not is_valid_pan(pan):
        return jsonify({"error": "That PAN doesn't look right. It should be 10 characters like ABCDE1234F."}), 400

    donor = find_or_create_donor(data)

    donation = Donation(
        donor_id=donor.id,
        campaign_id=campaign.id,
        amount=amount,
        payment_mode="online",
        status="pending",
        recorded_by="online",
        # Actually persist the consent this donor just gave -- previously
        # only checked at submission time and then discarded, with no
        # record of it afterwards.
        consent_given=True,
        consent_at=datetime.datetime.utcnow(),
        consent_version=current_app.config.get("CONSENT_VERSION"),
    )
    db.session.add(donation)
    db.session.flush()

    order_id = None
    if current_app.config["RAZORPAY_ENABLED"]:
        import razorpay
        client = razorpay.Client(
            auth=(current_app.config["RAZORPAY_KEY_ID"], current_app.config["RAZORPAY_KEY_SECRET"])
        )
        order = client.order.create(
            {
                "amount": int(amount * 100),  # paise
                "currency": "INR",
                "receipt": f"donation_{donation.id}",
                "notes": {"donation_id": str(donation.id), "campaign": campaign.name},
            }
        )
        order_id = order["id"]
        donation.razorpay_order_id = order_id

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
    # Idempotency guard: a receipt number must be issued exactly once per
    # donation. Without this, a double-submitted verify/simulate call (double
    # click, browser retry, etc.) would burn a second serial number and
    # overwrite the first receipt on a donation that already succeeded --
    # exactly the kind of gap/duplicate that shouldn't show up in data that
    # ultimately goes into the Form 10BD filing to the Income Tax Department.
    if donation.status == "success" and donation.receipt_number:
        return

    campaign = donation.campaign
    receipt_number, fy = ReceiptCounter.next_receipt_number(campaign.is_80g, donation.donation_date)
    donation.receipt_number = receipt_number
    donation.financial_year = fy
    donation.status = "success"
    db.session.commit()

    pdf_bytes = generate_receipt_pdf(donation, donation.donor, campaign, _org_cfg())
    donation.receipt_pdf = pdf_bytes
    db.session.commit()

    # Send the email in a background thread rather than blocking this
    # request on it. A slow/hanging SMTP connection (Gmail has been observed
    # taking 10-20s+ over some hosts' networks) stacking on top of PDF
    # generation can blow past gunicorn's worker timeout (30s default),
    # which SIGKILLs the whole worker mid-response -- not a clean 500, but
    # the connection dropping outright while a donor is watching Razorpay
    # redirect them back after paying. The receipt is already saved to the
    # database at this point regardless of whether the email send succeeds.
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
    expected_sig = hmac.new(
        key_secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_sig, data.get("razorpay_signature", "")):
        donation.status = "failed"
        db.session.commit()
        return jsonify({"error": "Signature verification failed"}), 400

    donation.razorpay_payment_id = data["razorpay_payment_id"]
    _finalize_success(donation)
    return jsonify({"ok": True, "receipt_number": donation.receipt_number})


@bp.route("/payment/callback", methods=["POST"])
@csrf.exempt
def payment_callback():
    """Second, more reliable confirmation path alongside /api/verify-payment.

    The JS `handler` callback above only runs if checkout.js successfully
    calls back into the donor's own tab after payment -- this has been
    observed to silently not fire in some browsers (notably Safari, where
    Intelligent Tracking Prevention can block the checkout iframe's
    postMessage back to the parent page), leaving a donation stuck on
    "pending" forever even though Razorpay shows the payment as captured.

    Razorpay Checkout supports a `callback_url` + `redirect: true` option
    (set in donate.html) that, when present, has Razorpay's own server do a
    real HTTP POST/redirect straight to this route instead of relying on
    JS in the donor's tab at all -- so it works even if the tab's JS
    context never gets the postMessage. Same signature verification as
    /api/verify-payment; _finalize_success() is idempotent so it's safe for
    this, /api/verify-payment, and the webhook to all fire for the same
    donation.
    """
    data = request.form
    order_id = data.get("razorpay_order_id")
    payment_id = data.get("razorpay_payment_id")
    signature = data.get("razorpay_signature")

    donation = Donation.query.filter_by(razorpay_order_id=order_id).first() if order_id else None

    if not (order_id and payment_id and signature) or donation is None:
        flash("We couldn't confirm your payment. If money was deducted, please contact the temple office.")
        return redirect(url_for("public.donate_form"))

    key_secret = current_app.config["RAZORPAY_KEY_SECRET"]
    payload = f"{order_id}|{payment_id}"
    expected_sig = hmac.new(key_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected_sig, signature):
        donation.status = "failed"
        db.session.commit()
        flash("Payment verification failed. If money was deducted, please contact the temple office.")
        return redirect(url_for("public.donate_form"))

    donation.razorpay_payment_id = payment_id
    try:
        _finalize_success(donation)
    except Exception:
        # Payment is already verified genuine at this point (signature
        # check above passed) -- a failure past here is something like a
        # PDF-generation bug, not a fake payment. Don't leave the donor
        # looking at a crashed page after money has actually moved; the
        # webhook (if configured) will retry finalization independently,
        # and this is loud in the server logs either way.
        current_app.logger.exception(
            "Failed to finalize donation %s after verified Razorpay callback", donation.id
        )
        flash(
            "Your payment was received, but we hit a snag preparing your receipt. "
            "It will appear shortly, or please contact the temple office with payment ID "
            f"{payment_id}."
        )
        return redirect(url_for("public.donate_form"))
    return redirect(url_for("public.donate_success", donation_id=donation.id))


@bp.route("/api/donation-status/<int:donation_id>", methods=["GET"])
@limiter.limit("60 per minute")
def donation_status(donation_id):
    """Polled by donate.html after checkout, as the reliable fallback to
    the JS `handler` callback (which has been observed not firing in some
    browsers) and to a callback_url redirect (found to get blocked before
    leaving the checkout iframe on at least one real device). The webhook
    finalizes donations server-to-server, independent of the browser --
    this endpoint just lets the donor's tab find out once that's happened,
    without needing any particular browser-mediated confirmation to work.
    """
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
    """Server-to-server payment confirmation, called directly by Razorpay
    (Dashboard -> Settings -> Webhooks), independent of the browser.

    /api/verify-payment above only fires if the donor's browser stays open
    long enough to run Razorpay checkout's `handler` callback after paying.
    This webhook is a reliable backstop: Razorpay calls it from their own
    servers regardless of what happens to the donor's tab, so the donation
    still gets finalized (receipt generated + emailed) even if they close
    the browser, lose signal, or the callback JS never runs for some other
    reason. _finalize_success() is idempotent, so it's safe for this and
    /api/verify-payment to both fire for the same donation.

    Verified using RAZORPAY_WEBHOOK_SECRET -- a separate secret from
    RAZORPAY_KEY_SECRET, generated when you add the webhook in the
    Dashboard and never sent to the browser. There's no session/cookie on a
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

    reference = None
    if method == "upi":
        reference = payment_entity.get("vpa")
    elif method == "card":
        card = payment_entity.get("card") or {}
        network = card.get("network")
        last4 = card.get("last4")
        if network or last4:
            reference = f"{network or 'Card'} ****{last4 or ''}".strip()
    elif method == "netbanking":
        reference = payment_entity.get("bank")
    elif method == "wallet":
        reference = payment_entity.get("wallet")
    donation.razorpay_reference = reference

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
