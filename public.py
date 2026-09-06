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
from models import (
    Donor, Campaign, Donation, ReceiptCounter, BaceProperty, Festival, SevaType, LiveToGivePurpose,
    AssociatedWith, AdminActivityLog, PendingZohoSubmission,
)
from pdf_utils import generate_receipt_pdf, receipt_pdf_path
from email_utils import send_receipt_email
from whatsapp_utils import send_receipt_whatsapp
from utils import (
    HIGH_VALUE_PAN_THRESHOLD, is_valid_pan, is_valid_phone, normalize_phone, now_ist, receipt_access_token, retry,
    to_ist,
)

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
    "live_to_give_purpose_id": "donation purpose", "associated_with_id": "associated-with option",
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
    associated_withs = AssociatedWith.query.filter_by(is_active=True).order_by(
        AssociatedWith.display_order, AssociatedWith.name
    ).all()
    return render_template(
        "donate.html",
        campaign=campaign,
        purposes=purposes,
        associated_withs=associated_withs,
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
    associated_withs = AssociatedWith.query.filter_by(is_active=True).order_by(
        AssociatedWith.display_order, AssociatedWith.name
    ).all()
    return render_template(
        "festival_seva.html",
        campaign=campaign,
        festivals=festivals,
        seva_types=seva_types,
        associated_withs=associated_withs,
        razorpay_enabled=current_app.config["RAZORPAY_ENABLED"],
        razorpay_key_id=current_app.config["RAZORPAY_KEY_ID"],
        org_name=current_app.config["ORG_NAME"],
    )


@bp.route("/dhoti-kurta-contribution")
def dhoti_kurta_form():
    """Deliberately not linked from the main nav, homepage, or any of the
    other donation sections -- only reachable via the small link in the
    site footer (see base.html). A minimal Name/Mobile/Amount form,
    intentionally lighter than every other donation form here: no PAN/
    address/consent fields, no purpose picker, no email.

    Fixed to the "Dhoti Kurta Contribution" campaign, which is seeded
    with suppress_receipt=True -- no receipt number, PDF, or email/
    WhatsApp is ever generated for a donation against it (see
    _finalize_success). The amount is capped at HIGH_VALUE_PAN_THRESHOLD
    (Rs. 49,000): above that, Income Tax rules require PAN and address
    regardless of campaign (see high_value_pan_address_error), which
    this form has nowhere to collect -- someone wanting to give more than
    that should use the main Give to Krishna form instead.
    """
    campaign = Campaign.query.filter_by(name="Dhoti Kurta Contribution").first()
    if campaign is None or not campaign.is_active:
        flash("The Dhoti Kurta Contribution campaign isn't set up yet -- please contact the office.")
        return redirect(url_for("public.donate_form"))

    return render_template(
        "dhoti_kurta.html",
        campaign=campaign,
        high_value_threshold=HIGH_VALUE_PAN_THRESHOLD,
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
        return jsonify({"error": "That phone number doesn't look right. Please enter a 10-digit mobile number, or a foreign number starting with + and country code."}), 400
    if data.get("whatsapp_number") and not is_valid_phone(data.get("whatsapp_number")):
        return jsonify({"error": "That WhatsApp number doesn't look right. Please enter a 10-digit mobile number, or a foreign number starting with + and country code."}), 400

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
        # "I am associated with" -- offered on Give to Krishna (Live To
        # Give) and Festival Seva, but validated here unconditionally like
        # every other optional FK above: which forms a client claims to be
        # submitting from is incidental, not something to trust. Entirely
        # independent of campaign/purpose -- see AssociatedWith's
        # docstring -- so no campaign-specific gating applies to it here.
        associated_with_id = _validated_fk_id(data, "associated_with_id", AssociatedWith)
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

    # Mirrors Donation.effective_is_80g -- computed here, before the
    # Donation row exists, because a PAN missing at intake can't be added
    # after the fact without contacting the donor. The purpose-eligibility
    # check just above already means is_80g_requested can't be True here
    # against an ineligible purpose, so this is safe to compute directly
    # rather than re-deriving the purpose override too.
    effective_is_80g = is_80g_requested if is_80g_requested is not None else campaign.is_80g
    if effective_is_80g and not pan:
        # high_value_pan_address_error() above only requires PAN past the
        # Rs. 49,000 income-tax threshold -- this is the separate,
        # lower-value gap QA report REG-036 found: a donation recorded as
        # 80G-eligible (either because the donor picked "Yes" on Live To
        # Give, or because the campaign itself is fixed 80G) with no PAN on
        # file is exactly the record Form 10BD can't actually report. The
        # donate.html field label has always said "PAN (required for 80G
        # receipt)" -- this makes the server actually enforce that.
        return jsonify({
            "error": "A PAN is required to issue an 80G tax receipt for this donation. Please enter "
                     "your PAN, or select \"No\" for the 80G receipt question if you don't need one."
                     if live_to_give_purpose_id else
                     "A PAN is required to issue an 80G tax receipt for this donation. Please enter your PAN."
        }), 400

    remarks = (data.get("remarks") or "").strip()[:300] or None

    # Wrapped in try/except: find_or_create_donor()/Donation(...)/flush()
    # can still fail on something other than an over-length field (a
    # transient DB hiccup, for instance) -- without this, that exception
    # would propagate straight out of the request the same way the
    # now-fixed field-length crash used to, presenting to the donor's
    # browser as the connection simply dying instead of a normal error.
    # REG-001 (QA report): the donate.html opt-out toggle now excludes the
    # PAN/address fields from FormData once "No" is selected (they're
    # disabled, not just hidden) -- but that's a client-side fix, and the
    # report explicitly calls for a server-side backstop too: "verify
    # server-side that a non-80G donation record never stores a PAN even
    # if one arrives in the request." A PAN is only ever legitimately
    # needed here for one of two already-enforced reasons -- this donation
    # is 80G (checked above) or it's above the high-value reporting
    # threshold (checked in high_value_pan_address_error above). If
    # neither applies, any PAN that still arrived (stale form state, a
    # non-JS client, a hand-crafted request) is spurious and must not be
    # written to the donor's shared profile.
    donor_data = data
    if not (effective_is_80g or amount > HIGH_VALUE_PAN_THRESHOLD):
        donor_data = dict(data)
        donor_data["pan"] = ""

    try:
        donor = find_or_create_donor(donor_data)

        # REG-032 (QA report): two identical create-order requests fired
        # back-to-back (a genuine double-click, or the pay button
        # re-enabling on modal.ondismiss/payment.failed and the donor
        # retrying) used to mint two separate donations and two separate
        # Razorpay orders for what was really one donation intent. Rather
        # than a client-side-only guard (which the report's own PAY-12
        # case showed doesn't help a non-browser client, or two genuinely
        # concurrent requests), reuse an existing pending donation for the
        # same (donor, campaign, amount, purpose/property/festival/seva)
        # if one was created in the last few minutes and -- for a live
        # Razorpay payment -- already has an order attached, rather than
        # creating a second one. A short window on purpose: long enough to
        # absorb a rapid resubmit, short enough that a donor who
        # deliberately makes two separate donations of the same amount to
        # the same campaign minutes apart still gets two donations.
        dedup_cutoff = datetime.datetime.utcnow() - datetime.timedelta(minutes=5)
        existing_donation = Donation.query.filter(
            Donation.donor_id == donor.id,
            Donation.campaign_id == campaign.id,
            Donation.amount == amount,
            Donation.payment_mode == "online",
            Donation.status == "pending",
            Donation.bace_property_id == bace_property_id,
            Donation.festival_id == festival_id,
            Donation.seva_type_id == seva_type_id,
            Donation.live_to_give_purpose_id == live_to_give_purpose_id,
            Donation.associated_with_id == associated_with_id,
            Donation.donation_date >= dedup_cutoff,
        ).order_by(Donation.id.desc()).first()
        if existing_donation and (
            not current_app.config["RAZORPAY_ENABLED"] or existing_donation.razorpay_order_id
        ):
            return jsonify({
                "donation_id": existing_donation.id,
                "order_id": existing_donation.razorpay_order_id,
                "amount": amount,
                "razorpay_enabled": current_app.config["RAZORPAY_ENABLED"],
                "key_id": current_app.config["RAZORPAY_KEY_ID"],
            })

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
            associated_with_id=associated_with_id,
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
        except razorpay.errors.BadRequestError as e:
            # QA report REG-033: an out-of-range amount (Razorpay rejects
            # past its own ceiling) used to surface as a generic 502
            # "gateway" error -- the donor's own input was the actual
            # problem, not our connection to Razorpay, so this is a 400
            # like any other validation failure. BadRequestError is
            # specifically what the SDK raises for Razorpay's 4xx
            # responses (bad amount, bad currency, ...) -- a real outage
            # or misconfigured key raises something else and still falls
            # through to the 502 branch below. Either way, the rollback
            # (shared with that branch) means the pending donation row
            # from the try block above never survives a failed order
            # call -- confirmed by test_zzz_orphan_check-style coverage,
            # not just assumed.
            db.session.rollback()
            current_app.logger.warning("Razorpay rejected the order request: %s", e)
            return jsonify({
                "error": "That amount couldn't be processed -- please try a smaller amount, "
                         "or contact the temple office to arrange a large donation directly."
            }), 400
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

    if campaign.suppress_receipt:
        # Dhoti Kurta Contribution (and anything else marked this way): a
        # real receipt number and PDF were just issued above, same as any
        # other donation -- that part is unchanged, and it's what lets the
        # temple reconcile these internally. What's suppressed is only the
        # *proactive* send: no email, no WhatsApp goes out on its own. The
        # donor can still see/download it the normal way (this success
        # page, the donor portal) -- see _may_download_receipt's
        # docstring. Skipping straight to `return True` here means the
        # background email/WhatsApp thread below never starts.
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


@bp.route("/internal/zoho-reconcile", methods=["POST"])
@csrf.exempt
@_safe_json_route
def internal_zoho_reconcile():
    """Runs reconcile_zoho_submissions() -- the job that issues receipts
    for Zoho Forms payments Zoho never sent us a confirmation for. Meant
    to be called on a schedule (hourly; see render.yaml's
    "temple-zoho-reconcile" Cron Job), and safe to call by hand at any
    time: it only ever acts on submissions that are still unresolved and
    payments not already attached to a donation, so running it twice in a
    row does nothing the second time.

    Same server-to-server auth story as internal_daily_report_send above:
    no browser or cookie involved, so nothing to CSRF-protect;
    INTERNAL_TASK_TOKEN compared with hmac.compare_digest instead."""
    expected_token = current_app.config.get("INTERNAL_TASK_TOKEN")
    if not expected_token:
        return jsonify({"error": "INTERNAL_TASK_TOKEN not configured"}), 503

    provided_token = request.headers.get("X-Internal-Token", "")
    if not hmac.compare_digest(provided_token, expected_token):
        return jsonify({"error": "Unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    summary = reconcile_zoho_submissions(
        current_app.config,
        lookback_days=int(payload.get("lookback_days") or 3),
    )

    if summary["error"]:
        # 502, not 200: an unreachable Razorpay means nothing was checked,
        # and a scheduled job that reports success on a scan it never
        # performed is exactly the kind of silent failure this whole
        # feature exists to eliminate.
        return jsonify({"error": summary["error"]}), 502

    return jsonify({
        "created": [
            {"donation_id": d.id, "receipt_number": d.receipt_number, "amount": d.amount}
            for d in summary["created"]
        ],
        "ambiguous": [
            {"pending_id": s.id, "name": s.full_name, "amount": s.amount, "note": s.note}
            for s in summary["ambiguous"]
        ],
        "unpaid": summary["unpaid"],
        "still_waiting": summary["still_waiting"],
        "failed": summary["failed"],
        "orphan_payments": [
            {"payment_id": p["payment_id"], "amount": p["amount"], "contact": p["contact"]}
            for p in summary["orphan_payments"]
        ],
    })


@bp.route("/internal/daily-report/send", methods=["POST"])
@csrf.exempt
@_safe_json_route
def internal_daily_report_send():
    """Runs the 4 AM daily collection report's actual send (see
    daily_report_utils.send_report) from inside this always-on web app,
    triggered by daily_report.py over HTTPS instead of running in the
    separate Cron Job container.

    Why: every automatic run of that Cron Job failed to deliver both
    email and WhatsApp (2026-08-29, 08-30, 08-31), while manual re-runs
    -- and every donor-facing receipt this same web app sends over email/
    WhatsApp, all day, every day -- succeed without issue. Adding retries
    inside the send functions (utils.retry()) didn't change that, which
    rules out a code bug in the sends themselves and points at the Cron
    Job container's own networking being unreliable on cold start,
    distinct from this process. Moving the actual SMTP/WhatsApp network
    calls here sidesteps whatever that difference is, rather than trying
    to diagnose an environment this app has no visibility into.

    No browser/cookie is involved in this server-to-server call, so
    there's nothing to CSRF-protect -- same reasoning as the Razorpay
    webhook above. Authenticated instead via INTERNAL_TASK_TOKEN, a
    shared secret compared with hmac.compare_digest (not a plain ==,
    which would leak timing information about how many leading
    characters matched).
    """
    expected_token = current_app.config.get("INTERNAL_TASK_TOKEN")
    if not expected_token:
        return jsonify({"error": "INTERNAL_TASK_TOKEN not configured"}), 503

    provided_token = request.headers.get("X-Internal-Token", "")
    if not hmac.compare_digest(provided_token, expected_token):
        return jsonify({"error": "Unauthorized"}), 401

    payload = request.get_json(silent=True) or {}

    report_date = None
    if payload.get("date"):
        try:
            report_date = datetime.datetime.strptime(payload["date"], "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"error": "Invalid date, expected YYYY-MM-DD"}), 400

    from daily_report_utils import send_report

    result = send_report(current_app._get_current_object(), report_date=report_date, force=bool(payload.get("force")))

    if result.get("skipped"):
        return jsonify({"skipped": result["skipped"], "report_date": result["report_date"].isoformat()})

    return jsonify({
        "report_date": result["report_date"].isoformat(),
        "data": {
            "today": result["data"]["today"],
            "week": result["data"]["week"],
            "month": result["data"]["month"],
        },
        "email_sent": result["email_sent"],
        "email_recipients_count": len(result["email_recipients"]),
        "email_error": result["email_error"],
        "whatsapp_sent_count": result["whatsapp_sent_count"],
        "whatsapp_recipients_count": len(result["whatsapp_recipients"]),
        "whatsapp_error": result["whatsapp_error"],
    })


def _zoho_payment_is_captured(payment_id):
    """Returns (captured: bool, error: str|None). The Zoho webhook's only
    proof a payment is genuine and captured -- unlike _payment_is_captured
    above (used by the site's own flow, where the caller has *already*
    verified a Razorpay signature and this only refines "captured vs.
    merely authorized"), so this fails closed rather than open: an error
    here is reported as an error to the caller (which turns into a
    non-2xx response Zoho logs as a failed webhook delivery, re-pushable
    once Razorpay is reachable again), never silently treated as success.

    retry()'d (same helper daily_report_utils's email/WhatsApp sends use)
    since this runs synchronously inside the webhook request -- a single
    transient network hiccup talking to Razorpay shouldn't be the
    difference between a real donation getting its receipt or not."""
    if not current_app.config.get("RAZORPAY_ENABLED"):
        return False, "Razorpay isn't configured on this deployment"

    try:
        import razorpay

        def _fetch():
            client = razorpay.Client(
                auth=(current_app.config["RAZORPAY_KEY_ID"], current_app.config["RAZORPAY_KEY_SECRET"])
            )
            return client.payment.fetch(payment_id)

        payment = retry(_fetch, attempts=3, delay_seconds=2) or {}
    except Exception as exc:
        current_app.logger.exception("Could not fetch payment %s from Razorpay for Zoho webhook", payment_id)
        return False, str(exc)

    return payment.get("status") == "captured", None


def unreconciled_razorpay_payments(config, lookback_days=3):
    """Returns (payments, error) -- every Razorpay payment captured in the
    trailing `lookback_days` that has no matching Donation anywhere in this
    app, neither via this site's own checkout flow nor the Zoho Forms
    webhook. This is the actual safety net behind
    zoho_form_donation_webhook()/_zoho_payment_is_captured: those only ever
    run when Zoho's webhook calls this app at all, carrying a transaction
    ID for this route to check. On this live account, a submission's first
    (and often only) call routinely carries no transaction ID yet -- the
    normal shape of Zoho's call before a payment finishes -- and more than
    once, no follow-up call has ever arrived once it did. When that
    happens, nothing in this app ever runs again for that payment, and the
    donation is otherwise lost with no trace anywhere in this app, no
    matter how the webhook route itself is written.

    Razorpay is the one party in this whole chain that never fails to
    record a captured payment, so reconciling against it directly --
    independent of whether Zoho or this app's webhook route ever gets
    involved correctly -- is what makes a gap like that visible within a
    day (via the daily report) instead of only being discovered whenever
    someone happens to notice a stale "Webhook Status" cell in Zoho's own
    Reports grid.

    Each returned dict: payment_id/order_id/amount/email/contact/
    created_at (IST)/notes. Deliberately doesn't create a Donation itself
    -- there's no campaign or donor-consent data to attach here, only what
    Razorpay itself recorded. Staff match a flagged payment to its real
    Zoho submission (or other source) and finish the job via the existing
    Offline Donation -> Single Entry form, same as every backfill this app
    has needed so far.

    error is None on success -- including when Razorpay isn't configured
    at all, which returns ([], None): nothing to reconcile against isn't a
    failure. A real API error returns ([], the error string) so the caller
    (the daily report) can say the scan itself failed rather than silently
    imply a clean one."""
    if not config.get("RAZORPAY_ENABLED"):
        return [], None

    try:
        import razorpay

        client = razorpay.Client(auth=(config["RAZORPAY_KEY_ID"], config["RAZORPAY_KEY_SECRET"]))
        # Deliberately utcnow(), not now_ist(): now_ist() returns a *naive*
        # datetime holding IST wall-clock time, and .timestamp() on a naive
        # datetime makes Python interpret it as the host's local zone (UTC
        # on Render). Feeding now_ist() in here therefore reads as an
        # instant 5h30m in the future and quietly shortens the scan window
        # to 2 days 18.5 hours -- payments just outside it would never be
        # looked at. Razorpay's from/to are absolute unix timestamps, so
        # the right input is an absolute UTC now.
        from_ts = int(
            (datetime.datetime.utcnow() - datetime.timedelta(days=lookback_days))
            .replace(tzinfo=datetime.timezone.utc).timestamp()
        )

        captured = []
        skip = 0
        page_size = 100
        # Safety cap: 10 pages (1000 payments) is far beyond what a few
        # days of a single temple's traffic could ever produce -- a bug in
        # the pagination below then fails loud (an incomplete scan next to
        # a suspiciously round 1000) rather than looping forever.
        for _ in range(10):
            page = retry(
                lambda: client.payment.all({"from": from_ts, "count": page_size, "skip": skip}),
                attempts=3, delay_seconds=2,
            ) or {}
            items = page.get("items", [])
            captured.extend(item for item in items if item.get("status") == "captured")
            if len(items) < page_size:
                break
            skip += page_size
    except Exception as exc:
        current_app.logger.exception("Razorpay reconciliation scan failed")
        return [], str(exc)

    known_ids = {
        pid for (pid,) in db.session.query(Donation.razorpay_payment_id)
        .filter(Donation.razorpay_payment_id.isnot(None)).all()
    }

    unreconciled = [
        {
            "payment_id": item["id"],
            "order_id": item.get("order_id"),
            "amount": (item.get("amount") or 0) / 100,
            "email": item.get("email") or None,
            "contact": item.get("contact") or None,
            # Both frames kept deliberately: created_at (IST) is what the
            # daily report and admin page display, created_at_utc is what
            # reconcile_zoho_submissions() compares against the naive-UTC
            # received_at it has stored, with no conversion at the comparison.
            "created_at": to_ist(datetime.datetime.utcfromtimestamp(item["created_at"])),
            "created_at_utc": datetime.datetime.utcfromtimestamp(item["created_at"]),
            "notes": item.get("notes") or {},
        }
        for item in captured if item["id"] not in known_ids
    ]
    unreconciled.sort(key=lambda p: p["created_at"], reverse=True)
    return unreconciled, None


def reconcile_zoho_submissions(config, lookback_days=3, min_age_minutes=15, max_age_hours=48,
                               max_per_run=25):
    """Issues the receipts Zoho never told us to issue.

    This is the fix for the failure that kept recurring: Zoho Forms fires
    its webhook once, at submission time, *before* the donor has paid --
    so that call has the whole form but no transaction ID -- and then, on
    this live account, repeatedly never calls again. The donor pays, the
    money arrives in Razorpay, and nothing in this app ever hears about
    it. No amount of logic inside the webhook route can fix that: by the
    time the payment exists, nothing is calling us.

    So this doesn't wait to be called. It takes the donor details Zoho did
    give us (PendingZohoSubmission, written on that first call) and the
    payments Razorpay knows it captured, and joins the two itself.

    Matching, deliberately conservative -- these produce 80G tax receipts,
    so a wrong match is worse than no match:
      - same phone number, normalised (Zoho sends "+919873287387" where
        Razorpay commonly returns "9873287387" for the same donor), and
      - the payment was captured *after* the form was submitted and within
        max_age_hours of it, and
      - the same amount, when the submission carries one at all (see
        _candidates -- Zoho's pre-payment call doesn't always include it),
        and
      - the payment isn't already attached to some other donation, and
      - exactly one payment fits this submission and exactly one
        submission fits that payment.
    Anything ambiguous is marked "ambiguous" and left for a human rather
    than guessed at. A submission older than max_age_hours with nothing
    matching is marked "unpaid" -- the ordinary case of someone opening
    the form and not paying -- so it stops being rechecked forever.

    min_age_minutes leaves a payment in flight alone: a donor who
    submitted the form 30 seconds ago may be mid-checkout, and Zoho's own
    follow-up call (when it does work) should get the chance to handle it
    first.

    max_per_run bounds the work one run does, because each match costs a
    Razorpay confirmation call and this executes inside an HTTP request
    under gunicorn's worker timeout -- the failure mode this codebase has
    already been bitten by twice. The remainder is picked up next run.

    Submissions older than lookback_days + 1 are out of scope entirely
    (there'd be no payment left in the scan window to match them to). If
    the job is ever off for longer than that, those rows stay unresolved
    rather than being wrongly closed -- they're inert, and the money they
    represent still surfaces via orphan_payments below.

    Returns a summary dict: created/ambiguous/unpaid/still_waiting/failed,
    the orphan payments nothing could explain, and `error` if the Razorpay
    scan itself failed (in which case nothing was resolved -- an
    unreachable Razorpay must never be read as "no payments exist")."""
    summary = {
        "created": [], "ambiguous": [], "unpaid": 0, "still_waiting": 0,
        "orphan_payments": [], "failed": [], "error": None,
    }

    def _mark(submission, resolution, note=None, donation_id=None):
        """Records a decision about one submission and commits it there and
        then, rather than batching every decision into a single commit at
        the end. A creation failure further down the loop rolls the session
        back, and a batched commit would take every decision made since the
        last one with it -- so a run that hit one bad payload would quietly
        forget which submissions it had already ruled on."""
        submission.resolution = resolution
        submission.resolved_at = datetime.datetime.utcnow()
        if note is not None:
            submission.note = note[:300]
        if donation_id is not None:
            submission.donation_id = donation_id
        db.session.commit()

    payments, error = unreconciled_razorpay_payments(config, lookback_days=lookback_days)
    if error:
        summary["error"] = error
        return summary

    now = datetime.datetime.utcnow()
    open_submissions = (
        PendingZohoSubmission.query
        .filter(PendingZohoSubmission.resolved_at.is_(None))
        .filter(PendingZohoSubmission.received_at >= now - datetime.timedelta(days=lookback_days + 1))
        .all()
    )

    def _candidates(submission):
        """Payments that could plausibly belong to this submission.

        Phone and the time window are required. Amount is used when we
        have it, and skipped when we don't: Zoho's pre-payment call is not
        guaranteed to carry an amount (it fires before the gateway is
        involved, and which field a given form maps to the `amount`
        payload parameter is per-form configuration we don't control).
        Requiring it outright would mean any form that omits it silently
        never matches anything -- the same shape of invisible failure this
        whole feature exists to end, just one level further in.

        Dropping to phone + window is still a narrow match, and the
        one-payment-to-one-submission check downstream refuses anything
        ambiguous, so the weaker case fails safe rather than guessing."""
        if not submission.phone_normalized:
            return []
        return [
            p for p in payments
            if normalize_phone(p["contact"] or "") == submission.phone_normalized
            and (submission.amount is None or abs(p["amount"] - submission.amount) < 0.01)
            # Both sides naive UTC: received_at is stored that way like
            # every other timestamp in this codebase, and created_at_utc is
            # carried alongside the IST created_at precisely so this
            # comparison never has to straddle two frames.
            and submission.received_at <= p["created_at_utc"]
            <= submission.received_at + datetime.timedelta(hours=max_age_hours)
        ]

    # Ambiguity is worked out across the whole set *before* anything is
    # resolved, deliberately. Deciding it as the loop went was subtly
    # order-dependent and wrong: the first of two submissions competing for
    # one payment would be flagged ambiguous, which then made it invisible
    # as a rival to the second, which would sail through and claim the
    # payment. One of two indistinguishable donors got a receipt purely by
    # virtue of database row order. Counting first removes the ordering
    # from the decision entirely.
    eligible = [
        s for s in open_submissions
        if (now - s.received_at) >= datetime.timedelta(minutes=min_age_minutes)
    ]
    summary["still_waiting"] += len(open_submissions) - len(eligible)

    candidates_by_submission = {s.id: _candidates(s) for s in eligible}

    claims_per_payment = {}
    for candidates in candidates_by_submission.values():
        for candidate in candidates:
            claims_per_payment[candidate["payment_id"]] = claims_per_payment.get(candidate["payment_id"], 0) + 1

    for submission in eligible:
        age = now - submission.received_at
        candidates = candidates_by_submission[submission.id]

        if len(candidates) > 1:
            _mark(submission, "ambiguous", (
                f"{len(candidates)} captured payments match this submission "
                f"({', '.join(c['payment_id'] for c in candidates)}) -- resolve by hand."
            ))
            summary["ambiguous"].append(submission)
            continue

        if not candidates:
            if age > datetime.timedelta(hours=max_age_hours):
                _mark(submission, "unpaid",
                      "No captured payment ever matched -- most likely an abandoned checkout.")
                summary["unpaid"] += 1
            else:
                summary["still_waiting"] += 1
            continue

        payment = candidates[0]

        # The mirror of the check above: if this one payment also fits some
        # *other* submission, neither can be resolved safely.
        if claims_per_payment.get(payment["payment_id"], 0) > 1:
            _mark(submission, "ambiguous", (
                f"Payment {payment['payment_id']} matches this and "
                f"{claims_per_payment[payment['payment_id']] - 1} other submission(s) "
                "equally well -- resolve by hand."
            ))
            summary["ambiguous"].append(submission)
            continue

        if len(summary["created"]) >= max_per_run:
            # Bounded work per run. Each match below costs a Razorpay
            # confirmation call, and this runs inside an HTTP request with
            # gunicorn's worker timeout over it -- the failure mode this
            # codebase has already been bitten by twice (slow synchronous
            # work blowing past the timeout, worker killed mid-response,
            # nothing in the log). Anything over the cap is simply picked
            # up by the next hourly run.
            summary["still_waiting"] += 1
            continue

        # Re-verify against Razorpay by ID before writing a receipt. The
        # scan above already filtered on status == "captured", but that
        # was a list read; this is the same single-payment confirmation
        # the webhook path makes, and it costs one API call to be certain
        # the thing we're about to issue a tax receipt for is real.
        captured, verify_error = _zoho_payment_is_captured(payment["payment_id"])
        if verify_error or not captured:
            summary["still_waiting"] += 1
            continue

        # Last line of defence against two receipts for one payment,
        # checked here rather than earlier in the loop: as late as
        # possible, immediately before the write. The scan already
        # excludes payments attached to a donation, but that snapshot was
        # taken before this loop began, and the hourly cron can overlap
        # with the daily report's own run -- so the donation this is
        # guarding against may well appear *during* the confirmation call
        # just above. Checking before that call (where this originally
        # sat) leaves exactly that window open.
        if Donation.query.filter_by(razorpay_payment_id=payment["payment_id"]).first():
            summary["still_waiting"] += 1
            continue

        payload = json.loads(submission.payload_json or "{}")

        # If Zoho's pre-payment call carried no usable amount (see
        # _candidates above), take it from Razorpay. That's the amount of
        # money actually received, which is the figure a receipt has to
        # state regardless of what any form field said -- without this,
        # a matched payment would go on to fail _create_zoho_donation's
        # amount validation and be filed as "needs a human" for a value
        # we already have in hand.
        try:
            float(payload.get("amount"))
        except (TypeError, ValueError):
            payload["amount"] = payment["amount"]

        campaign = submission.campaign
        if campaign is None:
            _mark(submission, "ambiguous",
                  f"Campaign '{submission.campaign_param}' no longer exists -- resolve by hand.")
            summary["ambiguous"].append(submission)
            continue

        try:
            donation, create_error = _create_zoho_donation(
                payload, campaign, payment["payment_id"], payment.get("order_id"),
            )
        except Exception as exc:
            # One malformed stored payload must not abort the whole run
            # and strand every other donor's receipt behind it. Rolled
            # back so the session is usable for the submissions after this
            # one, and left unresolved so the next run retries it.
            db.session.rollback()
            current_app.logger.exception(
                "Reconciliation failed to create a donation for pending submission %s", submission.id,
            )
            summary["failed"].append({"pending_id": submission.id, "error": str(exc)})
            continue

        if create_error:
            body, _status = create_error
            _mark(submission, "ambiguous",
                  f"Payment {payment['payment_id']} matched, but: {body.get('error')}")
            summary["ambiguous"].append(submission)
            continue

        _mark(submission, "reconciled", donation_id=donation.id)
        summary["created"].append(donation)

        # Keeps a payment from being matched twice within one run.
        payments = [p for p in payments if p["payment_id"] != payment["payment_id"]]

        db.session.add(AdminActivityLog(
            admin_username="system", action="zoho_donation_reconciled", target_type="donation",
            target_id=donation.id,
            details=(
                f"campaign={campaign.name} amount={donation.amount} "
                f"transaction_id={payment['payment_id']} receipt={donation.receipt_number} "
                f"(Zoho never sent a payment-confirmed webhook; matched from Razorpay)"
            )[:500],
        ))

    db.session.commit()

    # Whatever's left is money Razorpay captured that this app still can't
    # account for from any source -- not just Zoho. Reported, never
    # guessed at, since there's no donor or campaign data behind it.
    summary["orphan_payments"] = payments
    return summary


def _record_pending_zoho_submission(payload, campaign, campaign_param):
    """Stores Zoho's early (pre-payment) webhook call so the donor details
    on it survive until a payment can be matched to them. See
    PendingZohoSubmission's docstring for why this exists at all.

    Deduplicated on (campaign, normalised phone, amount) among records
    still unresolved: Zoho can send this early call more than once for one
    submission (its own internal retries, a donor retrying a failed
    payment on the same form), and two pending rows for one real
    submission would later look to the reconciler like two people owed a
    receipt -- which it would then correctly refuse to resolve as
    ambiguous, turning a recoverable case into a manual one for no
    reason."""
    phone_normalized = normalize_phone(payload.get("phone") or "") or None
    try:
        amount = float(payload.get("amount"))
    except (TypeError, ValueError):
        amount = None

    existing = PendingZohoSubmission.query.filter_by(
        campaign_id=campaign.id, phone_normalized=phone_normalized,
        amount=amount, resolved_at=None,
    ).first()
    if existing:
        # Refresh the stored payload -- a resubmission may carry corrected
        # details (a fixed typo in the name/PAN), and the newest version of
        # what the donor told us is the one worth keeping.
        existing.payload_json = json.dumps(payload)
        existing.full_name = (payload.get("full_name") or "")[:150] or None
        db.session.commit()
        return existing

    pending = PendingZohoSubmission(
        campaign_id=campaign.id,
        campaign_param=(campaign_param or "")[:150] or None,
        full_name=(payload.get("full_name") or "")[:150] or None,
        phone_normalized=phone_normalized,
        amount=amount,
        payload_json=json.dumps(payload),
    )
    db.session.add(pending)
    db.session.commit()
    return pending


def _close_pending_for_donation(donation, resolution):
    """Marks any still-open pending submission that this donation clearly
    satisfies as resolved, so reconciliation stops looking for a payment
    that has already been accounted for. Matched on the same three fields
    the reconciler itself matches on (campaign, normalised phone, amount);
    a pending row that doesn't match stays open on purpose rather than
    being closed on a guess."""
    phone_normalized = normalize_phone(donation.donor.phone or "") if donation.donor else None
    if not phone_normalized:
        return

    for pending in PendingZohoSubmission.query.filter_by(
        campaign_id=donation.campaign_id, phone_normalized=phone_normalized,
        amount=float(donation.amount), resolved_at=None,
    ).all():
        pending.resolved_at = datetime.datetime.utcnow()
        pending.resolution = resolution
        pending.donation_id = donation.id
    db.session.commit()


def _create_zoho_donation(payload, campaign, transaction_id, order_id):
    """Turns a validated Zoho Forms payload plus a *confirmed-captured*
    Razorpay payment into a real donation with a real receipt. Returns
    (donation, error) -- error is (dict, status_code) ready to jsonify, and
    is None on success.

    Extracted from zoho_form_donation_webhook() so reconcile_zoho_submissions()
    can create donations through the identical path rather than a
    second implementation of the same rules. That mattered: the whole
    reason reconciliation exists is that Zoho's follow-up call often never
    arrives, which means the reconciler -- not the webhook -- is the path
    most of these donations will actually be created through. Two copies
    of the PAN/80G/high-value rules would have meant the path that runs
    least often is the only one anybody ever reviews.

    Callers must have already verified with Razorpay that transaction_id
    is captured. This function does not re-check; it's the "write it down"
    half, not the "is it real" half."""
    try:
        amount = float(payload.get("amount"))
        if amount <= 0:
            raise ValueError("amount must be greater than 0")
    except (TypeError, ValueError):
        return None, ({"error": f"Invalid amount '{payload.get('amount')}'"}, 400)

    pan = (payload.get("pan") or "").strip().upper()
    if pan and not is_valid_pan(pan):
        return None, ({"error": f"Invalid PAN '{pan}'"}, 400)
    if not is_valid_phone(payload.get("phone")):
        return None, ({"error": f"Invalid phone '{payload.get('phone')}'"}, 400)
    if payload.get("whatsapp_number") and not is_valid_phone(payload.get("whatsapp_number")):
        return None, ({"error": f"Invalid whatsapp_number '{payload.get('whatsapp_number')}'"}, 400)

    high_value_error = high_value_pan_address_error(amount, pan, payload.get("address"))
    if high_value_error:
        return None, ({"error": high_value_error}, 400)

    # Same three-way choice as create_order()'s receipt_type: the Zoho
    # Form can pass "80g"/"non80g" explicitly (a payload parameter mapped
    # to a field on that form), or leave it blank to fall back to the
    # campaign's own fixed Campaign.is_80g default.
    receipt_type = (payload.get("receipt_type") or "").strip().lower()
    if receipt_type == "80g":
        is_80g_requested = True
    elif receipt_type == "non80g":
        is_80g_requested = False
    else:
        is_80g_requested = None

    effective_is_80g = is_80g_requested if is_80g_requested is not None else campaign.is_80g
    if effective_is_80g and not pan:
        return None, ({"error": "A PAN is required to issue an 80G tax receipt for this donation."}, 400)

    # Same REG-001 backstop as create_order(): a PAN that arrived despite
    # not being legally required here (not 80G, not above the high-value
    # threshold) is spurious and must not be written to the donor's shared
    # profile.
    donor_data = dict(payload)
    if not (effective_is_80g or amount > HIGH_VALUE_PAN_THRESHOLD):
        donor_data["pan"] = ""

    donor = find_or_create_donor(donor_data)

    donation = Donation(
        donor_id=donor.id,
        campaign_id=campaign.id,
        amount=amount,
        payment_mode="online",
        status="pending",
        recorded_by="zoho_form",
        razorpay_payment_id=transaction_id,
        razorpay_order_id=order_id,
        is_80g_requested=is_80g_requested,
        remarks=(payload.get("remarks") or "").strip()[:300] or f"Collected via Zoho Form ({campaign.name})",
    )
    db.session.add(donation)
    db.session.commit()

    _finalize_success(donation)
    return donation, None


@bp.route("/internal/zoho-form-donation", methods=["POST"])
@csrf.exempt
@_safe_json_route
def zoho_form_donation_webhook():
    """Receives a payment-confirmed submission from a Zoho Forms donation
    form and turns it into a real donation with a real receipt, the same
    way a donation made directly on this site would be -- via
    _finalize_success, since the underlying payment is a genuine Razorpay
    transaction either way; the account's Zoho Forms are just configured
    to charge through Razorpay from a different form UI, for collection
    that happens outside this website. See README's "Zoho Forms" section
    for exactly what to configure on the Zoho side (Payload Parameters,
    URL Parameter, Custom Header) -- this route is only half of the
    integration; each Zoho Form's own Webhook settings are the other half,
    and this app has no way to configure those for you.

    No browser/cookie is involved -- authenticated the same way as
    internal_daily_report_send, via a shared secret (X-Zoho-Webhook-Token
    header) compared with hmac.compare_digest, not session/CSRF.

    Which campaign a submission belongs to is fixed per Zoho Form, not
    something the submitted data can claim -- passed as a URL Parameter
    (?campaign=<name>) configured once in that form's own Webhook setup,
    the same way the shared token is a Custom Header rather than part of
    the JSON body every form's own payload mapping would otherwise have to
    repeat.

    Zoho sends the payment result asynchronously from the form submission
    itself -- a "pending" call first, then (in theory) the real outcome
    once the gateway responds, and Zoho's docs recommend enabling the
    payment field's "workflow" option so the *final* status/transaction ID
    reach here directly instead of the initial pending one. In practice,
    on a live account, that second call has not reliably arrived even with
    workflow enabled: a submission's own Zoho record went on to show
    `Payment Status: Completed`, but the one and only webhook call this
    route ever received for it carried `status: processing`, and no
    follow-up call ever came -- Zoho counts a 200 response as a
    successfully delivered webhook regardless of what this route did with
    it, so there's nothing on Zoho's side to retry or alert on, and the
    donation would otherwise have been silently lost forever.

    Because of that, Zoho's self-reported payment_status is treated as
    advisory, not authoritative -- it's used only to short-circuit calls
    that don't even have a transaction ID yet, avoiding a wasted Razorpay
    API call. The actual pass/fail decision is made by asking Razorpay
    directly whether the extracted payment ID is captured
    (_zoho_payment_is_captured), the same source of truth every other
    payment-confirmation path in this codebase ultimately defers to. This
    means a donation can be created off the *very first* call this route
    receives for a given payment, regardless of what status label Zoho
    attached to it or whether Zoho ever sends a second call at all.

    A receipt is only ever issued when *both* of these hold: a
    genuine-looking Razorpay payment ID was extracted from
    payment_transaction_id, and Razorpay itself confirms that payment is
    captured. Neither is trusted alone -- a "Completed"-labelled call with
    a blank/unrecognisable transaction id is rejected (400) rather than
    accepted with a placeholder, and a real-looking transaction id is
    still checked against Razorpay regardless of what status Zoho
    attached to it, since a receipt must never be issued for anything
    short of a confirmed payment.

    payment_transaction_id doesn't arrive as a bare Razorpay payment ID --
    Zoho's own "Payment Transaction ID" field (confirmed from a live
    account's Reports grid) is a combined string like "Txn ID :
    pay_TWnsKWUifmlYnc Order ID : order_TWns42m6OljsvJ". Pulled apart with
    a regex below rather than a fixed split position, so a bare "pay_..."
    (if some future form's payload parameter ever sends one on its own)
    still works the same way. If no "pay_..." pattern is found at all, the
    transaction id is treated as missing rather than falling back to
    whatever raw text arrived.

    Idempotency: keyed on the extracted payment_id (stored as
    Donation.razorpay_payment_id, same column the site's own Razorpay flow
    uses) -- a re-pushed or duplicate-delivered webhook for a
    transaction_id already recorded is a no-op, returning the donation
    that already exists rather than creating a second one. This is a
    plain SELECT-then-INSERT check, not a database-enforced uniqueness
    constraint (razorpay_payment_id has no unique index -- some rows
    already share the literal value "SIMULATED" from demo-mode testing),
    so two deliveries for the same transaction_id arriving within
    milliseconds of each other could in principle both pass the check
    before either commits. Realistic donation traffic through a form is
    human-paced, not concurrent, so this is an accepted gap rather than
    one closed with a schema migration -- any duplicate that did slip
    through would still show up in Activity Log against the same
    transaction_id and campaign, so it wouldn't go unnoticed.
    """
    expected_token = current_app.config.get("ZOHO_FORMS_WEBHOOK_TOKEN")
    if not expected_token:
        return jsonify({"error": "ZOHO_FORMS_WEBHOOK_TOKEN not configured"}), 503

    provided_token = request.headers.get("X-Zoho-Webhook-Token", "")
    if not hmac.compare_digest(provided_token, expected_token):
        return jsonify({"error": "Unauthorized"}), 401

    campaign_name = (request.args.get("campaign") or "").strip()
    if not campaign_name:
        return jsonify({"error": "Missing ?campaign= URL parameter"}), 400
    campaign = Campaign.query.filter(db.func.lower(Campaign.name) == campaign_name.lower()).first()
    if not campaign:
        return jsonify({
            "error": f"No campaign named '{campaign_name}' -- create it first under Admin > Campaigns"
        }), 400

    # Zoho can send application/json, application/x-www-form-urlencoded, or
    # multipart/form-data depending on the webhook's Content-Type setting
    # -- none of our payload parameters are file attachments, so all three
    # carry everything this route needs; falls back to form data (covers
    # both of the non-JSON content types) when the body isn't JSON.
    payload = request.get_json(silent=True)
    if payload is None:
        payload = request.form.to_dict()

    # Zoho's own payment_status (confirmed from a live account's Reports
    # grid: "Completed"/"Processing"/"Processing not needed") is advisory
    # only now -- see the docstring for why. Used here just to recognise
    # the one case worth flagging as an error rather than a normal skip: a
    # "Completed"-labelled call that somehow has no transaction id at all.
    payment_status = (payload.get("payment_status") or "").strip().lower()
    looks_like_success_label = payment_status in ("completed", "success", "succeeded", "paid", "captured")

    # Zoho's own "Payment Transaction ID" field (the same one its Reports
    # grid shows) isn't a bare Razorpay payment ID -- it's a combined
    # string like "Txn ID : pay_TWnsKWUifmlYnc Order ID : order_TWns42m6OljsvJ".
    # Pulled apart with a regex rather than trusting either the whole
    # string or a fixed split position, so this still works if that
    # string's exact wording/spacing ever changes, or if a future form's
    # payload parameter happens to send a bare "pay_..." on its own.
    raw_transaction_field = (payload.get("payment_transaction_id") or "").strip()
    payment_id_match = re.search(r"pay_\w+", raw_transaction_field)
    # In practice, a live account's webhook payload for this parameter has
    # come through as just the bare "pay_..." transaction ID, without the
    # "Order ID : order_..." half the Reports grid shows alongside it (the
    # combined display there appears to be Reports-only, not something the
    # webhook payload itself carries) -- so the order ID is picked up two
    # ways: from inside payment_transaction_id if it happens to be there
    # (some accounts/forms may still combine them), or from a separate
    # `payment_order_id` payload parameter if that form's Payload
    # Parameters were mapped to include one (see README -- Zoho Forms
    # doesn't officially list "Order ID" as one of its four
    # webhook-transferable payment fields, so this may not always be
    # available; it's a nice-to-have here, not required for the receipt).
    order_id_match = re.search(r"order_\w+", raw_transaction_field)
    order_id = (
        order_id_match.group() if order_id_match
        else (payload.get("payment_order_id") or "").strip() or None
    )
    # Deliberately not falling back to the raw string when no "pay_..."
    # pattern is found. No payment id at all is the normal shape of Zoho's
    # early async call -- skipped like any other incomplete submission,
    # *unless* Zoho itself already labelled this call "Completed", in
    # which case a call with a success label but no id at all is genuinely
    # anomalous and worth surfacing as an error rather than silently
    # skipping.
    if not payment_id_match:
        if looks_like_success_label:
            return jsonify({
                "error": "Missing or unrecognised payment_transaction_id for a completed payment"
            }), 400
        # NOT a dead end any more. This is the call Zoho reliably sends and
        # the one it frequently never follows up on, so throwing it away
        # was exactly what made those donations vanish: the donor had
        # already told us everything except the payment ID, and we dropped
        # it. Recorded instead, for reconcile_zoho_submissions() to match
        # against Razorpay's own record of captured payments once the money
        # actually lands -- with or without Zoho ever calling again.
        pending = _record_pending_zoho_submission(payload, campaign, campaign_name)
        return jsonify({
            "pending": "awaiting payment confirmation", "status": payment_status,
            "pending_submission_id": pending.id,
        }), 200
    transaction_id = payment_id_match.group()

    existing = Donation.query.filter_by(razorpay_payment_id=transaction_id).first()
    if existing:
        return jsonify({
            "skipped": "already processed", "donation_id": existing.id,
            "receipt_number": existing.receipt_number,
        }), 200

    # The actual pass/fail decision: verified against Razorpay directly
    # rather than trusting payment_status, which production traffic showed
    # can arrive as "processing" on the only call this route ever gets for
    # a payment that Razorpay itself goes on to capture moments later (see
    # the docstring). captured=True here is what "the payment is real and
    # the money is ours" now actually means for this route.
    captured, verify_error = _zoho_payment_is_captured(transaction_id)
    if verify_error:
        # Non-2xx and deliberately so: unlike _payment_is_captured's
        # fail-open (used by the site's own already-signature-verified
        # flow), this webhook has no other proof the payment is genuine,
        # so an inability to check is a failure, not a shrug. Zoho logs a
        # non-2xx response as a failed webhook delivery under that form's
        # "Webhooks - Failed Entries", re-pushable by hand once Razorpay
        # is reachable again -- better than the donation silently vanishing.
        return jsonify({
            "error": f"Could not confirm payment {transaction_id} with Razorpay: {verify_error}"
        }), 502
    if not captured:
        return jsonify({"skipped": "payment not yet captured", "transaction_id": transaction_id}), 200

    donation, create_error = _create_zoho_donation(payload, campaign, transaction_id, order_id)
    if create_error:
        body, status = create_error
        return jsonify(body), status
    donor = donation.donor

    # Zoho's follow-up call did arrive for this one after all, so close out
    # any pending record this submission left behind on its earlier call --
    # otherwise reconciliation would keep chasing a payment that's already
    # been turned into a receipt right here.
    _close_pending_for_donation(donation, resolution="webhook")

    db.session.add(AdminActivityLog(
        admin_username="system", action="zoho_form_donation_received", target_type="donation",
        target_id=donation.id,
        details=(
            f"campaign={campaign.name} amount={donation.amount} transaction_id={transaction_id}"
            + (f" order_id={order_id}" if order_id else "")
        )[:500],
    ))
    db.session.commit()

    return jsonify({
        "ok": True, "donation_id": donation.id, "donor_id": donor.id,
        "receipt_number": donation.receipt_number, "transaction_id": transaction_id,
        "order_id": order_id,
    })


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

    Campaign.suppress_receipt (Dhoti Kurta Contribution and anything else
    marked this way) does NOT change any of this -- a receipt number and
    PDF are issued for these donations exactly like any other, and the
    donor can still view/download their own here or on the success page
    the same way. What suppress_receipt actually suppresses is the
    *proactive* send: no email, no WhatsApp message goes out on its own
    (see public._finalize_success and admin._create_offline_donation).
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
