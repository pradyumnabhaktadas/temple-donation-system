"""The public donation flow: the form, order creation, and every path that
can confirm a payment succeeded.

Payment confirmation has three layers, from most to least reliable:

  1. Webhook (razorpay_webhook) -- Razorpay's own server calls this
     directly, entirely independent of the donor's browser. This is the
     source of truth. Configure it under Razorpay Dashboard -> Settings ->
     Webhooks -> https://<your-domain>/webhooks/razorpay. Subscribe at
     least payment.captured (finalizes the donation); payment.failed and
     payment.dispute.* are also handled if subscribed (see
     _handle_payment_failed/_handle_payment_dispute below) but aren't
     required for the core donation flow to work. Any other event type
     is acknowledged and ignored -- safe to subscribe to more than these
     without code changes.
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
import re
import threading

from flask import (
    Blueprint, render_template, request, jsonify, redirect, url_for,
    send_file, current_app, flash,
)

from extensions import db, csrf, limiter
from models import Donor, Campaign, Donation, ReceiptCounter, BaceProperty, Festival, SevaType, LiveToGivePurpose
from pdf_utils import generate_receipt_pdf, receipt_pdf_path
from email_utils import send_receipt_email
from whatsapp_utils import send_receipt_whatsapp
from utils import is_valid_pan, is_valid_phone, normalize_phone

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


def _normalize_name(name):
    """Case/whitespace-insensitive comparison key for donor names -- used
    to tell "same person, retyped slightly differently" apart from
    "different person" when a phone/email is shared (see below)."""
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


# Income Tax Rule 114B requires PAN to be quoted for various high-value
# transactions once they reach Rs 50,000 -- this app requires PAN (and a
# postal address, so the office can actually reach a large donor if
# anything needs following up) starting at Rs 49,000 instead, as a safety
# margin under that line rather than cutting it exactly at the legal
# threshold. Applies to every donation entry point regardless of 80G
# status (BACE Contribution payments are not tax-deductible but can still
# be large enough to trigger this same PAN-quoting requirement).
HIGH_VALUE_PAN_THRESHOLD = 49000


def high_value_pan_address_error(amount, pan, address):
    """Returns an error message if `amount` requires PAN+address (see
    HIGH_VALUE_PAN_THRESHOLD) but either is missing, else None. Shared by
    every donation entry point -- the public donation form (create_order),
    admin manual entry, and the CSV importers -- so the rule can't drift
    out of sync between them. Doesn't re-validate PAN's *format* (callers
    already do that separately via is_valid_pan); this only checks
    presence."""
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return None  # a bad amount is caught elsewhere; not this function's job
    if amount <= HIGH_VALUE_PAN_THRESHOLD:
        return None
    missing = []
    if not (pan or "").strip():
        missing.append("PAN")
    if not (address or "").strip():
        missing.append("address")
    if not missing:
        return None
    verb = "is" if len(missing) == 1 else "are"
    return (
        f"{' and '.join(missing)} {verb} required for donations above "
        f"Rs. {HIGH_VALUE_PAN_THRESHOLD:,} (Income Tax rules require PAN to be quoted for high-value transactions)."
    )


def find_or_create_donor(data):
    """Dedup a donor by PAN first, then phone+name, then email+name. This
    is the single-donor-database mechanism that prevents duplicate donor
    records across separate campaign submissions.

    PAN is treated as a strong identity signal (it's legally unique to one
    person) -- a PAN match always updates that donor's record with
    whatever this submission supplied.

    Phone and email are *contact* details, not identity -- it's common in
    Indian households for a spouse, parents, or grown children to all
    donate through one shared phone number (or email). So a phone/email
    match only counts as "the same donor" if the name on this submission
    also matches the name already on file (normalized, case/whitespace-
    insensitive). If the phone/email matches but the name doesn't, this is
    a *different* person who happens to share that contact detail -- a new
    donor record is created instead of overwriting the existing one.
    Without this check, one family member's donation could silently
    overwrite another's name/PAN/address on file (e.g. your PAN ending up
    attached to a relative's name, or vice versa) -- exactly the failure
    this function is built to prevent.
    """
    donor = None
    pan = (data.get("pan") or "").strip().upper()
    # Normalized to a plain 10-digit local number regardless of how it was
    # typed ("+91 88020 81265", "918802081265", "08802081265", ...) -- see
    # normalize_phone()'s docstring. Without this, the same donor typing
    # their number differently across two donations (or a donor logging in
    # with a different format than the one stored) would silently fail to
    # match.
    phone = normalize_phone(data.get("phone"))
    email = (data.get("email") or "").strip().lower()
    whatsapp_number = normalize_phone(data.get("whatsapp_number"))
    full_name = data.get("full_name", "").strip()
    incoming_name = _normalize_name(full_name)

    if pan:
        donor = Donor.query.filter_by(pan=pan).first()

    if donor is None and phone and incoming_name:
        candidates = Donor.query.filter_by(phone=phone).all()
        donor = next((d for d in candidates if _normalize_name(d.full_name) == incoming_name), None)

    if donor is None and email and incoming_name:
        candidates = Donor.query.filter_by(email=email).all()
        donor = next((d for d in candidates if _normalize_name(d.full_name) == incoming_name), None)

    if donor is None:
        donor = Donor(
            full_name=full_name,
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
        # Update the existing record with whatever was entered on *this*
        # donation, rather than only backfilling blanks -- safe to do here
        # because we only reach this branch on a PAN match (definitely the
        # same person) or a phone/email match where the name also agreed
        # (see the matching logic above). A field left blank on this
        # submission keeps whatever was already on file instead of being
        # wiped out.
        donor.full_name = full_name or donor.full_name
        donor.email = email or donor.email
        donor.phone = phone or donor.phone
        donor.whatsapp_number = whatsapp_number or donor.whatsapp_number
        donor.pan = pan or donor.pan
        donor.address = data.get("address", "").strip() or donor.address
        donor.city = data.get("city", "").strip() or donor.city
        donor.state = data.get("state", "").strip() or donor.state
        donor.pincode = data.get("pincode", "").strip() or donor.pincode

    db.session.flush()
    return donor


@bp.route("/")
def donate_form():
    """Main public donation page. This *is* the "Live To Give" (Nitya
    Seva) form -- there used to be a separate general-purpose multi-campaign
    picker here plus a dedicated /live-to-give page; the two served the
    same purpose (donor picks any purpose and any amount) so they were
    merged into this one route. Festival Seva and BACE Contribution remain
    separate dedicated forms, each fixed to its own campaign -- see
    festival_seva_form()/bace_rent_form() below.

    Purpose picker (LiveToGivePurpose, managed at Admin -> Live To Give
    Purposes) and donor-chosen 80G/Non-80G receipt type per donation are
    unchanged from the old dedicated page -- see Donation.effective_is_80g.
    """
    campaign = Campaign.query.filter_by(name="Live To Give").first()
    purposes = []
    if campaign is not None and campaign.is_active:
        purposes = LiveToGivePurpose.query.filter_by(is_active=True).order_by(LiveToGivePurpose.name).all()
    return render_template(
        "donate.html",
        campaign=campaign,
        purposes=purposes,
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
    The property list is managed at Admin -> BACE."""
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


@bp.route("/about")
def about_us():
    """Static About Us page -- mission/activities/contact info. org_name,
    org_about_text, org_contact_address, org_contact_email are already
    available in every template via the inject_org context processor
    (see app.py), so nothing extra needs to be passed in here."""
    return render_template("about.html")


@bp.route("/live-to-give")
def live_to_give_form():
    """Live To Give used to be its own dedicated page (live_to_give.html);
    it's now just the main Donate page (see donate_form() above) -- kept
    here as a redirect so any already-shared /live-to-give links (WhatsApp
    receipt messages already sent, QR codes, bookmarks) keep working
    instead of 404ing."""
    return redirect(url_for("public.donate_form"), code=301)


@bp.route("/robots.txt")
def robots_txt():
    """Allows every public page to be crawled/indexed, points crawlers at
    the sitemap below, and explicitly keeps admin/donor-account/API/
    webhook paths out of search results -- none of those are meant for
    the public web, and there's no benefit (and some risk) to a search
    engine indexing an admin login page or a raw JSON endpoint."""
    lines = [
        "User-agent: *",
        "Disallow: /admin/",
        "Disallow: /api/",
        "Disallow: /webhooks/",
        "Disallow: /donor/",
        "Disallow: /receipt/",
        f"Sitemap: {url_for('public.sitemap_xml', _external=True)}",
    ]
    return current_app.response_class("\n".join(lines) + "\n", mimetype="text/plain")


@bp.route("/sitemap.xml")
def sitemap_xml():
    """Static list of the handful of public-facing pages actually meant to
    be indexed -- there's no dynamic/per-donor public content on this
    site (donation forms and the About page are the entire public
    surface), so a hand-maintained list is simpler and more reliable than
    generating one from the URL map, which would also pick up admin/API/
    donor-portal routes that shouldn't be here."""
    pages = [
        {"loc": url_for("public.donate_form", _external=True), "priority": "1.0"},
        {"loc": url_for("public.about_us", _external=True), "priority": "0.8"},
        {"loc": url_for("public.festival_seva_form", _external=True), "priority": "0.8"},
        {"loc": url_for("public.bace_rent_form", _external=True), "priority": "0.8"},
    ]
    xml_lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for page in pages:
        xml_lines.append(f'  <url><loc>{page["loc"]}</loc><priority>{page["priority"]}</priority></url>')
    xml_lines.append("</urlset>")
    return current_app.response_class("\n".join(xml_lines) + "\n", mimetype="application/xml")


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

    # Catches a mistyped extra/missing digit or a non-mobile number before
    # it's stored -- normalize_phone() (used later in find_or_create_donor)
    # can only fix *recognised* formats (spaces, +91, leading 0), it can't
    # tell a typo from a genuinely unusual number, so this needs a separate
    # validity check. Phone is required on the form; whatsapp_number is
    # optional and only checked if the donor actually filled it in.
    if not is_valid_phone(data.get("phone")):
        return jsonify({"error": "That phone number doesn't look right. Please enter a 10-digit mobile number."}), 400
    if data.get("whatsapp_number") and not is_valid_phone(data.get("whatsapp_number")):
        return jsonify({"error": "That WhatsApp number doesn't look right. Please enter a 10-digit mobile number."}), 400

    high_value_error = high_value_pan_address_error(amount, pan, data.get("address"))
    if high_value_error:
        return jsonify({"error": high_value_error}), 400

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
                    # round(), not int(): float multiplication can land a
                    # hair under the intended paise value (e.g. 128.14 * 100
                    # == 12813.999999999998 in IEEE 754 float64), and int()
                    # truncates that down to the wrong paise amount --
                    # off-by-one-paisa from what the browser then passes to
                    # Razorpay's checkout widget (templates/*.html all do
                    # Math.round(order.amount * 100), which rounds
                    # correctly). That mismatch between the order Razorpay
                    # actually created and the amount checkout.js opens with
                    # is rejected client-side as a generic "Something went
                    # wrong" -- silently breaking any donation amount whose
                    # cents happen to round down under float imprecision
                    # (roughly 1 in 20 possible two-decimal amounts).
                    "amount": round(amount * 100),  # paise
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


def _send_receipt_notifications_background(app, donation_id, pdf_bytes):
    """Runs in a background thread -- see the comment in _finalize_success()
    for why. Needs its own app context (and its own DB session, which a
    fresh app context gives it) since it's not running inside the request
    that spawned it anymore. Sends both email and WhatsApp -- each is
    independently demo-mode-guarded and independently try/excepted, so one
    failing (or not being configured) never blocks the other."""
    with app.app_context():
        donation = Donation.query.get(donation_id)
        if donation is not None:
            try:
                send_receipt_email(donation, donation.donor, _org_cfg(), pdf_bytes)
            except Exception:
                app.logger.exception("Background receipt email failed for donation %s", donation_id)
            try:
                send_receipt_whatsapp(donation, donation.donor, _org_cfg(), pdf_bytes)
            except Exception:
                app.logger.exception("Background receipt WhatsApp send failed for donation %s", donation_id)


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

    # Email + WhatsApp in a background thread rather than blocking the
    # request on them. A slow/hanging SMTP connection or WhatsApp API call
    # stacking on top of PDF generation can blow past gunicorn's worker
    # timeout, which kills the whole worker mid-response -- not a clean
    # error, just the connection dropping outright. The receipt is already
    # saved to the database at this point regardless of whether either send
    # succeeds.
    #
    # Synchronous under TESTING so the test suite can assert on the send
    # deterministically instead of racing a background thread.
    app = current_app._get_current_object()
    if app.config.get("TESTING"):
        _send_receipt_notifications_background(app, donation.id, pdf_bytes)
    else:
        threading.Thread(
            target=_send_receipt_notifications_background, args=(app, donation.id, pdf_bytes), daemon=True
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

    if event_type in ("payment.captured", "order.paid"):
        return _handle_payment_captured(event)
    if event_type == "payment.failed":
        return _handle_payment_failed(event)
    if event_type and event_type.startswith("payment.dispute."):
        return _handle_payment_dispute(event, event_type)

    # Acknowledge (200) anything we don't act on -- payment.authorized,
    # the payment.downtime.* / order.notification.* infra events, etc. --
    # so Razorpay doesn't keep retrying an event we're deliberately
    # ignoring.
    return jsonify({"ok": True, "ignored": event_type}), 200


def _handle_payment_captured(event):
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


def _handle_payment_failed(event):
    """Marks a donation failed the moment Razorpay reports the payment
    itself failed, instead of waiting for the Dashboard's time-based
    "abandoned donation" heuristic (admin.dashboard) to eventually notice
    it's been sitting in "pending" too long. Only touches donations still
    "pending" -- if it somehow already finalized successfully (a captured
    event racing ahead of this one) or was already cancelled, this is a
    no-op rather than clobbering a more authoritative status."""
    payment_entity = event.get("payload", {}).get("payment", {}).get("entity", {})
    order_id = payment_entity.get("order_id")
    payment_id = payment_entity.get("id")

    if not order_id:
        return jsonify({"error": "Missing order_id in payload"}), 400

    donation = Donation.query.filter_by(razorpay_order_id=order_id).first()
    if donation is None:
        return jsonify({"ok": True, "matched": False}), 200

    if donation.status == "pending":
        donation.status = "failed"
        if payment_id:
            donation.razorpay_payment_id = payment_id
        donation.razorpay_status = payment_entity.get("status") or "failed"
        db.session.commit()

    return jsonify({"ok": True, "donation_id": donation.id}), 200


def _handle_payment_dispute(event, event_type):
    """Records a chargeback/dispute against the donation it applies to --
    doesn't change Donation.status (the payment itself was captured and
    the receipt already issued; a dispute is a separate, ongoing process
    layered on top, not an instant reversal). Surfaced on the admin
    Dashboard (see admin.dashboard's disputed_donations) so staff notice
    and can follow up -- Razorpay resolves the dispute on its own
    timeline (won/lost/closed), this just keeps the donation record in
    sync with whatever Razorpay's dashboard shows.
    """
    dispute_entity = event.get("payload", {}).get("dispute", {}).get("entity", {})
    payment_id = dispute_entity.get("payment_id") or (
        event.get("payload", {}).get("payment", {}).get("entity", {}).get("id")
    )

    if not payment_id:
        return jsonify({"error": "Missing payment_id in dispute payload"}), 400

    donation = Donation.query.filter_by(razorpay_payment_id=payment_id).first()
    if donation is None:
        return jsonify({"ok": True, "matched": False}), 200

    donation.razorpay_dispute_id = dispute_entity.get("id")
    # Razorpay's own status string (created/under_review/action_required/
    # won/lost/closed) -- kept verbatim rather than remapped to this app's
    # own vocabulary, since dispute-specific terminology is Razorpay's own
    # domain and staff will be cross-referencing this against Razorpay's
    # dashboard directly. Fall back to inferring one from the event name
    # itself (e.g. "payment.dispute.won" -> "won") if the payload doesn't
    # include a status field.
    donation.razorpay_dispute_status = dispute_entity.get("status") or event_type.rsplit(".", 1)[-1]
    donation.razorpay_dispute_reason = dispute_entity.get("reason_code") or dispute_entity.get("reason")
    if donation.disputed_at is None:
        donation.disputed_at = datetime.datetime.utcnow()
    db.session.commit()

    return jsonify({"ok": True, "donation_id": donation.id}), 200


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
