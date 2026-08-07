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
from models import Donor, Campaign, Donation, AdminUser, ReceiptCounter, BaceProperty, Festival, SevaType
from utils import get_financial_year, is_valid_pan
from pdf_utils import generate_receipt_pdf
from email_utils import send_receipt_email
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
    query = Donation.query.filter_by(status="success")
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
        campaign_id=campaign_id, mode=mode,
    )


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
            remarks=form.get("remarks"),
            recorded_by=current_user.username,
        )
        db.session.add(donation)
        db.session.flush()

        receipt_number, fy = ReceiptCounter.next_receipt_number(campaign.is_80g, donation_date)
        donation.receipt_number = receipt_number
        donation.financial_year = fy
        db.session.commit()

        pdf_bytes = generate_receipt_pdf(donation, donor, campaign, _org_cfg())
        donation.receipt_pdf = pdf_bytes
        db.session.commit()
        send_receipt_email(donation, donor, _org_cfg(), pdf_bytes)

        flash(f"Donation recorded. Receipt {receipt_number} generated.")
        return redirect(url_for("admin.donor_detail", donor_id=donor.id))

    return render_template(
        "admin/manual_donation.html", campaigns=campaigns, bace_properties=bace_properties,
        festivals=festivals, seva_types=seva_types, today=datetime.date.today(),
    )


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


@bp.route("/export/10bd")
@login_required
def export_10bd():
    fy = request.args.get("fy") or get_financial_year()
    rows = (
        db.session.query(Donation, Donor, Campaign)
        .join(Donor, Donation.donor_id == Donor.id)
        .join(Campaign, Donation.campaign_id == Campaign.id)
        .filter(Donation.status == "success", Donation.financial_year == fy, Campaign.is_80g.is_(True))
        .order_by(Donation.donation_date)
        .all()
    )

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
            "Yes" if campaign.is_80g else "No",
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename=Collections_{fy}.csv"},
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
