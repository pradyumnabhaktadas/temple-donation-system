import datetime
from functools import wraps

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash,
    session, send_file, current_app,
)
from flask_login import current_user

from extensions import db, limiter
from models import Donor, Donation, DonorLoginOTP
from pdf_utils import generate_annual_statement_pdf
from public import _org_cfg
from sms_utils import generate_otp, send_otp
from utils import is_valid_pan, is_valid_phone, normalize_phone

bp = Blueprint("donor_portal", __name__, url_prefix="/my-donations")


def donor_login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("donor_id"):
            flash("Please log in to continue.")
            return redirect(url_for("donor_portal.login"))
        return view(*args, **kwargs)
    return wrapped


def _current_donor():
    donor_id = session.get("donor_id")
    return Donor.query.get(donor_id) if donor_id else None


@bp.route("/", methods=["GET"])
def login():
    if session.get("donor_id"):
        return redirect(url_for("donor_portal.account"))
    return render_template("my_donations.html")


@bp.route("/send-otp", methods=["POST"])
# IP-level throttle on top of the per-phone hourly cap below -- QA report
# REG-041 found 8 rapid POSTs for the same number all went through
# unthrottled. On a live, SMS-configured deployment an unthrottled send is
# an SMS-bombing / cost / harassment vector against any registered donor's
# phone; the per-phone cap alone doesn't stop one IP from doing this to
# many different numbers. 10/hour is looser than the 5/hour per-phone cap
# since one IP can legitimately be a shared household/office connection
# (the same reasoning already applied to the donation-status poll limit).
@limiter.limit("10 per hour")
def send_otp_route():
    # normalize_phone() so a donor typing "+91 88020 81265" (or any other
    # equivalent format) still matches the plain 10-digit number their
    # donation was recorded under -- see utils.normalize_phone's docstring.
    # This normalized value is what flows through to /verify via the
    # redirect below, so that route doesn't need to normalize again.
    phone = normalize_phone(request.form.get("phone"))

    if not phone:
        flash("Please enter your phone number.")
        return redirect(url_for("donor_portal.login"))

    donor = Donor.query.filter_by(phone=phone).first()

    # From here on, the response is identical regardless of whether this
    # phone number has a donor account, and regardless of whether it's
    # already hit its hourly cap -- see QA report REG-040. The old code
    # returned a distinct "No donor account found" message and sent the
    # donor back to the login page for an unregistered number, versus
    # redirecting to the verify page for a registered one; that difference
    # is a donor-privacy oracle (anyone could test whether any given phone
    # number has ever donated to the temple), independent of whether login
    # would actually succeed. Everything below only decides, silently,
    # whether a real OTP gets created and sent -- that decision never
    # changes what the caller sees.
    otp_for_demo_display = None
    if donor is not None:
        # Per-phone rate limit: don't let one number burn through OTP
        # requests (matters once this is wired to a real, per-message-cost
        # SMS provider). Tighter than the IP limit above on purpose --
        # this is the one that actually protects a specific donor's phone.
        one_hour_ago = datetime.datetime.utcnow() - datetime.timedelta(hours=1)
        recent_count = DonorLoginOTP.query.filter(
            DonorLoginOTP.phone == phone, DonorLoginOTP.created_at >= one_hour_ago
        ).count()
        if recent_count < current_app.config["OTP_MAX_REQUESTS_PER_HOUR"]:
            otp = generate_otp(current_app.config["OTP_LENGTH"])
            record = DonorLoginOTP(
                phone=phone,
                expires_at=datetime.datetime.utcnow() + datetime.timedelta(
                    minutes=current_app.config["OTP_EXPIRY_MINUTES"]),
            )
            record.set_otp(otp)
            db.session.add(record)
            db.session.commit()

            was_sent = send_otp(phone, otp)
            if not was_sent:
                if current_app.config.get("IS_PRODUCTION"):
                    # DEMO MODE exists so the donor login flow can be tested
                    # end to end before an SMS provider is wired up (see
                    # sms_utils.py) -- it must never do that in production.
                    # Until this was fixed (REG-039/REG-055), knowing a
                    # donor's phone number was enough to read the OTP
                    # straight out of this response and log into their
                    # account -- donation history, address, PAN and all.
                    current_app.logger.warning(
                        "send_otp: no SMS provider configured -- refusing to disclose the OTP for %s in production",
                        phone,
                    )
                    record.consumed = True
                    db.session.commit()
                else:
                    # Local/dev only: show the code directly instead of
                    # texting it. Only reachable when a real donor exists
                    # and wasn't rate-limited -- outside production this
                    # convenience revealing that fact isn't the concern
                    # REG-040 is about (no real donor privacy is at stake
                    # against a developer's own local database).
                    otp_for_demo_display = otp

    if otp_for_demo_display:
        flash(f"DEMO MODE (no SMS provider configured): your OTP is {otp_for_demo_display}")
    else:
        flash("If that phone number has an account with us, a login code has been sent to it.")
    return redirect(url_for("donor_portal.verify", phone=phone))


@bp.route("/verify", methods=["GET"])
def verify():
    phone = normalize_phone(request.args.get("phone"))
    if not phone:
        return redirect(url_for("donor_portal.login"))
    return render_template("donor_verify_otp.html", phone=phone)


@bp.route("/verify", methods=["POST"])
def verify_submit():
    # Defensively normalized again (in case this form field was ever
    # tampered with or reached directly) before it's used for lookups --
    # cheap and idempotent, matches send_otp_route() above.
    phone = normalize_phone(request.form.get("phone"))
    otp_input = request.form.get("otp", "").strip()

    if not phone or not otp_input:
        flash("Please enter the OTP.")
        return redirect(url_for("donor_portal.verify", phone=phone))

    record = (
        DonorLoginOTP.query.filter_by(phone=phone, consumed=False)
        .order_by(DonorLoginOTP.created_at.desc())
        .first()
    )

    if record is None or not record.is_valid():
        flash("That OTP has expired. Please request a new one.")
        return redirect(url_for("donor_portal.login"))

    if record.attempts >= current_app.config["OTP_MAX_VERIFY_ATTEMPTS"]:
        record.consumed = True
        db.session.commit()
        flash("Too many incorrect attempts. Please request a new OTP.")
        return redirect(url_for("donor_portal.login"))

    if not record.check_otp(otp_input):
        record.attempts += 1
        db.session.commit()
        flash("Incorrect OTP. Please try again.")
        return redirect(url_for("donor_portal.verify", phone=phone))

    donor = Donor.query.filter_by(phone=phone).first()
    if donor is None:
        flash("No donor account found with that phone number.")
        return redirect(url_for("donor_portal.login"))

    record.consumed = True
    db.session.commit()

    # Note: deliberately not calling session.clear() here -- Flask's default
    # session is a signed client-side cookie (not a server-side session ID),
    # so there's no session-fixation vector to guard against by wiping it,
    # and clearing it would also log out an admin who happens to be using
    # the same browser (e.g. staff testing both flows).
    session["donor_id"] = donor.id
    session.permanent = True

    return redirect(url_for("donor_portal.account"))


@bp.route("/logout")
def logout():
    session.pop("donor_id", None)
    flash("You've been logged out.")
    return redirect(url_for("donor_portal.login"))


@bp.route("/account", methods=["GET"])
@donor_login_required
def account():
    donor = _current_donor()
    if donor is None:
        session.pop("donor_id", None)
        return redirect(url_for("donor_portal.login"))

    # Show every donation attempt, not just successful ones -- a donor
    # wondering "did my payment go through?" should be able to see a
    # pending/failed row here instead of it just silently not appearing
    # (Total Donated / donation_count above are unaffected, since those
    # properties on Donor already filter to status == "success" on their
    # own).
    donations = donor.donations.order_by(Donation.donation_date.desc()).all()
    available_fys = sorted(
        {d.financial_year for d in donations if d.financial_year and d.status == "success"}, reverse=True
    )

    return render_template(
        "my_donations_results.html", donor=donor, donations=donations, available_fys=available_fys
    )


@bp.route("/account/update", methods=["POST"])
@donor_login_required
def account_update():
    donor = _current_donor()
    if donor is None:
        session.pop("donor_id", None)
        return redirect(url_for("donor_portal.login"))

    form = request.form
    pan = form.get("pan", "").strip().upper()
    if pan and not is_valid_pan(pan):
        flash("That PAN doesn't look right. It should be 10 characters like ABCDE1234F.")
        return redirect(url_for("donor_portal.account"))

    if not is_valid_phone(form.get("whatsapp_number")):
        flash("That WhatsApp number doesn't look right. Please enter a 10-digit mobile number, or a foreign number starting with + and country code.")
        return redirect(url_for("donor_portal.account"))

    # Phone isn't editable here -- it's the login identity. A donor wanting
    # to change their phone number should contact the temple office (an
    # admin can update it via Admin -> Donors -> Edit).
    donor.full_name = form.get("full_name", "").strip() or donor.full_name
    donor.email = form.get("email", "").strip().lower() or None
    donor.whatsapp_number = normalize_phone(form.get("whatsapp_number")) or None
    donor.pan = pan or None
    donor.address = form.get("address", "").strip() or None
    donor.city = form.get("city", "").strip() or None
    donor.state = form.get("state", "").strip() or None
    donor.pincode = form.get("pincode", "").strip() or None
    db.session.commit()

    flash("Your details have been updated.")
    return redirect(url_for("donor_portal.account"))


@bp.route("/statement/<int:donor_id>")
def download_statement(donor_id):
    """One consolidated PDF of everything a donor gave in a given financial
    year, instead of them having to download every individual receipt.
    Generated on the fly, not stored -- always reflects the latest data.

    Access: either the logged-in donor viewing their own statement, or a
    logged-in staff/admin user (e.g. linking to this from a donor's admin
    page when they call the office asking for one).
    """
    is_self = session.get("donor_id") == donor_id
    is_staff = current_user.is_authenticated
    if not (is_self or is_staff):
        flash("Please log in to download your statement.")
        return redirect(url_for("donor_portal.login"))

    donor = Donor.query.get_or_404(donor_id)
    fy = request.args.get("fy", "").strip()

    if not fy:
        flash("Please choose a financial year.")
        return redirect(url_for("donor_portal.account") if is_self else url_for("donor_portal.login"))

    donations = (
        donor.donations.filter_by(status="success", financial_year=fy)
        .order_by(Donation.donation_date)
        .all()
    )

    pdf_buffer = generate_annual_statement_pdf(donor, donations, fy, _org_cfg())
    safe_name = f"{donor.full_name}_{fy}_statement".replace(" ", "_").replace("/", "_")
    return send_file(
        pdf_buffer, mimetype="application/pdf", as_attachment=True, download_name=f"{safe_name}.pdf"
    )
