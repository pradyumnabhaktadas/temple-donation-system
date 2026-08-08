import csv
import io
import datetime
from functools import wraps

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash,
    current_app, Response,
)
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy import func, extract

from extensions import db
from models import (
    Donor, Campaign, Donation, AdminUser, ReceiptCounter, BaceProperty, Festival, SevaType,
    LiveToGivePurpose,
)
from utils import get_financial_year, is_valid_pan
from pdf_utils import generate_receipt_pdf
from email_utils import send_receipt_email
from whatsapp_utils import send_receipt_whatsapp
from public import find_or_create_donor, _org_cfg

bp = Blueprint("admin", __name__, url_prefix="/admin")


def admin_role_required(view):
    """Restricts a route to the 'admin' role. 'staff' and 'manager' accounts
    can still do day-to-day work (log donations, view donors/reports) but
    can't create/edit campaigns, merge donor records, or manage other
    admin accounts -- those affect the whole organisation's data."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user.role != "admin":
            flash("That action requires an administrator account.")
            return redirect(url_for("admin.dashboard"))
        return view(*args, **kwargs)
    return wrapped


@bp.before_request
def enforce_password_change():
    # Don't let a first-login admin wander off to other admin pages until
    # they've set a real password.
    if (
        current_user.is_authenticated
        and getattr(current_user, "must_change_password", False)
        and request.endpoint not in ("admin.change_password", "admin.logout")
    ):
        flash("Please set a new password before continuing.")
        return redirect(url_for("admin.change_password"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        user = AdminUser.query.filter_by(username=username).first()

        if user and user.is_locked():
            minutes_left = max(1, int((user.locked_until - datetime.datetime.utcnow()).total_seconds() // 60) + 1)
            flash(f"Too many failed attempts. Try again in about {minutes_left} minute(s).")
            return render_template("admin/login.html")

        if user and user.check_password(password):
            user.register_successful_login()
            db.session.commit()
            login_user(user)
            if user.must_change_password:
                flash("Please set a new password before continuing.")
                return redirect(url_for("admin.change_password"))
            return redirect(url_for("admin.dashboard"))

        if user:
            max_attempts = current_app.config["LOGIN_MAX_ATTEMPTS"]
            lockout_minutes = current_app.config["LOGIN_LOCKOUT_MINUTES"]
            user.register_failed_attempt(max_attempts, lockout_minutes)
            db.session.commit()
        flash("Invalid username or password")
    return render_template("admin/login.html")


@bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("admin.login"))


@bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not current_user.check_password(current_password):
            flash("Current password is incorrect.")
        elif len(new_password) < 8:
            flash("New password must be at least 8 characters.")
        elif new_password != confirm_password:
            flash("New password and confirmation don't match.")
        else:
            current_user.set_password(new_password)
            current_user.must_change_password = False
            db.session.commit()
            flash("Password updated.")
            return redirect(url_for("admin.dashboard"))

    return render_template("admin/change_password.html", force=current_user.must_change_password)


@bp.route("/")
@bp.route("/dashboard")
@login_required
def dashboard():
    today = datetime.date.today()
    start_of_month = today.replace(day=1)
    fy = get_financial_year(today)
    fy_start_year = int(fy.split("-")[0])
    fy_start = datetime.date(fy_start_year, 4, 1)

    def total_since(d):
        dt = datetime.datetime.combine(d, datetime.time.min)
        return db.session.query(func.coalesce(func.sum(Donation.amount), 0)).filter(
            Donation.status == "success", Donation.donation_date >= dt
        ).scalar()

    today_start = datetime.datetime.combine(today, datetime.time.min)
    today_end = today_start + datetime.timedelta(days=1)
    today_total = db.session.query(func.coalesce(func.sum(Donation.amount), 0)).filter(
        Donation.status == "success",
        Donation.donation_date >= today_start,
        Donation.donation_date < today_end,
    ).scalar()
    month_total = total_since(start_of_month)
    year_total = total_since(fy_start)

    campaign_totals = (
        db.session.query(Campaign.name, func.coalesce(func.sum(Donation.amount), 0))
        .outerjoin(Donation, (Donation.campaign_id == Campaign.id) & (Donation.status == "success"))
        .group_by(Campaign.id)
        .order_by(func.coalesce(func.sum(Donation.amount), 0).desc())
        .all()
    )

    mode_totals = (
        db.session.query(Donation.payment_mode, func.coalesce(func.sum(Donation.amount), 0))
        .filter(Donation.status == "success")
        .group_by(Donation.payment_mode)
        .all()
    )

    # last 6 months trend
    def months_ago(base, i):
        """Calendar-correct 'i months before base', avoiding drift from
        fixed 30-day subtraction across months of different lengths."""
        total_months = base.year * 12 + (base.month - 1) - i
        y, m = divmod(total_months, 12)
        return datetime.date(y, m + 1, 1)

    monthly = []
    for i in range(5, -1, -1):
        month_date = months_ago(start_of_month, i)
        y, m = month_date.year, month_date.month
        total = db.session.query(func.coalesce(func.sum(Donation.amount), 0)).filter(
            Donation.status == "success",
            extract("year", Donation.donation_date) == y,
            extract("month", Donation.donation_date) == m,
        ).scalar()
        monthly.append({"label": month_date.strftime("%b %Y"), "total": float(total)})

    donor_count = Donor.query.count()
    donation_count = Donation.query.filter_by(status="success").count()

    # Form 10BD reminder: the statutory deadline for the annual "statement
    # of donations" filing (covering the FY that just ended on 31 March) is
    # 31 May. Surface a reminder banner during that April-May filing window.
    form_10bd_reminder = None
    if today.month in (4, 5):
        deadline = datetime.date(today.year, 5, 31)
        filing_fy = f"{today.year - 1}-{str(today.year)[-2:]}"
        days_left = (deadline - today).days
        form_10bd_reminder = {"filing_fy": filing_fy, "days_left": days_left, "overdue": days_left < 0}

    return render_template(
        "admin/dashboard.html",
        today_total=float(today_total),
        month_total=float(month_total),
        year_total=float(year_total),
        campaign_totals=[(n, float(t)) for n, t in campaign_totals],
        mode_totals=[(m, float(t)) for m, t in mode_totals],
        monthly=monthly,
        donor_count=donor_count,
        donation_count=donation_count,
        fy=fy,
        form_10bd_reminder=form_10bd_reminder,
        today=today,
    )


DONORS_PER_PAGE = 30
DONATIONS_PER_PAGE = 50


@bp.route("/donors")
@login_required
def donors():
    q = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int)
    query = Donor.query
    if q:
        like = f"%{q}%"
        query = query.filter(
            (Donor.full_name.ilike(like))
            | (Donor.phone.ilike(like))
            | (Donor.email.ilike(like))
            | (Donor.pan.ilike(like))
        )
    pagination = db.paginate(
        query.order_by(Donor.full_name), page=page, per_page=DONORS_PER_PAGE, error_out=False
    )

    # Compute totals with one aggregate query instead of the N+1 pattern of
    # calling donor.total_donated / donor.donation_count (each of which
    # issues its own query) for every row on the page.
    donor_ids = [d.id for d in pagination.items]
    totals = {}
    if donor_ids:
        rows = (
            db.session.query(
                Donation.donor_id,
                func.coalesce(func.sum(Donation.amount), 0),
                func.count(Donation.id),
            )
            .filter(Donation.status == "success", Donation.donor_id.in_(donor_ids))
            .group_by(Donation.donor_id)
            .all()
        )
        totals = {donor_id: (float(total), count) for donor_id, total, count in rows}

    return render_template(
        "admin/donors.html", donors=pagination.items, pagination=pagination, totals=totals, q=q
    )


@bp.route("/donors/<int:donor_id>")
@login_required
def donor_detail(donor_id):
    donor = Donor.query.get_or_404(donor_id)
    donations = donor.donations.order_by(Donation.donation_date.desc()).all()
    available_fys = sorted({d.financial_year for d in donations if d.financial_year and d.status == "success"}, reverse=True)
    return render_template(
        "admin/donor_detail.html", donor=donor, donations=donations, available_fys=available_fys
    )


@bp.route("/donors/<int:donor_id>/edit", methods=["GET", "POST"])
@login_required
def donor_edit(donor_id):
    donor = Donor.query.get_or_404(donor_id)
    if request.method == "POST":
        form = request.form
        pan = form.get("pan", "").strip().upper()
        if pan and not is_valid_pan(pan):
            flash("That PAN doesn't look right. It should be 10 characters like ABCDE1234F.")
            return redirect(url_for("admin.donor_edit", donor_id=donor.id))

        donor.full_name = form.get("full_name", "").strip() or donor.full_name
        donor.phone = form.get("phone", "").strip() or None
        donor.whatsapp_number = form.get("whatsapp_number", "").strip() or None
        donor.email = form.get("email", "").strip().lower() or None
        donor.pan = pan or None
        donor.address = form.get("address", "").strip() or None
        donor.city = form.get("city", "").strip() or None
        donor.state = form.get("state", "").strip() or None
        donor.pincode = form.get("pincode", "").strip() or None
        db.session.commit()
        flash("Donor details updated.")
        return redirect(url_for("admin.donor_detail", donor_id=donor.id))
    return render_template("admin/donor_edit.html", donor=donor)


@bp.route("/donors/<int:donor_id>/merge", methods=["POST"])
@login_required
@admin_role_required
def donor_merge(donor_id):
    """Merges a duplicate donor record into this one: reassigns all of the
    duplicate's donations, backfills any blank fields on the kept record,
    then removes the duplicate. This is the fix for donors who ended up
    with two records (e.g. a typo'd phone number) despite the dedup logic
    on the donation form."""
    keep = Donor.query.get_or_404(donor_id)
    lookup = request.form.get("duplicate_lookup", "").strip()

    if not lookup:
        flash("Enter the duplicate donor's phone or email to merge.")
        return redirect(url_for("admin.donor_detail", donor_id=donor_id))

    duplicate = Donor.query.filter(
        Donor.id != keep.id, (Donor.phone == lookup) | (Donor.email == lookup.lower())
    ).first()

    if duplicate is None:
        flash(f"No other donor found matching '{lookup}'.")
        return redirect(url_for("admin.donor_detail", donor_id=donor_id))

    moved = Donation.query.filter_by(donor_id=duplicate.id).update({"donor_id": keep.id})
    keep.full_name = keep.full_name or duplicate.full_name
    keep.phone = keep.phone or duplicate.phone
    keep.email = keep.email or duplicate.email
    keep.pan = keep.pan or duplicate.pan
    keep.address = keep.address or duplicate.address
    keep.city = keep.city or duplicate.city
    keep.state = keep.state or duplicate.state
    keep.pincode = keep.pincode or duplicate.pincode

    db.session.delete(duplicate)
    db.session.commit()

    flash(f"Merged {moved} donation(s) from the duplicate record into {keep.full_name}.")
    return redirect(url_for("admin.donor_detail", donor_id=donor_id))


@bp.route("/donations")
@login_required
def donations():
    # Defaults to "success" (the historical behaviour every other caller of
    # this route already relies on) but a status=... query param lets staff
    # pull up cancelled donations too, or "all" to see everything mixed
    # together -- otherwise a cancelled donation would just silently vanish
    # from this list with no way to find it again.
    status = request.args.get("status", "success")
    query = Donation.query
    if status != "all":
        query = query.filter_by(status=status)
    campaign_id = request.args.get("campaign_id", type=int)
    mode = request.args.get("mode")
    page = request.args.get("page", 1, type=int)
    if campaign_id:
        query = query.filter_by(campaign_id=campaign_id)
    if mode:
        query = query.filter_by(payment_mode=mode)
    pagination = db.paginate(
        query.order_by(Donation.donation_date.desc()), page=page, per_page=DONATIONS_PER_PAGE, error_out=False
    )
    campaigns = Campaign.query.order_by(Campaign.name).all()
    return render_template(
        "admin/donations.html", donations=pagination.items, pagination=pagination, campaigns=campaigns,
        campaign_id=campaign_id, mode=mode, status=status,
    )


@bp.route("/donations/<int:donation_id>/cancel", methods=["POST"])
@login_required
@admin_role_required
def cancel_donation(donation_id):
    """Cancels a donation/receipt rather than deleting it. Sets status to
    "cancelled" instead of a separate boolean flag so it's automatically
    excluded from every existing money-total query, export, and report
    that already filters on status == "success" (dashboard totals, donor
    totals, campaign totals, Form 10BD/collections exports, lapsed-donor
    list) -- with zero changes needed at any of those call sites.

    The receipt PDF itself is never touched -- receipts are immutable once
    issued in this system. The /receipt/<id> download route already
    refuses to serve non-"success" donations, so a cancelled receipt just
    becomes undownloadable rather than being altered or reissued.
    """
    donation = Donation.query.get_or_404(donation_id)
    if donation.status == "cancelled":
        flash("That donation is already cancelled.")
        return redirect(url_for("admin.donor_detail", donor_id=donation.donor_id))
    if donation.status != "success":
        flash("Only successful donations can be cancelled.")
        return redirect(url_for("admin.donor_detail", donor_id=donation.donor_id))

    reason = (request.form.get("cancellation_reason") or "").strip()[:300]
    if not reason:
        flash("Please give a reason for cancelling this donation.")
        return redirect(url_for("admin.donor_detail", donor_id=donation.donor_id))

    donation.status = "cancelled"
    donation.cancelled_at = datetime.datetime.utcnow()
    donation.cancelled_by = current_user.username
    donation.cancellation_reason = reason
    db.session.commit()

    flash(f"Donation {donation.receipt_number or ('#' + str(donation.id))} has been cancelled.")
    return redirect(url_for("admin.donor_detail", donor_id=donation.donor_id))


@bp.route("/donations/<int:donation_id>/restore", methods=["POST"])
@login_required
@admin_role_required
def restore_donation(donation_id):
    """Undoes an accidental cancellation -- restores status to "success"
    and clears the cancellation fields. The donation reappears in every
    total/export it had been excluded from, and its receipt becomes
    downloadable again."""
    donation = Donation.query.get_or_404(donation_id)
    if donation.status != "cancelled":
        flash("That donation isn't cancelled.")
        return redirect(url_for("admin.donor_detail", donor_id=donation.donor_id))

    donation.status = "success"
    donation.cancelled_at = None
    donation.cancelled_by = None
    donation.cancellation_reason = None
    db.session.commit()

    flash(f"Donation {donation.receipt_number or ('#' + str(donation.id))} has been restored.")
    return redirect(url_for("admin.donor_detail", donor_id=donation.donor_id))


def _validated_id_from_form(form, key, model, label):
    """Same idea as public.py's _validated_fk_id, but for the admin
    manual-donation form (werkzeug form data, and flash+redirect instead of
    a JSON error response on failure)."""
    raw = form.get(key)
    if not raw:
        return None, None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None, f"Invalid {label}."
    if not model.query.get(value):
        return None, f"Invalid {label}."
    return value, None


@bp.route("/donations/manual", methods=["GET", "POST"])
@login_required
def manual_donation():
    campaigns = Campaign.query.filter_by(is_active=True).order_by(Campaign.name).all()
    bace_properties = BaceProperty.query.filter_by(is_active=True).order_by(BaceProperty.name).all()
    festivals = Festival.query.filter_by(is_active=True).order_by(Festival.name).all()
    seva_types = SevaType.query.filter_by(is_active=True).order_by(SevaType.name).all()
    live_to_give_purposes = LiveToGivePurpose.query.filter_by(is_active=True).order_by(LiveToGivePurpose.name).all()
    if request.method == "POST":
        form = request.form

        try:
            campaign_id = int(form["campaign_id"])
            amount = float(form["amount"])
        except (KeyError, TypeError, ValueError):
            flash("Please choose a campaign and enter a valid amount.")
            return redirect(url_for("admin.manual_donation"))
        campaign = Campaign.query.get_or_404(campaign_id)

        pan = (form.get("pan") or "").strip()
        if pan and not is_valid_pan(pan):
            flash("That PAN doesn't look right. It should be 10 characters like ABCDE1234F.")
            return redirect(url_for("admin.manual_donation"))

        bace_property_id, error = _validated_id_from_form(form, "bace_property_id", BaceProperty, "BACE property")
        if error:
            flash(error)
            return redirect(url_for("admin.manual_donation"))
        festival_id, error = _validated_id_from_form(form, "festival_id", Festival, "festival")
        if error:
            flash(error)
            return redirect(url_for("admin.manual_donation"))
        seva_type_id, error = _validated_id_from_form(form, "seva_type_id", SevaType, "seva type")
        if error:
            flash(error)
            return redirect(url_for("admin.manual_donation"))
        live_to_give_purpose_id, error = _validated_id_from_form(
            form, "live_to_give_purpose_id", LiveToGivePurpose, "donation purpose"
        )
        if error:
            flash(error)
            return redirect(url_for("admin.manual_donation"))

        receipt_type = form.get("receipt_type")
        if receipt_type == "80g":
            is_80g_requested = True
        elif receipt_type == "non80g":
            is_80g_requested = False
        else:
            is_80g_requested = None

        # Offline payment reference details -- only meaningful for their
        # matching payment_mode (cheque_number/cheque_bank_name for
        # "cheque", bank_transaction_id for "bank_transfer"), but captured
        # regardless of which mode is selected rather than validated
        # against it -- same permissive approach as the rest of this form
        # (e.g. a cheque number entered then the mode changed back to Cash
        # shouldn't block submission, just goes unused).
        cheque_number = (form.get("cheque_number") or "").strip()[:50] or None
        cheque_bank_name = (form.get("cheque_bank_name") or "").strip()[:150] or None
        bank_transaction_id = (form.get("bank_transaction_id") or "").strip()[:100] or None

        donor = find_or_create_donor(form)

        donation_date_str = form.get("donation_date")
        try:
            donation_date = (
                datetime.datetime.strptime(donation_date_str, "%Y-%m-%d")
                if donation_date_str
                else datetime.datetime.utcnow()
            )
        except ValueError:
            flash("That donation date doesn't look right.")
            return redirect(url_for("admin.manual_donation"))

        donation = Donation(
            donor_id=donor.id,
            campaign_id=campaign.id,
            amount=amount,
            payment_mode=form.get("payment_mode", "cash"),
            status="success",
            donation_date=donation_date,
            bace_property_id=bace_property_id,
            festival_id=festival_id,
            seva_type_id=seva_type_id,
            live_to_give_purpose_id=live_to_give_purpose_id,
            is_80g_requested=is_80g_requested,
            cheque_number=cheque_number,
            cheque_bank_name=cheque_bank_name,
            bank_transaction_id=bank_transaction_id,
            remarks=form.get("remarks"),
            recorded_by=current_user.username,
        )
        db.session.add(donation)
        db.session.flush()

        receipt_number, fy = ReceiptCounter.next_receipt_number(donation.effective_is_80g, donation_date)
        donation.receipt_number = receipt_number
        donation.financial_year = fy
        db.session.commit()

        pdf_bytes = generate_receipt_pdf(donation, donor, campaign, _org_cfg())
        donation.receipt_pdf = pdf_bytes
        db.session.commit()
        send_receipt_email(donation, donor, _org_cfg(), pdf_bytes)
        send_receipt_whatsapp(donation, donor, _org_cfg(), pdf_bytes)

        flash(f"Donation recorded. Receipt {receipt_number} generated.")
        return redirect(url_for("admin.donor_detail", donor_id=donor.id))

    return render_template(
        "admin/manual_donation.html", campaigns=campaigns, bace_properties=bace_properties,
        festivals=festivals, seva_types=seva_types, live_to_give_purposes=live_to_give_purposes,
        today=datetime.date.today(),
    )


# Columns the demo file offers and the importer reads. Only the first five
# are mandatory per row -- everything else is optional and left blank/None
# if not supplied.
BULK_IMPORT_REQUIRED_COLUMNS = ["full_name", "campaign_name", "amount", "payment_mode", "donation_date"]
BULK_IMPORT_COLUMNS = [
    "full_name", "phone", "whatsapp_number", "email", "pan", "address", "city", "state", "pincode",
    "campaign_name", "amount", "payment_mode", "donation_date",
    "cheque_number", "cheque_bank_name", "bank_transaction_id",
    "receipt_type", "bace_property_name", "festival_name", "seva_type_name", "live_to_give_purpose_name",
    "remarks",
]
BULK_IMPORT_DEMO_ROWS = [
    {
        "full_name": "Ramesh Kumar", "phone": "9876543210", "whatsapp_number": "", "email": "ramesh@example.com",
        "pan": "ABCDE1234F", "address": "12 MG Road", "city": "Delhi", "state": "Delhi", "pincode": "110001",
        "campaign_name": "General Donations", "amount": "1100", "payment_mode": "cash",
        "donation_date": "2026-04-15", "cheque_number": "", "cheque_bank_name": "", "bank_transaction_id": "",
        "receipt_type": "", "bace_property_name": "", "festival_name": "", "seva_type_name": "",
        "live_to_give_purpose_name": "", "remarks": "Monthly seva",
    },
    {
        "full_name": "Sita Devi", "phone": "9123456780", "whatsapp_number": "", "email": "",
        "pan": "", "address": "", "city": "", "state": "", "pincode": "",
        "campaign_name": "Temple Construction", "amount": "5000", "payment_mode": "cheque",
        "donation_date": "2026-04-20", "cheque_number": "123456", "cheque_bank_name": "HDFC Bank",
        "bank_transaction_id": "", "receipt_type": "", "bace_property_name": "", "festival_name": "",
        "seva_type_name": "", "live_to_give_purpose_name": "", "remarks": "",
    },
    {
        "full_name": "Amit Sharma", "phone": "", "whatsapp_number": "9988776655", "email": "amit@example.com",
        "pan": "FGHIJ5678K", "address": "", "city": "", "state": "", "pincode": "",
        "campaign_name": "Live To Give", "amount": "2100", "payment_mode": "bank_transfer",
        "donation_date": "2026-04-22", "cheque_number": "", "cheque_bank_name": "",
        "bank_transaction_id": "UTR2026042212345", "receipt_type": "80g", "bace_property_name": "",
        "festival_name": "", "seva_type_name": "",
        "live_to_give_purpose_name": "Temple Construction (मंदिर निर्माण के लिए)",
        "remarks": "",
    },
]


@bp.route("/donations/bulk-import/demo.csv")
@login_required
@admin_role_required
def bulk_import_demo_csv():
    """A ready-to-edit template CSV -- every column the importer understands,
    header row plus a few example rows covering cash/cheque/bank transfer
    and an 80G Live To Give donation."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=BULK_IMPORT_COLUMNS)
    writer.writeheader()
    writer.writerows(BULK_IMPORT_DEMO_ROWS)
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=offline_donations_demo.csv"},
    )


def _lookup_by_name(items_by_lower_name, raw_name, label, row_errors):
    """Case-insensitive name lookup for an optional bulk-import column
    (BACE property / festival / seva type / Live To Give purpose). Blank
    input is fine (returns None, no error) -- a name that doesn't match
    anything is treated as a row error rather than silently ignored, so a
    typo doesn't quietly drop data instead of failing loudly."""
    name = (raw_name or "").strip()
    if not name:
        return None
    match = items_by_lower_name.get(name.lower())
    if not match:
        row_errors.append(f"{label} '{name}' not found")
        return None
    return match.id


@bp.route("/donations/bulk-import", methods=["GET", "POST"])
@login_required
@admin_role_required
def bulk_import_donations():
    """Bulk-logs offline donations (cash/cheque/bank transfer) from an
    uploaded CSV -- the same underlying steps as the single-entry "Log
    Offline Donation" form (manual_donation), just looped over many rows.
    Each successful row gets everything an individual manual donation
    gets: a donor record (matched/created via the same find_or_create_donor
    PAN->phone->email dedup used everywhere else), a real receipt number
    from the shared ReceiptCounter sequence, and a generated receipt PDF
    stored on the donation -- so cancellation, exports, statements, and
    Form 10BD all treat imported rows identically to hand-entered ones.

    Rows are validated and processed independently -- one bad row (unknown
    campaign, invalid PAN, malformed date, ...) is skipped with a reason
    rather than failing the whole file. Nothing is committed for a row
    until it passes every check.

    Email/WhatsApp receipt notifications are opt-in per import (default
    off): bulk imports are usually historical/backlog entries, and a donor
    who gave three months ago typically shouldn't get a receipt "just
    arriving" today.
    """
    if request.method == "GET":
        return render_template("admin/bulk_import_donations.html", results=None)

    file = request.files.get("csv_file")
    if not file or not file.filename:
        flash("Please choose a CSV file to upload.")
        return redirect(url_for("admin.bulk_import_donations"))

    send_notifications = request.form.get("send_notifications") == "yes"

    try:
        stream = io.TextIOWrapper(file.stream, encoding="utf-8-sig")
        reader = csv.DictReader(stream)
        fieldnames = {(f or "").strip() for f in (reader.fieldnames or [])}
    except Exception:
        flash("Couldn't read that file -- please upload a CSV (comma-separated) file.")
        return redirect(url_for("admin.bulk_import_donations"))

    missing = [c for c in BULK_IMPORT_REQUIRED_COLUMNS if c not in fieldnames]
    if missing:
        flash(
            "That CSV is missing required column(s): " + ", ".join(missing)
            + ". Download the demo file below for the full column list."
        )
        return redirect(url_for("admin.bulk_import_donations"))

    campaigns_by_name = {c.name.strip().lower(): c for c in Campaign.query.all()}
    bace_by_name = {b.name.strip().lower(): b for b in BaceProperty.query.all()}
    festivals_by_name = {f.name.strip().lower(): f for f in Festival.query.all()}
    seva_by_name = {s.name.strip().lower(): s for s in SevaType.query.all()}
    purposes_by_name = {p.name.strip().lower(): p for p in LiveToGivePurpose.query.all()}

    org_cfg = _org_cfg()
    results = []
    created = 0

    for line_num, raw_row in enumerate(reader, start=2):  # header is line 1
        row = {(k or "").strip(): (v or "").strip() for k, v in raw_row.items() if k}
        row_errors = []

        full_name = row.get("full_name", "")
        if not full_name:
            row_errors.append("full_name is required")

        campaign_name = row.get("campaign_name", "")
        campaign = campaigns_by_name.get(campaign_name.lower()) if campaign_name else None
        if not campaign_name:
            row_errors.append("campaign_name is required")
        elif not campaign:
            row_errors.append(f"campaign '{campaign_name}' not found")

        amount = None
        amount_raw = row.get("amount", "")
        try:
            amount = float(amount_raw)
            if amount <= 0:
                row_errors.append("amount must be greater than 0")
        except ValueError:
            row_errors.append(f"invalid amount '{amount_raw}'")

        payment_mode = (row.get("payment_mode") or "cash").lower()
        if payment_mode not in ("cash", "cheque", "bank_transfer"):
            row_errors.append(f"payment_mode must be cash, cheque, or bank_transfer (got '{payment_mode}')")

        donation_date = None
        date_raw = row.get("donation_date", "")
        try:
            donation_date = datetime.datetime.strptime(date_raw, "%Y-%m-%d")
        except ValueError:
            row_errors.append(f"invalid donation_date '{date_raw}' (expected YYYY-MM-DD)")

        pan = row.get("pan", "").upper()
        if pan and not is_valid_pan(pan):
            row_errors.append(f"invalid PAN '{pan}'")
        row["pan"] = pan

        bace_property_id = _lookup_by_name(bace_by_name, row.get("bace_property_name"), "BACE property", row_errors)
        festival_id = _lookup_by_name(festivals_by_name, row.get("festival_name"), "festival", row_errors)
        seva_type_id = _lookup_by_name(seva_by_name, row.get("seva_type_name"), "seva type", row_errors)
        live_to_give_purpose_id = _lookup_by_name(
            purposes_by_name, row.get("live_to_give_purpose_name"), "donation purpose", row_errors
        )

        receipt_type = (row.get("receipt_type") or "").lower()
        if receipt_type == "80g":
            is_80g_requested = True
        elif receipt_type == "non80g":
            is_80g_requested = False
        else:
            is_80g_requested = None

        if row_errors:
            results.append({"line": line_num, "name": full_name or "(blank)", "ok": False, "errors": row_errors})
            continue

        try:
            donor = find_or_create_donor(row)

            donation = Donation(
                donor_id=donor.id,
                campaign_id=campaign.id,
                amount=amount,
                payment_mode=payment_mode,
                status="success",
                donation_date=donation_date,
                bace_property_id=bace_property_id,
                festival_id=festival_id,
                seva_type_id=seva_type_id,
                live_to_give_purpose_id=live_to_give_purpose_id,
                is_80g_requested=is_80g_requested,
                cheque_number=(row.get("cheque_number") or "")[:50] or None,
                cheque_bank_name=(row.get("cheque_bank_name") or "")[:150] or None,
                bank_transaction_id=(row.get("bank_transaction_id") or "")[:100] or None,
                remarks=(row.get("remarks") or "")[:300] or None,
                recorded_by=current_user.username,
            )
            db.session.add(donation)
            db.session.flush()

            receipt_number, fy = ReceiptCounter.next_receipt_number(donation.effective_is_80g, donation_date)
            donation.receipt_number = receipt_number
            donation.financial_year = fy
            db.session.commit()

            pdf_bytes = generate_receipt_pdf(donation, donor, campaign, org_cfg)
            donation.receipt_pdf = pdf_bytes
            db.session.commit()

            if send_notifications:
                send_receipt_email(donation, donor, org_cfg, pdf_bytes)
                send_receipt_whatsapp(donation, donor, org_cfg, pdf_bytes)

            results.append({"line": line_num, "name": full_name, "ok": True, "receipt_number": receipt_number})
            created += 1
        except Exception as exc:
            db.session.rollback()
            current_app.logger.exception("Bulk import failed on row %s", line_num)
            results.append({
                "line": line_num, "name": full_name or "(blank)", "ok": False,
                "errors": [f"unexpected error -- row skipped ({exc})"],
            })

    skipped = len(results) - created
    flash(f"Bulk import finished: {created} donation(s) created, {skipped} skipped.")
    return render_template("admin/bulk_import_donations.html", results=results, created=created, skipped=skipped)


# Same idea as BULK_IMPORT_* above, but for migrating history from before
# this website existed rather than logging new offline donations -- see
# import_legacy_donations() for how the two differ.
LEGACY_IMPORT_REQUIRED_COLUMNS = ["full_name", "campaign_name", "amount", "donation_date"]
LEGACY_IMPORT_COLUMNS = [
    "full_name", "phone", "whatsapp_number", "email", "pan", "address", "city", "state", "pincode",
    "campaign_name", "amount", "payment_mode", "donation_date", "receipt_number",
    "cheque_number", "cheque_bank_name", "bank_transaction_id", "remarks",
]
LEGACY_IMPORT_DEMO_ROWS = [
    {
        "full_name": "Gopal Krishna Das", "phone": "9811122233", "whatsapp_number": "", "email": "gopal@example.com",
        "pan": "ABCDE1234F", "address": "45 Preet Vihar", "city": "Delhi", "state": "Delhi", "pincode": "110092",
        "campaign_name": "Temple Construction", "amount": "11000", "payment_mode": "cash",
        "donation_date": "2023-06-10", "receipt_number": "OLD/2023/00456",
        "cheque_number": "", "cheque_bank_name": "", "bank_transaction_id": "", "remarks": "",
    },
    {
        "full_name": "Radha Rani Devi", "phone": "9822233344", "whatsapp_number": "", "email": "",
        "pan": "", "address": "", "city": "", "state": "", "pincode": "",
        "campaign_name": "General Donations", "amount": "2500", "payment_mode": "cheque",
        "donation_date": "2024-01-22", "receipt_number": "OLD/2024/00112",
        "cheque_number": "998877", "cheque_bank_name": "SBI", "bank_transaction_id": "", "remarks": "",
    },
    {
        "full_name": "Nitai Chandra", "phone": "", "whatsapp_number": "9933344455", "email": "nitai@example.com",
        "pan": "FGHIJ5678K", "address": "", "city": "", "state": "", "pincode": "",
        "campaign_name": "Annadan", "amount": "7500", "payment_mode": "bank_transfer",
        "donation_date": "2024-11-03", "receipt_number": "",
        "cheque_number": "", "cheque_bank_name": "", "bank_transaction_id": "UTR2024110312345", "remarks": "",
    },
]


@bp.route("/donations/import-legacy/demo.csv")
@login_required
@admin_role_required
def import_legacy_demo_csv():
    """Template CSV for migrating pre-website donor/donation history."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=LEGACY_IMPORT_COLUMNS)
    writer.writeheader()
    writer.writerows(LEGACY_IMPORT_DEMO_ROWS)
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=legacy_donations_demo.csv"},
    )


@bp.route("/donations/import-legacy", methods=["GET", "POST"])
@login_required
@admin_role_required
def import_legacy_donations():
    """Migrates pre-website donor/donation history whose receipts were
    already issued (on paper, or through whatever system you used before)
    -- this is the web-UI equivalent of import_legacy_data.py, for anyone
    who'd rather upload a file than run a script on the server.

    Differs from "Bulk Import" (bulk_import_donations) in the ways that
    matter for already-issued receipts:
      - `receipt_number` is read from the CSV and kept as-is when given,
        instead of being generated from this site's own numbering
        sequence -- your old receipt numbers stay the numbers of record.
        Leave the column blank on a row to auto-generate one from this
        site's sequence instead (e.g. for old records where the original
        number is lost).
      - No PDF is generated for imported rows by default (the real
        receipt already exists on paper/elsewhere) -- ticking "Generate
        PDF receipts" creates one in this site's own layout for every
        row, which will NOT match whatever the original receipt looked
        like. Donations with no PDF here still show correctly in every
        report, statement, and total; the receipt link on this site just
        won't have a file behind it unless you opt in.
      - Email/WhatsApp receipt notifications are never sent -- these
        donors already received their receipt at the time, so importing
        them now should never trigger a fresh notification.

    Donor de-duplication uses the same PAN -> phone -> email matching as
    everywhere else, so importing won't create duplicates of donors who
    already exist from live donations.
    """
    if request.method == "GET":
        return render_template("admin/import_legacy_donations.html", results=None)

    file = request.files.get("csv_file")
    if not file or not file.filename:
        flash("Please choose a CSV file to upload.")
        return redirect(url_for("admin.import_legacy_donations"))

    generate_pdfs = request.form.get("generate_pdfs") == "yes"

    try:
        stream = io.TextIOWrapper(file.stream, encoding="utf-8-sig")
        reader = csv.DictReader(stream)
        fieldnames = {(f or "").strip() for f in (reader.fieldnames or [])}
    except Exception:
        flash("Couldn't read that file -- please upload a CSV (comma-separated) file.")
        return redirect(url_for("admin.import_legacy_donations"))

    missing = [c for c in LEGACY_IMPORT_REQUIRED_COLUMNS if c not in fieldnames]
    if missing:
        flash(
            "That CSV is missing required column(s): " + ", ".join(missing)
            + ". Download the demo file below for the full column list."
        )
        return redirect(url_for("admin.import_legacy_donations"))

    campaigns_by_name = {c.name.strip().lower(): c for c in Campaign.query.all()}
    org_cfg = _org_cfg()
    results = []
    created = 0

    for line_num, raw_row in enumerate(reader, start=2):  # header is line 1
        row = {(k or "").strip(): (v or "").strip() for k, v in raw_row.items() if k}
        row_errors = []

        full_name = row.get("full_name", "")
        if not full_name:
            row_errors.append("full_name is required")

        campaign_name = row.get("campaign_name", "")
        campaign = campaigns_by_name.get(campaign_name.lower()) if campaign_name else None
        if not campaign_name:
            row_errors.append("campaign_name is required")
        elif not campaign:
            row_errors.append(f"campaign '{campaign_name}' not found -- create it first under Admin > Campaigns")

        amount = None
        amount_raw = row.get("amount", "")
        try:
            amount = float(amount_raw)
            if amount <= 0:
                row_errors.append("amount must be greater than 0")
        except ValueError:
            row_errors.append(f"invalid amount '{amount_raw}'")

        payment_mode = (row.get("payment_mode") or "cash").lower()
        if payment_mode not in ("cash", "cheque", "bank_transfer", "online"):
            payment_mode = "cash"  # unrecognised is treated as cash, same as the CLI importer, rather than skipped

        donation_date = None
        date_raw = row.get("donation_date", "")
        try:
            donation_date = datetime.datetime.strptime(date_raw, "%Y-%m-%d")
        except ValueError:
            row_errors.append(f"invalid donation_date '{date_raw}' (expected YYYY-MM-DD)")

        pan = row.get("pan", "").upper()
        if pan and not is_valid_pan(pan):
            row_errors.append(f"invalid PAN '{pan}'")
        row["pan"] = pan

        existing_receipt = (row.get("receipt_number") or "").strip() or None

        if row_errors:
            results.append({"line": line_num, "name": full_name or "(blank)", "ok": False, "errors": row_errors})
            continue

        try:
            donor = find_or_create_donor(row)

            donation = Donation(
                donor_id=donor.id,
                campaign_id=campaign.id,
                amount=amount,
                payment_mode=payment_mode,
                status="success",
                donation_date=donation_date,
                cheque_number=(row.get("cheque_number") or "")[:50] or None,
                cheque_bank_name=(row.get("cheque_bank_name") or "")[:150] or None,
                bank_transaction_id=(row.get("bank_transaction_id") or "")[:100] or None,
                remarks=(row.get("remarks") or "").strip()[:300] or "Imported from legacy records",
                recorded_by=f"legacy import ({current_user.username})",
            )
            db.session.add(donation)
            db.session.flush()

            if existing_receipt:
                donation.receipt_number = existing_receipt[:50]
                donation.financial_year = get_financial_year(donation_date)
            else:
                receipt_number, fy = ReceiptCounter.next_receipt_number(campaign.is_80g, donation_date)
                donation.receipt_number = receipt_number
                donation.financial_year = fy

            if generate_pdfs:
                donation.receipt_pdf = generate_receipt_pdf(donation, donor, campaign, org_cfg)

            db.session.commit()

            results.append({"line": line_num, "name": full_name, "ok": True, "receipt_number": donation.receipt_number})
            created += 1
        except Exception as exc:
            db.session.rollback()
            current_app.logger.exception("Legacy import failed on row %s", line_num)
            reason = "duplicate receipt number" if "unique" in str(exc).lower() else f"unexpected error ({exc})"
            results.append({
                "line": line_num, "name": full_name or "(blank)", "ok": False,
                "errors": [f"row skipped -- {reason}"],
            })

    skipped = len(results) - created
    flash(f"Legacy import finished: {created} donation(s) imported, {skipped} skipped.")
    return render_template("admin/import_legacy_donations.html", results=results, created=created, skipped=skipped)


@bp.route("/campaigns", methods=["GET", "POST"])
@login_required
def campaigns():
    if request.method == "POST":
        if current_user.role != "admin":
            flash("That action requires an administrator account.")
            return redirect(url_for("admin.campaigns"))
        form = request.form
        campaign = Campaign(
            name=form["name"].strip(),
            is_80g=(form.get("is_80g") == "on"),
            description=form.get("description", "").strip() or None,
            target_amount=float(form["target_amount"]) if form.get("target_amount") else None,
        )
        db.session.add(campaign)
        db.session.commit()
        flash(f"Campaign '{campaign.name}' created.")
        return redirect(url_for("admin.campaigns"))

    campaign_list = Campaign.query.order_by(Campaign.is_80g.desc(), Campaign.name).all()
    return render_template("admin/campaigns.html", campaigns=campaign_list)


@bp.route("/campaigns/<int:campaign_id>/toggle", methods=["POST"])
@login_required
@admin_role_required
def toggle_campaign(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    campaign.is_active = not campaign.is_active
    db.session.commit()
    return redirect(url_for("admin.campaigns"))


@bp.route("/campaigns/<int:campaign_id>/edit", methods=["GET", "POST"])
@login_required
@admin_role_required
def campaign_edit(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if request.method == "POST":
        form = request.form
        new_name = form.get("name", "").strip()
        if not new_name:
            flash("Campaign name can't be blank.")
            return redirect(url_for("admin.campaign_edit", campaign_id=campaign_id))

        existing = Campaign.query.filter(Campaign.name == new_name, Campaign.id != campaign.id).first()
        if existing:
            flash(f"Another campaign is already named '{new_name}'.")
            return redirect(url_for("admin.campaign_edit", campaign_id=campaign_id))

        campaign.name = new_name
        campaign.is_80g = form.get("is_80g") == "on"
        campaign.description = form.get("description", "").strip() or None
        campaign.target_amount = float(form["target_amount"]) if form.get("target_amount") else None
        db.session.commit()
        flash(f"Campaign '{campaign.name}' updated.")
        return redirect(url_for("admin.campaigns"))

    return render_template("admin/campaign_edit.html", campaign=campaign)


@bp.route("/campaigns/<int:campaign_id>/delete", methods=["POST"])
@login_required
@admin_role_required
def campaign_delete(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    has_donations = Donation.query.filter_by(campaign_id=campaign.id).first() is not None
    if has_donations:
        flash(
            f"Can't delete '{campaign.name}' -- it has donations recorded against it. "
            "Deactivate it instead to hide it from the donation form."
        )
        return redirect(url_for("admin.campaigns"))

    db.session.delete(campaign)
    db.session.commit()
    flash(f"Campaign '{campaign.name}' deleted.")
    return redirect(url_for("admin.campaigns"))


@bp.route("/bace-properties", methods=["GET", "POST"])
@login_required
def bace_properties():
    if request.method == "POST":
        if current_user.role != "admin":
            flash("That action requires an administrator account.")
            return redirect(url_for("admin.bace_properties"))
        name = request.form.get("name", "").strip()
        if not name:
            flash("Property name can't be blank.")
            return redirect(url_for("admin.bace_properties"))
        if BaceProperty.query.filter_by(name=name).first():
            flash(f"A BACE property named '{name}' already exists.")
            return redirect(url_for("admin.bace_properties"))
        db.session.add(BaceProperty(name=name))
        db.session.commit()
        flash(f"BACE property '{name}' added.")
        return redirect(url_for("admin.bace_properties"))

    properties = BaceProperty.query.order_by(BaceProperty.name).all()
    return render_template("admin/bace_properties.html", properties=properties)


@bp.route("/bace-properties/<int:property_id>/toggle", methods=["POST"])
@login_required
@admin_role_required
def toggle_bace_property(property_id):
    prop = BaceProperty.query.get_or_404(property_id)
    prop.is_active = not prop.is_active
    db.session.commit()
    return redirect(url_for("admin.bace_properties"))


@bp.route("/bace-properties/<int:property_id>/edit", methods=["GET", "POST"])
@login_required
@admin_role_required
def bace_property_edit(property_id):
    prop = BaceProperty.query.get_or_404(property_id)
    if request.method == "POST":
        new_name = request.form.get("name", "").strip()
        if not new_name:
            flash("Property name can't be blank.")
            return redirect(url_for("admin.bace_property_edit", property_id=property_id))

        existing = BaceProperty.query.filter(
            BaceProperty.name == new_name, BaceProperty.id != prop.id
        ).first()
        if existing:
            flash(f"Another BACE property is already named '{new_name}'.")
            return redirect(url_for("admin.bace_property_edit", property_id=property_id))

        prop.name = new_name
        db.session.commit()
        flash(f"BACE property renamed to '{prop.name}'.")
        return redirect(url_for("admin.bace_properties"))

    return render_template("admin/bace_property_edit.html", property=prop)


@bp.route("/bace-properties/<int:property_id>/delete", methods=["POST"])
@login_required
@admin_role_required
def bace_property_delete(property_id):
    prop = BaceProperty.query.get_or_404(property_id)
    has_donations = Donation.query.filter_by(bace_property_id=prop.id).first() is not None
    if has_donations:
        flash(
            f"Can't delete '{prop.name}' -- it has donations recorded against it. "
            "Deactivate it instead to hide it from the BACE Contribution form."
        )
        return redirect(url_for("admin.bace_properties"))

    db.session.delete(prop)
    db.session.commit()
    flash(f"BACE property '{prop.name}' deleted.")
    return redirect(url_for("admin.bace_properties"))


@bp.route("/festivals", methods=["GET", "POST"])
@login_required
def festivals():
    if request.method == "POST":
        if current_user.role != "admin":
            flash("That action requires an administrator account.")
            return redirect(url_for("admin.festivals"))
        name = request.form.get("name", "").strip()
        if not name:
            flash("Festival name can't be blank.")
            return redirect(url_for("admin.festivals"))
        if Festival.query.filter_by(name=name).first():
            flash(f"A festival named '{name}' already exists.")
            return redirect(url_for("admin.festivals"))
        event_date_str = request.form.get("event_date")
        try:
            event_date = datetime.datetime.strptime(event_date_str, "%Y-%m-%d").date() if event_date_str else None
        except ValueError:
            flash("That date doesn't look right.")
            return redirect(url_for("admin.festivals"))
        db.session.add(Festival(name=name, event_date=event_date))
        db.session.commit()
        flash(f"Festival '{name}' added.")
        return redirect(url_for("admin.festivals"))

    festival_list = Festival.query.order_by(Festival.event_date.is_(None), Festival.event_date, Festival.name).all()
    return render_template("admin/festivals.html", festivals=festival_list)


@bp.route("/festivals/<int:festival_id>/toggle", methods=["POST"])
@login_required
@admin_role_required
def toggle_festival(festival_id):
    festival = Festival.query.get_or_404(festival_id)
    festival.is_active = not festival.is_active
    db.session.commit()
    return redirect(url_for("admin.festivals"))


@bp.route("/festivals/<int:festival_id>/edit", methods=["GET", "POST"])
@login_required
@admin_role_required
def festival_edit(festival_id):
    festival = Festival.query.get_or_404(festival_id)
    if request.method == "POST":
        new_name = request.form.get("name", "").strip()
        if not new_name:
            flash("Festival name can't be blank.")
            return redirect(url_for("admin.festival_edit", festival_id=festival_id))
        existing = Festival.query.filter(Festival.name == new_name, Festival.id != festival.id).first()
        if existing:
            flash(f"Another festival is already named '{new_name}'.")
            return redirect(url_for("admin.festival_edit", festival_id=festival_id))

        event_date_str = request.form.get("event_date")
        try:
            event_date = datetime.datetime.strptime(event_date_str, "%Y-%m-%d").date() if event_date_str else None
        except ValueError:
            flash("That date doesn't look right.")
            return redirect(url_for("admin.festival_edit", festival_id=festival_id))

        festival.name = new_name
        festival.event_date = event_date
        db.session.commit()
        flash(f"Festival '{festival.name}' updated.")
        return redirect(url_for("admin.festivals"))

    return render_template("admin/festival_edit.html", festival=festival)


@bp.route("/festivals/<int:festival_id>/delete", methods=["POST"])
@login_required
@admin_role_required
def festival_delete(festival_id):
    festival = Festival.query.get_or_404(festival_id)
    has_donations = Donation.query.filter_by(festival_id=festival.id).first() is not None
    if has_donations:
        flash(
            f"Can't delete '{festival.name}' -- it has donations recorded against it. "
            "Deactivate it instead to hide it from the Festival Seva form."
        )
        return redirect(url_for("admin.festivals"))

    db.session.delete(festival)
    db.session.commit()
    flash(f"Festival '{festival.name}' deleted.")
    return redirect(url_for("admin.festivals"))


@bp.route("/seva-types", methods=["GET", "POST"])
@login_required
def seva_types():
    if request.method == "POST":
        if current_user.role != "admin":
            flash("That action requires an administrator account.")
            return redirect(url_for("admin.seva_types"))
        name = request.form.get("name", "").strip()
        if not name:
            flash("Seva type name can't be blank.")
            return redirect(url_for("admin.seva_types"))
        if SevaType.query.filter_by(name=name).first():
            flash(f"A seva type named '{name}' already exists.")
            return redirect(url_for("admin.seva_types"))
        suggested_amount = float(request.form["suggested_amount"]) if request.form.get("suggested_amount") else None
        db.session.add(SevaType(name=name, suggested_amount=suggested_amount))
        db.session.commit()
        flash(f"Seva type '{name}' added.")
        return redirect(url_for("admin.seva_types"))

    seva_type_list = SevaType.query.order_by(SevaType.name).all()
    return render_template("admin/seva_types.html", seva_types=seva_type_list)


@bp.route("/seva-types/<int:seva_type_id>/toggle", methods=["POST"])
@login_required
@admin_role_required
def toggle_seva_type(seva_type_id):
    seva_type = SevaType.query.get_or_404(seva_type_id)
    seva_type.is_active = not seva_type.is_active
    db.session.commit()
    return redirect(url_for("admin.seva_types"))


@bp.route("/seva-types/<int:seva_type_id>/edit", methods=["GET", "POST"])
@login_required
@admin_role_required
def seva_type_edit(seva_type_id):
    seva_type = SevaType.query.get_or_404(seva_type_id)
    if request.method == "POST":
        new_name = request.form.get("name", "").strip()
        if not new_name:
            flash("Seva type name can't be blank.")
            return redirect(url_for("admin.seva_type_edit", seva_type_id=seva_type_id))
        existing = SevaType.query.filter(SevaType.name == new_name, SevaType.id != seva_type.id).first()
        if existing:
            flash(f"Another seva type is already named '{new_name}'.")
            return redirect(url_for("admin.seva_type_edit", seva_type_id=seva_type_id))

        seva_type.name = new_name
        seva_type.suggested_amount = (
            float(request.form["suggested_amount"]) if request.form.get("suggested_amount") else None
        )
        db.session.commit()
        flash(f"Seva type '{seva_type.name}' updated.")
        return redirect(url_for("admin.seva_types"))

    return render_template("admin/seva_type_edit.html", seva_type=seva_type)


@bp.route("/seva-types/<int:seva_type_id>/delete", methods=["POST"])
@login_required
@admin_role_required
def seva_type_delete(seva_type_id):
    seva_type = SevaType.query.get_or_404(seva_type_id)
    has_donations = Donation.query.filter_by(seva_type_id=seva_type.id).first() is not None
    if has_donations:
        flash(
            f"Can't delete '{seva_type.name}' -- it has donations recorded against it. "
            "Deactivate it instead to hide it from the Festival Seva form."
        )
        return redirect(url_for("admin.seva_types"))

    db.session.delete(seva_type)
    db.session.commit()
    flash(f"Seva type '{seva_type.name}' deleted.")
    return redirect(url_for("admin.seva_types"))


@bp.route("/live-to-give-purposes", methods=["GET", "POST"])
@login_required
def live_to_give_purposes():
    if request.method == "POST":
        if current_user.role != "admin":
            flash("That action requires an administrator account.")
            return redirect(url_for("admin.live_to_give_purposes"))
        name = request.form.get("name", "").strip()
        if not name:
            flash("Purpose name can't be blank.")
            return redirect(url_for("admin.live_to_give_purposes"))
        if LiveToGivePurpose.query.filter_by(name=name).first():
            flash(f"A donation purpose named '{name}' already exists.")
            return redirect(url_for("admin.live_to_give_purposes"))
        db.session.add(LiveToGivePurpose(name=name))
        db.session.commit()
        flash(f"Donation purpose '{name}' added.")
        return redirect(url_for("admin.live_to_give_purposes"))

    purpose_list = LiveToGivePurpose.query.order_by(LiveToGivePurpose.name).all()
    return render_template("admin/live_to_give_purposes.html", purposes=purpose_list)


@bp.route("/live-to-give-purposes/<int:purpose_id>/toggle", methods=["POST"])
@login_required
@admin_role_required
def toggle_live_to_give_purpose(purpose_id):
    purpose = LiveToGivePurpose.query.get_or_404(purpose_id)
    purpose.is_active = not purpose.is_active
    db.session.commit()
    return redirect(url_for("admin.live_to_give_purposes"))


@bp.route("/live-to-give-purposes/<int:purpose_id>/edit", methods=["GET", "POST"])
@login_required
@admin_role_required
def live_to_give_purpose_edit(purpose_id):
    purpose = LiveToGivePurpose.query.get_or_404(purpose_id)
    if request.method == "POST":
        new_name = request.form.get("name", "").strip()
        if not new_name:
            flash("Purpose name can't be blank.")
            return redirect(url_for("admin.live_to_give_purpose_edit", purpose_id=purpose_id))
        existing = LiveToGivePurpose.query.filter(
            LiveToGivePurpose.name == new_name, LiveToGivePurpose.id != purpose.id
        ).first()
        if existing:
            flash(f"Another donation purpose is already named '{new_name}'.")
            return redirect(url_for("admin.live_to_give_purpose_edit", purpose_id=purpose_id))

        purpose.name = new_name
        db.session.commit()
        flash(f"Donation purpose renamed to '{purpose.name}'.")
        return redirect(url_for("admin.live_to_give_purposes"))

    return render_template("admin/live_to_give_purpose_edit.html", purpose=purpose)


@bp.route("/live-to-give-purposes/<int:purpose_id>/delete", methods=["POST"])
@login_required
@admin_role_required
def live_to_give_purpose_delete(purpose_id):
    purpose = LiveToGivePurpose.query.get_or_404(purpose_id)
    has_donations = Donation.query.filter_by(live_to_give_purpose_id=purpose.id).first() is not None
    if has_donations:
        flash(
            f"Can't delete '{purpose.name}' -- it has donations recorded against it. "
            "Deactivate it instead to hide it from the Live To Give form."
        )
        return redirect(url_for("admin.live_to_give_purposes"))

    db.session.delete(purpose)
    db.session.commit()
    flash(f"Donation purpose '{purpose.name}' deleted.")
    return redirect(url_for("admin.live_to_give_purposes"))


@bp.route("/export/10bd")
@login_required
def export_10bd():
    fy = request.args.get("fy") or get_financial_year()
    rows = (
        db.session.query(Donation, Donor, Campaign)
        .join(Donor, Donation.donor_id == Donor.id)
        .join(Campaign, Donation.campaign_id == Campaign.id)
        .filter(Donation.status == "success", Donation.financial_year == fy)
        .order_by(Donation.donation_date)
        .all()
    )
    # Filtered in Python rather than at the query level (Campaign.is_80g)
    # because 80G-eligibility is per-donation for Live To Give (donor picks
    # 80G/Non-80G per donation) -- see Donation.effective_is_80g.
    rows = [(donation, donor, campaign) for donation, donor, campaign in rows if donation.effective_is_80g]

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Receipt No", "Date", "Donor Name", "Address", "City", "State", "Pincode",
        "PAN", "Amount", "Payment Mode", "Campaign",
    ])
    for donation, donor, campaign in rows:
        writer.writerow([
            donation.receipt_number,
            donation.donation_date.strftime("%d-%m-%Y"),
            donor.full_name,
            donor.address or "",
            donor.city or "",
            donor.state or "",
            donor.pincode or "",
            donor.pan or "",
            float(donation.amount),
            donation.payment_mode,
            campaign.name,
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename=Form10BD_data_{fy}.csv"},
    )


@bp.route("/export/collections")
@login_required
def export_collections():
    fy = request.args.get("fy") or get_financial_year()
    rows = (
        db.session.query(Donation, Donor, Campaign)
        .join(Donor, Donation.donor_id == Donor.id)
        .join(Campaign, Donation.campaign_id == Campaign.id)
        .filter(Donation.status == "success", Donation.financial_year == fy)
        .order_by(Donation.donation_date)
        .all()
    )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Receipt No", "Date", "Donor Name", "Phone", "WhatsApp", "Email", "PAN", "Amount",
        "Payment Mode", "Campaign", "80G Eligible",
    ])
    for donation, donor, campaign in rows:
        writer.writerow([
            donation.receipt_number,
            donation.donation_date.strftime("%d-%m-%Y"),
            donor.full_name,
            donor.phone or "",
            donor.whatsapp_number or "",
            donor.email or "",
            donor.pan or "",
            float(donation.amount),
            donation.payment_mode,
            campaign.name,
            "Yes" if donation.effective_is_80g else "No",
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename=Collections_{fy}.csv"},
    )


@bp.route("/export/monthly")
@login_required
def export_monthly_report():
    """Full monthly donor report -- every successful donation in the given
    calendar month, with the donor's complete details (contact, address,
    PAN) alongside the donation's own details (amount, mode, reference,
    campaign, receipt number, 80G eligibility). Broader than
    export_collections (which is FY-wide and has fewer donor fields) --
    this is meant to be handed off as one month's complete donor list,
    e.g. for a mailing list or an external audit request.

    Cancelled donations are excluded for free by the existing
    status == "success" filter (see Donation.status / the cancellation
    system), same as every other export/total in this file.
    """
    today = datetime.date.today()
    year = request.args.get("year", today.year, type=int)
    month = request.args.get("month", today.month, type=int)
    if not (1 <= month <= 12):
        month = today.month

    start = datetime.date(year, month, 1)
    end = datetime.date(year + (1 if month == 12 else 0), 1 if month == 12 else month + 1, 1)

    rows = (
        db.session.query(Donation, Donor, Campaign)
        .join(Donor, Donation.donor_id == Donor.id)
        .join(Campaign, Donation.campaign_id == Campaign.id)
        .filter(
            Donation.status == "success",
            Donation.donation_date >= start,
            Donation.donation_date < end,
        )
        .order_by(Donation.donation_date)
        .all()
    )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Receipt No", "Date", "Donor Name", "Phone", "WhatsApp", "Email", "PAN",
        "Address", "City", "State", "Pincode",
        "Amount", "Payment Mode", "Reference", "Campaign", "80G Eligible", "Recorded By",
    ])
    for donation, donor, campaign in rows:
        writer.writerow([
            donation.receipt_number,
            donation.donation_date.strftime("%d-%m-%Y"),
            donor.full_name,
            donor.phone or "",
            donor.whatsapp_number or "",
            donor.email or "",
            donor.pan or "",
            donor.address or "",
            donor.city or "",
            donor.state or "",
            donor.pincode or "",
            float(donation.amount),
            donation.payment_mode,
            donation.reference_display or "",
            campaign.name,
            "Yes" if donation.effective_is_80g else "No",
            donation.recorded_by or "",
        ])

    month_label = start.strftime("%Y-%m")
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename=Monthly_Donor_Report_{month_label}.csv"},
    )


@bp.route("/donors/lapsed")
@login_required
def lapsed_donors():
    """Donors who gave in each of the last 3 months before last month, but
    NOT last month -- i.e. likely-recurring donors who may need a follow-up."""
    today = datetime.date.today()

    def month_key(d):
        return (d.year, d.month)

    def months_ago(base, i):
        total_months = base.year * 12 + (base.month - 1) - i
        y, m = divmod(total_months, 12)
        return datetime.date(y, m + 1, 1)

    start_of_this_month = today.replace(day=1)
    months_back = [month_key(months_ago(start_of_this_month, i)) for i in range(1, 5)]
    last_month_key = months_back[0]

    donors_all = Donor.query.all()
    lapsed = []
    for donor in donors_all:
        months_donated = set()
        for d in donor.donations.filter_by(status="success"):
            months_donated.add(month_key(d.donation_date.date()))
        prior_three = set(months_back[1:4])
        if prior_three.issubset(months_donated) and last_month_key not in months_donated:
            lapsed.append(donor)

    return render_template("admin/lapsed_donors.html", donors=lapsed)
