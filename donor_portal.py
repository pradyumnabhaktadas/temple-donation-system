import datetime
from functools import wraps

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash,
    session, send_file, current_app,
)
from flask_login import current_user

from extensions import db
from models import Donor, Donation, DonorLoginOTP
from pdf_utils import generate_annual_statement_pdf
from public import _org_cfg
from sms_utils import generate_otp, send_otp
from utils import is_valid_pan

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
def send_otp_route():
    phone = request.form.get("phone", "").strip()

    if not phone:
        flash("Please enter your phone number.")
        return redirect(url_for("donor_portal.login"))

    donor = Donor.query.filter_by(phone=phone).first()
    if donor is None:
        flash("No donor account found with that phone number. Have you made a donation with us before?")
        return redirect(url_for("donor_portal.login"))

    # Rate limit: don't let one phone number burn through OTP requests
    # (matters once this is wired to a real, per-message-cost SMS provider).
    one_hour_ago = datetime.datetime.utcnow() - datetime.timedelta(hours=1)
    recent_count = DonorLoginOTP.query.filter(
        DonorLoginOTP.phone == phone, DonorLoginOTP.created_at >= one_hour_ago
    ).count()
    if recent_count >= current_app.config["OTP_MAX_REQUESTS_PER_HOUR"]:
        flash("Too many login attempts for this number. Please try again in an hour.")
        return redirect(url_for("donor_portal.login"))

    otp = generate_otp(current_app.config["OTP_LENGTH"])
    record = DonorLoginOTP(
        phone=phone,
        expires_at=datetime.datetime.utcnow() + datetime.timedelta(minutes=current_app.config["OTP_EXPIRY_MINUTES"]),
    )
    record.set_otp(otp)
    db.session.add(record)
    db.session.commit()

    was_sent = send_otp(phone, otp)
    if not was_sent:
        # Demo mode -- no SMS provider configured yet. Show the code
        # directly instead of texting it, clearly marked as such.
        flash(f"DEMO MODE (no SMS provider configured): your OTP is {otp}")
    else:
        flash(f"An OTP has been sent to {phone}.")

    return redirect(url_for("donor_portal.verify", phone=phone))


@bp.route("/verify", methods=["GET"])
def verify():
    phone = request.args.get("phone", "").strip()
    if not phone:
        return redirect(url_for("donor_portal.login"))
    return render_template("donor_verify_otp.html", phone=phone)


@bp.route("/verify", methods=["POST"])
def verify_submit():
    phone = request.form.get("phone", "").strip()
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

    donations = donor.donations.filter_by(status="success").order_by(Donation.donation_date.desc()).all()
    available_fys = sorted({d.financial_year for d in donations if d.financial_year}, reverse=True)

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

    # Phone isn't editable here -- it's the login identity. A donor wanting
    # to change their phone number should contact the temple office (an
    # admin can update it via Admin -> Donors -> Edit).
    donor.full_name = form.get("full_name", "").strip() or donor.full_name
    donor.email = form.get("email", "").strip().lower() or None
    donor.whatsapp_number = form.get("whatsapp_number", "").strip() or None
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
