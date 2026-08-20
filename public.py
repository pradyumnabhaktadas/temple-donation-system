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
import functools
import hmac
import hashlib
import json
import io
import os
import re
import threading

from flask import (
    Blueprint, render_template, request, jsonify, redirect, url_for,
    send_file, current_app, flash, session, abort,
)
from flask_login import current_user
from werkzeug.exceptions import HTTPException

from extensions import db, csrf, limiter
from models import Donor, Campaign, Donation, ReceiptCounter, BaceProperty, Festival, SevaType, LiveToGivePurpose
from pdf_utils import generate_receipt_pdf, receipt_pdf_path
from email_utils import send_receipt_email
from whatsapp_utils import send_receipt_whatsapp
from utils import is_valid_pan, is_valid_phone, normalize_phone, receipt_access_token

bp = Blueprint("public", __name__)


def _safe_json_route(view_func):
    """Blanket safety net for every JSON API route in the payment flow.

    This session found the same bug three separate times in three
    different functions along this exact flow: an unanticipated
    exception (an over-length field, a PDF-generation edge case, a
    transient DB hiccup) propagating straight out of a view with nothing
    catching it, which presents to the donor's browser not as a clean
    error but as the connection simply dying mid-request -- a payment
    that may have actually succeeded looking, from the donor's side,
    indistinguishable from one that silently failed. Each occurrence got
    its own bespoke try/except added after the fact, once someone
    noticed.

    This decorator makes that fix structural instead of incidental: any
    view it wraps gets a final catch-all, so the *next* unanticipated
    exception -- wherever it turns out to be, in code that doesn't exist
    yet -- still gets a normal JSON 500 instead of a dropped connection.
    It's a last resort, not a replacement for handling specific,
    anticipated failure modes with their own status codes and messages
    (invalid input -> 400, Razorpay unreachable -> 502, etc.) -- those
    still live in the view functions themselves, closer to where they
    actually happen, so the donor gets the most specific and useful
    message available.

    HTTPException (get_or_404, Flask-Limiter's rate-limit response, etc.)
    is deliberately let through unmodified -- those are already Flask's
    own clean, intentional error responses, not the "something nobody
    anticipated" case this exists to catch.
    """
    @functools.wraps(view_func)
    def wrapped(*args, **kwargs):
        try:
            return view_func(*args, **kwargs)
        except HTTPException:
            raise
        except Exception:
            db.session.rollback()
            current_app.logger.exception("Unhandled error in %s", view_func.__name__)
            return jsonify({
                "error": "Something went wrong on our end. Please try again, or contact the "
                         "temple office if it keeps happening."
            }), 500
    return wrapped


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


# DB column widths (see models.Donor). None of the public donation forms
# enforce a client-side maxlength on Address/City/State, so a donor who
# pastes a long address hits sqlalchemy.exc.DataError the moment the
# session flushes an over-length value -- and until this fix, that
# exception had nowhere to go: it propagated straight out of every path
# that reaches find_or_create_donor() (online create_order, offline
# manual entry, bulk import, donor merge), none of which wrapped this
# call. Clipping here, at the one function all of them funnel through,
# fixes it once for every caller instead of requiring each call site to
# remember to sanitize first -- the same class of bug already found and
# fixed twice this session in _create_offline_donation and
# _finalize_success, just one level further upstream.
_DONOR_FIELD_LIMITS = {
    "full_name": 200, "email": 200, "pan": 10,
    "address": 400, "city": 100, "state": 100, "pincode": 10,
}


def _clip(value, limit):
    return (value or "")[:limit]


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
    pan = _clip((data.get("pan") or "").strip().upper(), _DONOR_FIELD_LIMITS["pan"])
    # Normalized to a plain 10-digit local number regardless of how it was
    # typed ("+91 88020 81265", "918802081265", "08802081265", ...) -- see
    # normalize_phone()'s docstring. Without this, the same donor typing
    # their number differently across two donations (or a donor logging in
    # with a different format than the one stored) would silently fail to
    # match.
    phone = normalize_phone(data.get("phone"))
    email = _clip((data.get("email") or "").strip().lower(), _DONOR_FIELD_LIMITS["email"])
    whatsapp_number = normalize_phone(data.get("whatsapp_number"))
    full_name = _clip((data.get("full_name") or "").strip(), _DONOR_FIELD_LIMITS["full_name"])
    incoming_name = _normalize_name(full_name)
    address = _clip((data.get("address") or "").strip(), _DONOR_FIELD_LIMITS["address"])
    city = _clip((data.get("city") or "").strip(), _DONOR_FIELD_LIMITS["city"])
    state = _clip((data.get("state") or "").strip(), _DONOR_FIELD_LIMITS["state"])
    pincode = _clip((data.get("pincode") or "").strip(), _DONOR_FIELD_LIMITS["pincode"])

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
            address=address or None,
            city=city or None,
            state=state or None,
            pincode=pincode or None,
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
        donor.address = address or donor.address
        donor.city = city or donor.city
        donor.state = state or donor.state
        donor.pincode = pincode or donor.pincode

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
@_safe_json_route
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
    # Per-campaign floor, admin-editable from Admin -> Campaigns -> Edit
    # (Campaign.min_amount). NULL means no floor beyond the check above.
    if campaign.min_amount and amount < float(campaign.min_amount):
        return jsonify({
            "error": f"Minimum contribution for {campaign.name} is Rs. {campaign.min_amount:.0f}."
        }), 400

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
    # 80G vs Non-80G receipt for this specific donation.
    receipt_type = data.get("receipt_type")
    if receipt_type == "80g":
        is_80g_requested = True
    elif receipt_type == "non80g":
        is_80g_requested = False
    elif live_to_give_purpose_id:
        # Live To Give donation, but no answer arrived (the donor didn't
        # pick either option, or reached this endpoint some other way
        # than the current form JS, which now defaults the "No" radio
        # itself). Defaulting to Non-80G here -- rather than falling
        # through to Campaign.is_80g below -- means a donor who has no
        # opinion on the tax-receipt question is never blocked or
        # surprised by whatever the campaign's own default happens to be
        # set to; they get a regular receipt unless they actively asked
        # for the 80G one.
        is_80g_requested = False
    else:
        # Every other campaign's 80G status is a fixed property of the
        # campaign itself and never sends receipt_type at all -- leave
        # this None so Donation.effective_is_80g falls back to
        # Campaign.is_80g exactly as it always has.
        is_80g_requested = None

    # A donation purpose's own 80G eligibility (LiveToGivePurpose.is_80g)
    # is a hard rule, not a donor choice -- only a fixed set of purposes
    # (Food for Life, Charity, Donation, Life Membership, Construction,
    # Annadan) actually qualify. Donation.effective_is_80g enforces this
    # regardless, but reject it here too rather than silently downgrading,
    # so a donor who explicitly asked for an 80G receipt on an ineligible
    # purpose finds out immediately instead of being surprised later.
    if live_to_give_purpose_id and is_80g_requested:
        purpose = LiveToGivePurpose.query.get(live_to_give_purpose_id)
        if purpose and not purpose.is_80g:
            return jsonify({
                "error": f'"{purpose.name}" isn\'t eligible for an 80G receipt. '
                         'Please select "No" for the 80G receipt question, or choose a different purpose.'
            }), 400

    remarks = (data.get("remarks") or "").strip()[:300] or None

    # Wrapped in try/except: find_or_create_donor()/Donation(...)/flush()
    # can still fail on something other than an over-length field (a
    # transient DB hiccup, for instance) -- without this, that exception
    # would propagate straight out of the request the same way the
    # now-fixed field-length crash used to, presenting to the donor's
    # browser as the connection simply dying instead of a normal error.
    try:
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
            # Not from Razorpay -- captured from the donor's own request to
            # our server, for the same fraud/audit-trail purpose.
            # request.remote_addr reflects the real client IP behind
            # Render's proxy because ProxyFix is wired up in app.py for
            # production.
            donor_ip_address=(request.remote_addr or "")[:45] or None,
            donor_user_agent=(request.headers.get("User-Agent") or "")[:300] or None,
        )
        db.session.add(donation)
        db.session.flush()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Failed to create donor/donation record for online donation")
        return jsonify({"error": "Something went wrong starting the payment. Please try again."}), 500

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

    Returns True once the donation has a receipt number (whether issued
    just now or by an earlier call) -- that's the only part a caller
    should treat as a hard failure if missing. PDF generation and
    notifications are best-effort layered on top: same crash risk this
    session already found and fixed for the offline-donation path
    (_create_offline_donation) -- an unhandled exception here used to
    propagate straight out of this function, past verify_payment/
    simulate_payment/the webhook handler with no try/except of their own,
    which can present to the donor's browser as the connection simply
    dying mid-request instead of a normal error response. Wrapped the
    same way here: the receipt number, once committed, is never lost or
    reissued even if PDF generation or notifications blow up afterward.

    Concurrency: the webhook and the browser's own verify-payment call
    routinely arrive for the *same* donation within milliseconds of each
    other in real usage -- Razorpay typically fires both close together.
    Without a lock, both could read the idempotency check below as "not
    yet finalized" before either commits, and both would proceed to call
    ReceiptCounter.next_receipt_number() for the same donation: not a
    duplicate receipt number (that's already guarded by the counter's own
    row lock -- see ReceiptCounter.next_receipt_number), but a wasted
    number and a lost update on this donation's own row, whichever commit
    lands second silently overwriting the first. with_for_update() below
    takes a row lock on this specific donation before the check, so a
    second concurrent caller blocks until the first one's transaction
    (ending at the commit a few lines down) finishes, then sees
    status == "success" already set and returns immediately instead of
    racing to issue a second number.
    """
    try:
        donation = Donation.query.filter_by(id=donation.id).with_for_update().one()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Failed to lock donation %s for finalization", donation.id)
        return False

    if donation.status == "cancelled":
        # An admin explicitly cancelled this donation (admin.cancel_donation
        # -- only reachable from an already-"success" donation, and it
        # deliberately leaves the original receipt_number in place rather
        # than clearing it, since receipts are immutable once issued).
        # That decision has to be sticky: Razorpay's webhook redelivery
        # schedule can span hours after the original payment (longer if
        # our server had any downtime), and without this check, a late
        # redelivery -- or, less likely, a very delayed browser-side
        # confirmation -- would silently un-cancel the donation and even
        # overwrite its original receipt number with a freshly issued
        # one, undoing whatever it was cancelled for. Returning True
        # (not False) is deliberate: this donation already has a real
        # receipt number on file from before it was cancelled, which is
        # what True is documented to mean here -- it just must never be
        # re-finalized once cancelled.
        return True

    if donation.status == "success" and donation.receipt_number:
        return True

    try:
        campaign = donation.campaign
        receipt_number, fy = ReceiptCounter.next_receipt_number(donation.effective_is_80g, donation.donation_date)
        donation.receipt_number = receipt_number
        donation.financial_year = fy
        donation.status = "success"
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Failed to issue receipt number for donation %s", donation.id)
        return False

    try:
        pdf_bytes = generate_receipt_pdf(donation, donation.donor, campaign, _org_cfg())
        donation.receipt_pdf = pdf_bytes
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Receipt PDF generation failed for donation %s", donation.id)
        return True

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
    try:
        if app.config.get("TESTING"):
            _send_receipt_notifications_background(app, donation.id, pdf_bytes)
        else:
            threading.Thread(
                target=_send_receipt_notifications_background, args=(app, donation.id, pdf_bytes), daemon=True
            ).start()
    except Exception:
        current_app.logger.exception("Failed to start receipt notification thread for donation %s", donation.id)

    return True


@bp.route("/api/verify-payment", methods=["POST"])
@limiter.limit("30 per hour")
@_safe_json_route
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

    # The signature below proves only that *some* genuine Razorpay payment
    # exists for the order_id/payment_id pair in this request -- it says
    # nothing about which donation that pair belongs to. Both values are
    # handed to the browser, and this endpoint is unauthenticated, so
    # without this check anyone could take a valid triple from a payment
    # they genuinely made (a Rs. 1 donation of their own) and re-post it
    # with somebody else's donation_id: the signature would verify, and
    # that donation would be finalized and issued a real receipt --
    # including an 80G tax receipt -- for money never paid against it.
    # Binding the request to this donation's own stored order id closes
    # that. The webhook path never needed this because it looks the
    # donation *up* by order_id rather than being told which one to use.
    if not donation.razorpay_order_id or not hmac.compare_digest(
        str(donation.razorpay_order_id), str(data["razorpay_order_id"])
    ):
        current_app.logger.warning(
            "verify-payment order_id mismatch for donation %s (sent %r, expected %r)",
            donation.id, data.get("razorpay_order_id"), donation.razorpay_order_id,
        )
        return jsonify({"error": "Payment details don't match this donation."}), 400

    if not _verify_checkout_signature(
        donation, data["razorpay_payment_id"], data.get("razorpay_signature", "")
    ):
        # Only a still-pending donation may be marked failed here. This
        # used to be unconditional, which meant an unauthenticated caller
        # sending a deliberately bad signature could flip *any* donation to
        # "failed" -- including one that had already succeeded and been
        # issued a receipt number, which cancel_donation() treats as
        # immutable. A donation that already reached a terminal state is
        # never downgraded by a failed verification attempt; the webhook
        # (layer 1) remains the authority on what actually happened.
        if donation.status == "pending":
            donation.status = "failed"
            db.session.commit()
        return jsonify({"error": "Signature verification failed"}), 400

    donation.razorpay_payment_id = data["razorpay_payment_id"]

    # A valid signature proves the payment is authentic -- not that the
    # money will actually settle. Razorpay's best-practices page is
    # explicit: "Check the payment/order status, that is if the payment's
    # status is captured and the order's status is paid, before providing
    # the services to the customers." An "authorized" payment has been
    # approved by the bank but not captured, and Razorpay auto-refunds
    # uncaptured payments after a fixed period -- so finalizing one here
    # would issue a receipt, and potentially an 80G tax certificate, for a
    # donation that quietly reverses later.
    #
    # The webhook path never had this problem (it only acts on
    # payment.captured / order.paid) and neither does the reconciliation
    # helper above; this was the one path that finalized on the signature
    # alone.
    #
    # Not fatal if the status can't be established: a failed lookup falls
    # through to finalizing, which is the behaviour this route has always
    # had. Refusing every receipt whenever Razorpay's API is briefly
    # unreachable would be the worse failure -- the signature has already
    # proven the payment is real, and the webhook will correct the record
    # either way.
    if not _payment_is_captured(data["razorpay_payment_id"]):
        db.session.commit()  # keep the payment id we just recorded
        return jsonify({
            "ok": False,
            "status": "pending",
            "error": "Payment received -- waiting for the bank to confirm it. Your receipt will "
                     "appear here automatically once that's done.",
        }), 202

    if not _finalize_success(donation):
        return jsonify({
            "error": "Payment verified, but we couldn't finish issuing the receipt. "
                     "Please check back in a minute or contact the temple office."
        }), 500
    # The signature check above already proved this caller is the browser
    # that made this exact payment, so it's safe to hand back the same
    # token /receipt/<id> requires -- the browser uses it to reach
    # /donate/success/<id> with proof of ownership instead of a bare id.
    # See donate_success()'s docstring-length comment for why that matters.
    return jsonify({
        "ok": True,
        "receipt_number": donation.receipt_number,
        "token": receipt_access_token(donation.id, current_app.config["SECRET_KEY"]),
    })


def _verify_checkout_signature(donation, payment_id, signature):
    """Shared by the browser fast path and the redirect callback below.

    Note which order id goes into the payload: the one *we* stored when we
    created the order, never one supplied by the caller. Razorpay's guide
    is explicit about this ("Retrieve the order_id from your server. Do not
    use the razorpay_order_id returned by Checkout") -- otherwise a valid
    signature from any other payment could be replayed against a different
    donation.
    """
    key_secret = current_app.config["RAZORPAY_KEY_SECRET"]
    payload = f"{donation.razorpay_order_id}|{payment_id}"
    expected = hmac.new(key_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


@bp.route("/api/payment-callback", methods=["POST"])
@csrf.exempt
@limiter.limit("60 per hour")
def payment_callback():
    """Redirect-flow counterpart to /api/verify-payment.

    Razorpay's checkout renders in an iframe, and some in-app browsers --
    Instagram, Facebook Messenger, Opera, UC Browser -- don't support
    iframes at all, so checkout simply never opens there. Razorpay's fix
    is `callback_url` + `redirect: true`: instead of calling the handler
    in our page, they take over the whole tab and POST the result back
    here as a normal form submission. Only those browsers get this flow
    (see needsRedirectFlow() in donation-payment.js); everywhere else
    keeps the handler, which Razorpay recommends for standard web.

    CSRF-exempt because this is a cross-origin form POST from Razorpay,
    with no session or token of ours. The signature check is the
    authentication, exactly as on the webhook.

    Not wrapped in _safe_json_route: a donor's browser lands here, so
    every exit has to be a page they can look at, never JSON.
    """
    order_id = (request.form.get("razorpay_order_id") or "").strip()
    payment_id = (request.form.get("razorpay_payment_id") or "").strip()
    signature = (request.form.get("razorpay_signature") or "").strip()

    # Looked up *by* order id rather than being told which donation this
    # is -- same as the webhook, and inherently immune to the replay this
    # binding exists to prevent.
    donation = Donation.query.filter_by(razorpay_order_id=order_id).first() if order_id else None
    if donation is None:
        current_app.logger.warning("payment-callback for unknown order %r", order_id)
        flash("We couldn't match that payment to a donation. Please contact the temple office.")
        return redirect(url_for("public.donate_form"))

    try:
        if not payment_id or not _verify_checkout_signature(donation, payment_id, signature):
            current_app.logger.warning(
                "payment-callback signature check failed for donation %s", donation.id
            )
            if donation.status == "pending":
                donation.status = "failed"
                db.session.commit()
            flash("We couldn't verify that payment. If money was deducted, please contact the temple office.")
            return redirect(url_for("public.donate_form"))

        donation.razorpay_payment_id = payment_id

        # The signature check above already proved this request is the tail
        # end of a real payment for this exact donation, so every redirect
        # to donate_success from here on carries the same proof-of-ownership
        # token /receipt/<id> and donate_success() now require -- this flow
        # has no session/cookie of its own (see the docstring) to prove it
        # any other way.
        success_token = receipt_access_token(donation.id, current_app.config["SECRET_KEY"])

        # Same rule as everywhere else: authorized isn't good enough, since
        # uncaptured payments are auto-refunded and the receipt would end up
        # certifying a donation that reversed.
        if not _payment_is_captured(payment_id):
            db.session.commit()
            return redirect(url_for("public.donate_success", donation_id=donation.id, t=success_token))

        _finalize_success(donation)
    except Exception:
        # The donor is mid-redirect with money already taken. The webhook
        # will still finalize this independently, so send them to the
        # status page rather than an error page -- it shows the receipt
        # once confirmation lands.
        db.session.rollback()
        current_app.logger.exception("payment-callback failed for donation %s", donation.id)
        return redirect(url_for("public.donate_success", donation_id=donation.id,
                                 t=receipt_access_token(donation.id, current_app.config["SECRET_KEY"])))

    return redirect(url_for("public.donate_success", donation_id=donation.id, t=success_token))


@bp.route("/api/donation-status/<int:donation_id>", methods=["GET"])
@limiter.limit("120 per minute")
@_safe_json_route
def donation_status(donation_id):
    """Client polling target -- see module docstring, layer 3. Doesn't
    confirm anything itself; just reports whatever the webhook or the
    browser fast path has already recorded, so a donor's tab finds out
    even when the fast path never fires.

    Rate limit is keyed by IP (see extensions.py), and this is a cheap,
    read-only, unauthenticated lookup -- raised from 60/min to 120/min
    because the client-side polling was made more aggressive this session
    (a Page Visibility check fires an extra request the instant a
    backgrounded tab comes back, on top of the regular interval), and
    many donors on the same mobile carrier's shared IP (common in India)
    polling concurrently could otherwise collide with each other's
    budget and see checks silently fail more than necessary.

    ?verify=1 additionally asks Razorpay directly -- see
    _reconcile_pending_with_razorpay(). Razorpay's own integration guide
    recommends exactly this: rely on webhooks for automation, and "if a
    critical user-facing flow requires instant status, but the webhook
    notification has not arrived within the time mandated by your business
    needs, perform an immediate API Fetch call to verify the status." The
    client only sets it when the ordinary poll has run out of patience or
    the donor pressed "Check again", so the extra API call is bounded by
    those events rather than fired on every 3-second tick."""
    donation = Donation.query.get_or_404(donation_id)

    if request.args.get("verify") == "1" and donation.status == "pending":
        _reconcile_pending_with_razorpay(donation)

    return jsonify({"status": donation.status, "receipt_number": donation.receipt_number})


def _payment_is_captured(payment_id):
    """True if Razorpay says this payment is captured -- i.e. the money is
    actually ours and won't be auto-refunded.

    Returns True rather than False when the answer can't be established
    (Razorpay unreachable, keys not configured, unexpected payload). This
    is a deliberate fail-open: the only caller has already verified the
    payment's signature, so the payment is known to be genuine, and
    blocking every receipt during a transient Razorpay API outage would be
    a worse failure than briefly trusting a signature the way this code
    always used to. The webhook remains the authority either way.
    """
    if not current_app.config.get("RAZORPAY_ENABLED"):
        return True

    try:
        import razorpay

        client = razorpay.Client(
            auth=(current_app.config["RAZORPAY_KEY_ID"], current_app.config["RAZORPAY_KEY_SECRET"])
        )
        payment = client.payment.fetch(payment_id) or {}
    except Exception:
        current_app.logger.exception(
            "Could not fetch payment %s to confirm capture; trusting the verified "
            "signature instead", payment_id,
        )
        return True

    status = payment.get("status")
    if status == "captured":
        return True

    current_app.logger.info(
        "Payment %s is %r, not captured -- holding the receipt until it is. If this "
        "keeps happening, check auto-capture under Razorpay Dashboard -> Account & "
        "Settings -> Payment Capture.", payment_id, status,
    )
    return False


def _reconcile_pending_with_razorpay(donation):
    """Last-resort truth check for a donation still sitting at "pending".

    Everything else in this flow waits to be *told* a payment succeeded:
    the webhook is Razorpay calling us, and the browser fast path is the
    donor's tab calling us. When both are lost -- webhook delayed or
    misdelivered, and the tab backgrounded through a UPI hand-off or
    closed outright -- nothing else ever asks Razorpay, who has known the
    answer the whole time. That gap is what produced donors being shown
    "we could not confirm your payment" for donations that had in fact
    succeeded (confirmed more than once in Admin -> Donations Log).

    So: ask. If Razorpay reports a captured payment against this
    donation's own order, finalize it exactly as the webhook would have.

    Deliberately quiet on failure -- this is a best-effort improvement on
    top of three existing paths, and the caller only wants a status back.
    Never raises.
    """
    if not current_app.config.get("RAZORPAY_ENABLED") or not donation.razorpay_order_id:
        return

    try:
        import razorpay

        client = razorpay.Client(
            auth=(current_app.config["RAZORPAY_KEY_ID"], current_app.config["RAZORPAY_KEY_SECRET"])
        )
        payments = client.order.payments(donation.razorpay_order_id) or {}
    except Exception:
        current_app.logger.exception(
            "Razorpay status reconciliation failed for donation %s", donation.id
        )
        return

    # "captured" is the only state that means the money is actually ours.
    # "authorized" is approved-but-not-settled and is auto-refunded if it
    # is never captured, so issuing an 80G receipt against one would risk
    # certifying a donation that later reverses. Accounts with auto-capture
    # on (Razorpay's own recommendation) move straight to captured anyway.
    captured = next(
        (p for p in (payments.get("items") or []) if p.get("status") == "captured"),
        None,
    )
    if not captured:
        return

    current_app.logger.info(
        "Reconciled donation %s from Razorpay (payment %s) -- neither the webhook "
        "nor the browser had reported it", donation.id, captured.get("id"),
    )
    if captured.get("id"):
        donation.razorpay_payment_id = captured["id"]
    _apply_payment_details(donation, captured)
    _finalize_success(donation)


@bp.route("/api/simulate-payment", methods=["POST"])
@limiter.limit("30 per hour")
@_safe_json_route
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
    if not _finalize_success(donation):
        return jsonify({"error": "Couldn't finish issuing the receipt. Please try again."}), 500
    return jsonify({
        "ok": True,
        "receipt_number": donation.receipt_number,
        "token": receipt_access_token(donation.id, current_app.config["SECRET_KEY"]),
    })


@bp.route("/webhooks/razorpay", methods=["POST"])
@csrf.exempt
@_safe_json_route
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
    if not _finalize_success(donation):
        # Returning non-200 tells Razorpay to retry this webhook delivery
        # on its normal backoff schedule, rather than us silently
        # swallowing a failure that means no receipt number was ever
        # issued for a captured payment.
        return jsonify({"error": "Failed to finalize donation"}), 500

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
    # Donation ids are sequential and this route takes no other input, so
    # without a gate here anyone can page through ids and read another
    # donor's amount/campaign -- and, worse, the page used to embed a live
    # receipt_token() download link for whoever it belonged to (QA report
    # REG-026/REG-056: the /receipt/<id> route itself is correctly token-
    # gated, but this page was handing that same token to anyone who asked,
    # unauthenticated, undoing the point of gating the other route at all).
    #
    # _may_download_receipt() is the same check /receipt/<id> already uses.
    # The donor's own browser reaches this page in one of two ways that can
    # legitimately carry that authorization forward without asking them to
    # log in seconds after paying: payment_callback() and /api/verify-
    # payment both redirect/report back only after checking this donation's
    # Razorpay signature, and both now append the same token to the URL --
    # see the two call sites below. A donation confirmed purely by the
    # webhook + polling (no signed round trip in this browser) has no way
    # to prove it's the paying donor, so it falls back to a generic page;
    # the donor already has the PDF by email/WhatsApp either way.
    may_view = _may_download_receipt(donation)
    return render_template("donate_success.html", donation=donation, may_view=may_view)


def _may_download_receipt(donation):
    """Who is allowed to fetch a receipt PDF.

    The PDF contains the donor's full name, address, PAN, email and phone.
    Donation ids are sequential, so this route has to prove the requester
    is entitled to *this* receipt rather than just able to count.

    Three legitimate routes in, in order of how often they're used:

    1. A signed token in the URL (see utils.receipt_access_token) -- how
       donors reach their own receipt straight after paying, and how
       Airtel fetches the PDF to attach to a WhatsApp message. Compared
       with compare_digest to keep the check constant-time.
    2. A logged-in admin, who can already see every donation in the admin
       area anyway.
    3. A donor logged into the donor portal, for their own donations only.
    """
    token = request.args.get("t") or ""
    expected = receipt_access_token(donation.id, current_app.config["SECRET_KEY"])
    if token and hmac.compare_digest(token, expected):
        return True

    if current_user.is_authenticated:
        return True

    return session.get("donor_id") == donation.donor_id


@bp.route("/receipt/<int:donation_id>")
def download_receipt(donation_id):
    donation = Donation.query.get_or_404(donation_id)

    if not _may_download_receipt(donation):
        # Deliberately the same 404 an unknown id gets: distinguishing
        # "wrong token" from "no such donation" would confirm which ids
        # exist, which is most of what an enumeration attempt wants.
        abort(404)

    if donation.status != "success" or not donation.receipt_number:
        flash("Receipt not available for this donation.")
        return redirect(url_for("public.donate_form"))

    pdf_bytes = donation.receipt_pdf

    if not pdf_bytes:
        # Legacy fallback: donations issued before receipts moved into the
        # database (see README "Receipt storage") were written to disk
        # instead. Read from there if it's still around, rather than
        # 404ing on a receipt that was genuinely issued.
        legacy_path = receipt_pdf_path(donation.receipt_number)
        if os.path.isfile(legacy_path):
            with open(legacy_path, "rb") as f:
                pdf_bytes = f.read()

    if not pdf_bytes:
        # No stored PDF and no legacy file, but the donation definitely
        # succeeded and definitely has a receipt number (checked above).
        # That combination is reachable: _finalize_success() deliberately
        # treats PDF generation as best-effort and returns success even if
        # it fails, precisely so that a PDF problem can never cost a donor
        # their already-committed receipt number. The cost of that choice
        # used to land here -- a dead end telling the donor to contact the
        # office about a receipt they're entitled to and that we hold all
        # the data for.
        #
        # Nothing about the receipt depends on when it's rendered (it's
        # built entirely from stored donation/donor/campaign data, and the
        # receipt number itself was fixed at finalization), so just build
        # it now and keep it for next time.
        try:
            pdf_bytes = generate_receipt_pdf(donation, donation.donor, donation.campaign, _org_cfg())
            donation.receipt_pdf = pdf_bytes
            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception(
                "On-demand receipt regeneration failed for donation %s", donation.id
            )
            flash("This receipt needs to be regenerated -- please contact the office.")
            return redirect(url_for("public.donate_form"))

    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"{donation.receipt_number.replace('/', '_')}.pdf",
    )
