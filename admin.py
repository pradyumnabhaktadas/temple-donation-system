import csv
import io
import os
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
    LiveToGivePurpose, Preacher, DONOR_TYPES, DONOR_TYPE_LABELS, DONATION_FREQUENCIES,
    DONATION_FREQUENCY_LABELS,
)
from utils import get_financial_year, is_valid_pan
from pdf_utils import generate_receipt_pdf
from email_utils import send_receipt_email
from whatsapp_utils import send_receipt_whatsapp
from public import find_or_create_donor, _org_cfg
from backup_utils import build_backup_zip

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

    # Birthday reminder banner -- today's only; the full calendar with
    # upcoming birthdays lives on its own page (Admin -> Donors -> Birthdays,
    # see the birthdays() route below). Reuses the same Feb-29/year-
    # wraparound-safe date math as Donor Insights and Analytics
    # (_next_occurrence, defined further down in this file).
    birthdays_today = []
    for donor in Donor.query.filter(Donor.dob.isnot(None)).all():
        days_until, _occurrence_date = _next_occurrence(donor.dob, today)
        if days_until == 0:
            birthdays_today.append({"donor": donor})

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
        birthdays_today=birthdays_today,
        today=today,
    )


def _months_ago(base, i):
    """Calendar-correct 'i months before base', avoiding drift from fixed
    30-day subtraction across months of different lengths."""
    total_months = base.year * 12 + (base.month - 1) - i
    y, m = divmod(total_months, 12)
    return datetime.date(y, m + 1, 1)


def _apply_donor_population_filters(query, donor_type, preacher_id, frequency):
    """Shared donor_type/connected_preacher/donation_frequency filtering,
    used by both the Donors list and Analytics -- `query` must have Donor
    columns in scope (either Donor.query directly, or a query that's
    joined Donor in). "none" for preacher_id is a sentinel meaning "no
    preacher assigned" (IS NULL), distinct from a blank/absent param
    ("any preacher")."""
    if donor_type:
        query = query.filter(Donor.donor_type == donor_type)
    if preacher_id == "none":
        query = query.filter(Donor.connected_preacher_id.is_(None))
    elif preacher_id:
        try:
            query = query.filter(Donor.connected_preacher_id == int(preacher_id))
        except ValueError:
            pass
    if frequency:
        query = query.filter(Donor.donation_frequency == frequency)
    return query


# Retention thresholds (days since last successful donation) -- a donor's
# frequency label (set manually by staff, see Donor.donation_frequency)
# determines when a gap starts looking like a missed gift vs. business as
# usual. These are reasonable defaults, not a spec from the temple --
# tune freely if real-world follow-up patterns suggest otherwise.
_RETENTION_THRESHOLDS = {
    "monthly": (35, 60),      # active <=35 days, at-risk 36-60, inactive >60
    "quarterly": (100, 150),
    "yearly": (380, 450),
    None: (180, 365),         # occasional / one_time / not set
}
for _f in ("occasional", "one_time"):
    _RETENTION_THRESHOLDS[_f] = _RETENTION_THRESHOLDS[None]


def _donor_activity_status(frequency, last_donation_date, today):
    """One of "never", "active", "at_risk", "inactive" for a donor, based
    on how long it's been since their last successful donation relative
    to how often they're expected to give."""
    if last_donation_date is None:
        return "never"
    days_since = (today - last_donation_date).days
    at_risk_after, inactive_after = _RETENTION_THRESHOLDS.get(frequency, _RETENTION_THRESHOLDS[None])
    if days_since <= at_risk_after:
        return "active"
    elif days_since <= inactive_after:
        return "at_risk"
    return "inactive"


def _resolve_analytics_date_range(preset, custom_from_raw, custom_to_raw, today):
    """Resolves the Analytics page's date-range filter to a [start, end)
    pair of dates. "this_quarter"/"this_year" are to-date (like the
    Dashboard's FY total) rather than the full period including days that
    haven't happened yet; "last_month" is the one preset that's a fully
    closed period."""
    if preset == "last_month":
        end_of_last_month = today.replace(day=1) - datetime.timedelta(days=1)
        start = end_of_last_month.replace(day=1)
        end = today.replace(day=1)
        label = end_of_last_month.strftime("%B %Y")
    elif preset == "this_quarter":
        q_start_month = ((today.month - 1) // 3) * 3 + 1
        start = today.replace(month=q_start_month, day=1)
        end = today + datetime.timedelta(days=1)
        label = f"This Quarter (from {start.strftime('%d-%b')})"
    elif preset == "this_year":
        fy = get_financial_year(today)
        start = datetime.date(int(fy.split("-")[0]), 4, 1)
        end = today + datetime.timedelta(days=1)
        label = f"FY {fy} to date"
    elif preset == "custom":
        try:
            start = datetime.datetime.strptime(custom_from_raw, "%Y-%m-%d").date() if custom_from_raw else today - datetime.timedelta(days=30)
        except ValueError:
            start = today - datetime.timedelta(days=30)
        try:
            custom_to = datetime.datetime.strptime(custom_to_raw, "%Y-%m-%d").date() if custom_to_raw else today
        except ValueError:
            custom_to = today
        end = custom_to + datetime.timedelta(days=1)
        label = f"{start.strftime('%d-%b-%Y')} to {custom_to.strftime('%d-%b-%Y')}"
    else:
        preset = "this_month"
        start = today.replace(day=1)
        end = today + datetime.timedelta(days=1)
        label = today.strftime("%B %Y")
    return preset, start, end, label


@bp.route("/analytics")
@login_required
def analytics():
    """Analytics built around the four things that actually matter for
    running an IYF/Live To Give donor program: how much is coming in
    (Donations), how the donor base is growing (Donors), how relationships
    are distributed (Preachers), and who needs attention right now
    (Follow-up) -- not just a running total.

    Implementation note: rather than issuing a separate SQL query per
    section, this pulls the (small, single-temple-scale) filtered donor
    list and their full donation history once each, then does every
    breakdown/aggregate in Python. That keeps ~12 report sections easy to
    verify and keeps the SQL portable (no dialect-specific DISTINCT ON /
    correlated-subquery tricks needed across SQLite dev / Postgres prod).
    """
    today = datetime.date.today()

    # -- Global filters (apply across every section) --
    date_preset = request.args.get("date_preset", "this_month")
    custom_from_raw = request.args.get("date_from", "")
    custom_to_raw = request.args.get("date_to", "")
    date_preset, period_start, period_end, period_label = _resolve_analytics_date_range(
        date_preset, custom_from_raw, custom_to_raw, today
    )

    donor_type_filter = request.args.get("donor_type", "")
    preacher_filter = request.args.get("preacher_id", "")
    frequency_filter = request.args.get("donation_frequency", "")

    donors_all = _apply_donor_population_filters(
        Donor.query, donor_type_filter, preacher_filter, frequency_filter
    ).all()
    population_donor_ids = [d.id for d in donors_all]
    donor_by_id = {d.id: d for d in donors_all}
    all_preachers = Preacher.query.order_by(Preacher.name).all()
    preacher_name_by_id = {p.id: p.name for p in all_preachers}

    trend_granularity = request.args.get("trend", "monthly")
    if trend_granularity not in ("monthly", "quarterly", "yearly"):
        trend_granularity = "monthly"
    top_scope = request.args.get("top_scope", "lifetime")
    if top_scope not in ("lifetime", "period"):
        top_scope = "lifetime"

    # Every successful donation by a population-filtered donor, pulled
    # once as (donor_id, amount, plain date) tuples -- everything below
    # slices this same list rather than re-querying.
    all_pop_donations = []
    if population_donor_ids:
        for did, amt, dt in (
            db.session.query(Donation.donor_id, Donation.amount, Donation.donation_date)
            .filter(Donation.status == "success", Donation.donor_id.in_(population_donor_ids))
            .all()
        ):
            d = dt.date() if hasattr(dt, "date") else dt
            all_pop_donations.append((did, float(amt), d))

    period_donation_rows = [(did, amt) for did, amt, d in all_pop_donations if period_start <= d < period_end]
    total_donations_amount = sum(amt for _, amt in period_donation_rows)
    active_donor_ids = {did for did, _ in period_donation_rows}
    donation_count_in_period = len(period_donation_rows)
    avg_donation_in_period = (total_donations_amount / donation_count_in_period) if donation_count_in_period else 0

    this_month_start = today.replace(day=1)
    this_month_total = sum(amt for _, amt, d in all_pop_donations if d >= this_month_start)

    first_donation_date, last_donation_date = {}, {}
    for did, amt, d in all_pop_donations:
        if did not in first_donation_date or d < first_donation_date[did]:
            first_donation_date[did] = d
        if did not in last_donation_date or d > last_donation_date[did]:
            last_donation_date[did] = d

    new_donors_count = sum(1 for d in first_donation_date.values() if period_start <= d < period_end)

    def _created_before(donor, cutoff_date):
        created = donor.created_at.date() if donor.created_at else datetime.date.min
        return created < cutoff_date

    # ================= 1. KPI CARDS =================
    kpis = {
        "total_donors": sum(1 for d in donors_all if _created_before(d, period_end)),
        "active_donors": len(active_donor_ids),
        "total_donations": total_donations_amount,
        "this_month": this_month_total,
        "monthly_recurring_donors": sum(
            1 for d in donors_all if d.donation_frequency == "monthly" and _created_before(d, period_end)
        ),
        "new_donors": new_donors_count,
        "avg_donation": avg_donation_in_period,
        "preacher_count": len({d.connected_preacher_id for d in donors_all if d.connected_preacher_id}),
    }

    # ================= 2. DONATION TREND =================
    trend_buckets = []
    if trend_granularity == "monthly":
        for i in range(11, -1, -1):
            bucket_start = _months_ago(today.replace(day=1), i)
            bucket_end = _months_ago(today.replace(day=1), i - 1)
            trend_buckets.append((bucket_start.strftime("%b %Y"), bucket_start, bucket_end))
    elif trend_granularity == "quarterly":
        this_q_start_month = ((today.month - 1) // 3) * 3 + 1
        this_q_start = today.replace(month=this_q_start_month, day=1)
        for i in range(7, -1, -1):
            bucket_start = _months_ago(this_q_start, i * 3)
            bucket_end = _months_ago(this_q_start, (i - 1) * 3)
            q_num = (bucket_start.month - 1) // 3 + 1
            trend_buckets.append((f"Q{q_num} {bucket_start.year}", bucket_start, bucket_end))
    else:  # yearly (financial years)
        current_fy_start_year = int(get_financial_year(today).split("-")[0])
        for offset in range(4, -1, -1):
            fy_start_year = current_fy_start_year - offset
            bucket_start = datetime.date(fy_start_year, 4, 1)
            bucket_end = datetime.date(fy_start_year + 1, 4, 1)
            trend_buckets.append((f"FY {fy_start_year}-{str(fy_start_year + 1)[-2:]}", bucket_start, bucket_end))

    donation_trend = []
    for label, bstart, bend in trend_buckets:
        bucket_rows = [amt for _, amt, d in all_pop_donations if bstart <= d < bend]
        donation_trend.append({"label": label, "total": sum(bucket_rows), "count": len(bucket_rows)})

    # ================= 3. DONOR TYPE ANALYTICS =================
    donor_amount_by_id = {}
    for did, amt in period_donation_rows:
        donor_amount_by_id[did] = donor_amount_by_id.get(did, 0) + amt

    type_totals = {t: {"donors": 0, "amount": 0.0} for t in DONOR_TYPES}
    for d in donors_all:
        if d.donor_type in type_totals:
            type_totals[d.donor_type]["donors"] += 1
            type_totals[d.donor_type]["amount"] += donor_amount_by_id.get(d.id, 0)
    grand_total_amount = sum(v["amount"] for v in type_totals.values()) or 1.0
    donor_type_breakdown = [
        {
            "type": t, "label": DONOR_TYPE_LABELS[t],
            "donors": type_totals[t]["donors"], "amount": type_totals[t]["amount"],
            "pct": round(type_totals[t]["amount"] / grand_total_amount * 100, 1),
        }
        for t in DONOR_TYPES
    ]

    # ================= 4. DONATION FREQUENCY BREAKDOWN =================
    freq_totals = {f: {"donors": 0, "amount": 0.0} for f in DONATION_FREQUENCIES}
    for d in donors_all:
        if d.donation_frequency in freq_totals:
            freq_totals[d.donation_frequency]["donors"] += 1
            freq_totals[d.donation_frequency]["amount"] += donor_amount_by_id.get(d.id, 0)
    frequency_breakdown = [
        {
            "freq": f, "label": DONATION_FREQUENCY_LABELS[f],
            "donors": freq_totals[f]["donors"], "amount": freq_totals[f]["amount"],
        }
        for f in DONATION_FREQUENCIES
    ]

    # ================= 5. PREACHER-WISE PERFORMANCE =================
    donors_by_preacher = {}
    for d in donors_all:
        donors_by_preacher.setdefault(d.connected_preacher_id, []).append(d)

    preacher_performance = []
    for p in all_preachers:
        p_donors = donors_by_preacher.get(p.id, [])
        if not p_donors:
            continue
        p_ids = {d.id for d in p_donors}
        lifetime_rows = [amt for did, amt, d in all_pop_donations if did in p_ids]
        lifetime_total = sum(lifetime_rows)
        lifetime_count = len(lifetime_rows)
        preacher_performance.append({
            "id": p.id, "name": p.name, "donors": len(p_donors),
            "active_donors": sum(1 for did in p_ids if did in active_donor_ids),
            "period_total": sum(amt for did, amt in period_donation_rows if did in p_ids),
            "lifetime_total": lifetime_total,
            "monthly_donors": sum(1 for d in p_donors if d.donation_frequency == "monthly"),
            "avg_donation": (lifetime_total / lifetime_count) if lifetime_count else 0,
        })
    preacher_performance.sort(key=lambda r: r["lifetime_total"], reverse=True)

    # ================= 6. DONOR GROWTH =================
    fy_start_date = datetime.date(int(get_financial_year(today).split("-")[0]), 4, 1)
    q_start_month = ((today.month - 1) // 3) * 3 + 1
    q_start_date = today.replace(month=q_start_month, day=1)
    month_start_date = today.replace(day=1)

    new_this_month = sum(1 for d in first_donation_date.values() if d >= month_start_date)
    new_this_quarter = sum(1 for d in first_donation_date.values() if d >= q_start_date)
    new_this_year = sum(1 for d in first_donation_date.values() if d >= fy_start_date)

    growth_trend = []
    for i in range(11, -1, -1):
        bucket_start = _months_ago(today.replace(day=1), i)
        bucket_end = _months_ago(today.replace(day=1), i - 1)
        count = sum(1 for d in first_donation_date.values() if bucket_start <= d < bucket_end)
        growth_trend.append({"label": bucket_start.strftime("%b %Y"), "new": count})

    prev_period_len = (period_end - period_start).days
    prev_period_start = period_start - datetime.timedelta(days=prev_period_len)
    prev_period_new = sum(1 for d in first_donation_date.values() if prev_period_start <= d < period_start)
    donor_growth_pct = None
    if prev_period_new > 0:
        donor_growth_pct = round((new_donors_count - prev_period_new) / prev_period_new * 100, 1)

    # ================= 7. DONOR RETENTION =================
    retention_counts = {"active": 0, "at_risk": 0, "inactive": 0, "never": 0}
    at_risk_list = []
    for donor in donors_all:
        last_date = last_donation_date.get(donor.id)
        status = _donor_activity_status(donor.donation_frequency, last_date, today)
        retention_counts[status] += 1
        if status in ("at_risk", "inactive"):
            at_risk_list.append({
                "donor": donor, "status": status, "last_donation": last_date,
                "days_since": (today - last_date).days if last_date else None,
            })
    at_risk_list.sort(key=lambda r: r["days_since"] or 0, reverse=True)

    # ================= 8. BIRTHDAYS & ANNIVERSARIES =================
    donors_with_dates = [
        d for d in donors_all
        if d.dob or d.father_dob or d.mother_dob or d.wife_dob or d.marriage_anniversary
    ]
    upcoming_30 = []
    for donor in donors_with_dates:
        for label, date_value in [
            ("Donor's Birthday", donor.dob), ("Marriage Anniversary", donor.marriage_anniversary),
            ("Father's Birthday", donor.father_dob), ("Mother's Birthday", donor.mother_dob),
            ("Wife's Birthday", donor.wife_dob),
        ]:
            if not date_value:
                continue
            days_until, occurrence_date = _next_occurrence(date_value, today)
            if days_until is not None and days_until <= 30:
                upcoming_30.append({"donor": donor, "label": label, "days_until": days_until, "date": occurrence_date})
    upcoming_30.sort(key=lambda u: u["days_until"])
    upcoming_7 = [u for u in upcoming_30 if u["days_until"] <= 7]

    birthday_month_counts = {
        "Donor's Birthday": 0, "Father's Birthday": 0, "Mother's Birthday": 0,
        "Wife's Birthday": 0, "Marriage Anniversary": 0,
    }
    for u in upcoming_30:
        birthday_month_counts[u["label"]] += 1

    # ================= 9. GEOGRAPHIC ANALYTICS =================
    geo = {}
    for d in donors_all:
        state = (d.state or "").strip() or "Unknown"
        geo.setdefault(state, {"donors": 0, "amount": 0.0})
        geo[state]["donors"] += 1
    for did, amt in period_donation_rows:
        d = donor_by_id.get(did)
        state = ((d.state or "").strip() or "Unknown") if d else "Unknown"
        geo.setdefault(state, {"donors": 0, "amount": 0.0})
        geo[state]["amount"] += amt
    geography = sorted(
        [{"state": s, "donors": v["donors"], "amount": v["amount"]} for s, v in geo.items()],
        key=lambda r: r["amount"], reverse=True,
    )

    # ================= 10. DATA COMPLETENESS =================
    total_pop = len(donors_all) or 1
    with_pan = sum(1 for d in donors_all if d.pan)
    with_address = sum(1 for d in donors_all if d.address and d.city and d.state and d.pincode)
    with_dob = sum(1 for d in donors_all if d.dob)
    with_preacher = sum(1 for d in donors_all if d.connected_preacher_id)
    incomplete_count = 0
    for d in donors_all:
        missing = sum([
            not d.pan, not (d.address and d.city and d.state and d.pincode),
            not d.dob, not d.connected_preacher_id, not d.donor_type,
        ])
        if missing >= 2:
            incomplete_count += 1
    completeness = {
        "pan_pct": round(with_pan / total_pop * 100, 1),
        "address_pct": round(with_address / total_pop * 100, 1),
        "dob_pct": round(with_dob / total_pop * 100, 1),
        "preacher_pct": round(with_preacher / total_pop * 100, 1),
        "overall_pct": round((with_pan + with_address + with_dob + with_preacher) / (total_pop * 4) * 100, 1),
        "incomplete_count": incomplete_count,
    }

    # ================= 11. FOLLOW-UP DASHBOARD =================
    overdue_count = sum(
        1 for r in at_risk_list if r["donor"].donation_frequency in ("monthly", "quarterly", "yearly")
    )
    unassigned_count = sum(1 for d in donors_all if not d.connected_preacher_id)
    new_without_preacher = sum(
        1 for did, fdate in first_donation_date.items()
        if (today - fdate).days <= 30 and donor_by_id.get(did) and not donor_by_id[did].connected_preacher_id
    )
    lifetime_totals_by_donor = {}
    for did, amt, d in all_pop_donations:
        lifetime_totals_by_donor[did] = lifetime_totals_by_donor.get(did, 0) + amt
    sorted_totals = sorted(lifetime_totals_by_donor.values(), reverse=True)
    high_value_cutoff = sorted_totals[9] if len(sorted_totals) >= 10 else (sorted_totals[-1] if sorted_totals else 0)
    high_value_at_risk = [
        r for r in at_risk_list
        if high_value_cutoff > 0 and lifetime_totals_by_donor.get(r["donor"].id, 0) >= high_value_cutoff
    ][:10]

    followup = {
        "overdue_count": overdue_count,
        "unassigned_count": unassigned_count,
        "new_without_preacher": new_without_preacher,
        "inactive_count": retention_counts["inactive"],
        "birthdays_week_count": sum(1 for u in upcoming_7 if "Birthday" in u["label"]),
        "anniversaries_week_count": sum(1 for u in upcoming_7 if u["label"] == "Marriage Anniversary"),
        "incomplete_count": completeness["incomplete_count"],
        "high_value_at_risk": high_value_at_risk,
    }

    # ================= 12. TOP DONORS =================
    totals_source = period_donation_rows if top_scope == "period" else [(did, amt) for did, amt, d in all_pop_donations]
    totals_by_donor, counts_by_donor = {}, {}
    for did, amt in totals_source:
        totals_by_donor[did] = totals_by_donor.get(did, 0) + amt
        counts_by_donor[did] = counts_by_donor.get(did, 0) + 1
    top_ids = sorted(totals_by_donor, key=lambda did: totals_by_donor[did], reverse=True)[:10]
    top_donors = [
        {
            "id": did, "name": donor_by_id[did].full_name,
            "type": DONOR_TYPE_LABELS.get(donor_by_id[did].donor_type, "-"),
            "preacher": preacher_name_by_id.get(donor_by_id[did].connected_preacher_id, "-"),
            "total": totals_by_donor[did], "count": counts_by_donor[did],
        }
        for did in top_ids if did in donor_by_id
    ]

    return render_template(
        "admin/analytics.html",
        date_preset=date_preset, custom_from=custom_from_raw, custom_to=custom_to_raw, period_label=period_label,
        donor_type_filter=donor_type_filter, preacher_filter=preacher_filter, frequency_filter=frequency_filter,
        donor_types=DONOR_TYPES, donor_type_labels=DONOR_TYPE_LABELS,
        donation_frequencies=DONATION_FREQUENCIES, donation_frequency_labels=DONATION_FREQUENCY_LABELS,
        all_preachers=all_preachers,
        kpis=kpis,
        trend_granularity=trend_granularity, donation_trend=donation_trend,
        donor_type_breakdown=donor_type_breakdown,
        frequency_breakdown=frequency_breakdown,
        preacher_performance=preacher_performance,
        new_this_month=new_this_month, new_this_quarter=new_this_quarter, new_this_year=new_this_year,
        donor_growth_pct=donor_growth_pct, growth_trend=growth_trend,
        retention_counts=retention_counts, at_risk_list=at_risk_list[:25],
        upcoming_7=upcoming_7, upcoming_30=upcoming_30[:25], birthday_month_counts=birthday_month_counts,
        geography=geography,
        completeness=completeness,
        followup=followup,
        top_donors=top_donors, top_scope=top_scope,
    )


DONORS_PER_PAGE = 30
DONATIONS_PER_PAGE = 50


@bp.route("/donors")
@login_required
def donors():
    q = request.args.get("q", "").strip()
    donor_type = request.args.get("donor_type", "")
    # "none" is a sentinel meaning "no preacher assigned" (connected_preacher_id
    # IS NULL) -- distinct from a blank/absent param, which means "any".
    preacher_id = request.args.get("preacher_id", "")
    donation_frequency = request.args.get("donation_frequency", "")
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
    if donor_type:
        query = query.filter(Donor.donor_type == donor_type)
    if preacher_id == "none":
        query = query.filter(Donor.connected_preacher_id.is_(None))
    elif preacher_id:
        try:
            query = query.filter(Donor.connected_preacher_id == int(preacher_id))
        except ValueError:
            pass
    if donation_frequency:
        query = query.filter(Donor.donation_frequency == donation_frequency)
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

    preachers_list = Preacher.query.order_by(Preacher.name).all()
    return render_template(
        "admin/donors.html", donors=pagination.items, pagination=pagination, totals=totals, q=q,
        donor_type=donor_type, preacher_id=preacher_id, donation_frequency=donation_frequency,
        preachers=preachers_list, donor_types=DONOR_TYPES, donor_type_labels=DONOR_TYPE_LABELS,
        donation_frequencies=DONATION_FREQUENCIES, donation_frequency_labels=DONATION_FREQUENCY_LABELS,
    )


@bp.route("/donors/<int:donor_id>")
@login_required
def donor_detail(donor_id):
    donor = Donor.query.get_or_404(donor_id)
    donations = donor.donations.order_by(Donation.donation_date.desc()).all()
    available_fys = sorted({d.financial_year for d in donations if d.financial_year and d.status == "success"}, reverse=True)
    return render_template(
        "admin/donor_detail.html", donor=donor, donations=donations, available_fys=available_fys,
        donor_type_labels=DONOR_TYPE_LABELS, donation_frequency_labels=DONATION_FREQUENCY_LABELS,
    )


def _parse_optional_date(form, key):
    """Parses an optional <input type=date> field (YYYY-MM-DD). Returns
    None for blank or malformed input rather than raising -- these are
    all "nice to have" relationship-building fields (DOB, anniversary,
    etc.), not required data, so a typo'd date shouldn't block saving the
    rest of the donor's details."""
    raw = (form.get(key) or "").strip()
    if not raw:
        return None
    try:
        return datetime.datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


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

        # -- Relationship-management fields --
        donor_type = form.get("donor_type") or ""
        donor.donor_type = donor_type if donor_type in DONOR_TYPES else None

        donation_frequency = form.get("donation_frequency") or ""
        donor.donation_frequency = donation_frequency if donation_frequency in DONATION_FREQUENCIES else None

        preacher_id = None
        raw_preacher_id = form.get("connected_preacher_id")
        if raw_preacher_id:
            try:
                candidate = int(raw_preacher_id)
                if Preacher.query.get(candidate):
                    preacher_id = candidate
            except ValueError:
                pass
        donor.connected_preacher_id = preacher_id

        donor.gifts = (form.get("gifts") or "").strip()[:500] or None
        donor.additional_info = (form.get("additional_info") or "").strip() or None
        donor.dob = _parse_optional_date(form, "dob")
        donor.father_dob = _parse_optional_date(form, "father_dob")
        donor.mother_dob = _parse_optional_date(form, "mother_dob")
        donor.wife_dob = _parse_optional_date(form, "wife_dob")
        donor.marriage_anniversary = _parse_optional_date(form, "marriage_anniversary")

        db.session.commit()
        flash("Donor details updated.")
        return redirect(url_for("admin.donor_detail", donor_id=donor.id))

    preachers_list = Preacher.query.filter_by(is_active=True).order_by(Preacher.name).all()
    if donor.connected_preacher and not donor.connected_preacher.is_active:
        # Keep the currently-assigned preacher selectable even if they've
        # since been deactivated -- otherwise saving this form with no
        # other changes would silently clear the assignment.
        preachers_list = sorted(preachers_list + [donor.connected_preacher], key=lambda p: p.name)
    return render_template(
        "admin/donor_edit.html", donor=donor, preachers=preachers_list,
        donor_types=DONOR_TYPES, donor_type_labels=DONOR_TYPE_LABELS,
        donation_frequencies=DONATION_FREQUENCIES, donation_frequency_labels=DONATION_FREQUENCY_LABELS,
    )


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


def _apply_donations_filters(query):
    """Shared campaign/mode/status/date-range filtering for the Donations
    Log page and its CSV export, kept in one place so the export can never
    drift out of sync with what's actually on screen (it's meant to be
    "everything currently filtered", just without pagination).

    Returns (query, filters) where filters is a dict of the resolved
    values, ready to splice straight into the template context.
    """
    # Defaults to "success" (the historical behaviour every other caller of
    # this route already relies on) but a status=... query param lets staff
    # pull up cancelled donations too, or "all" to see everything mixed
    # together -- otherwise a cancelled donation would just silently vanish
    # from this list with no way to find it again.
    status = request.args.get("status", "success")
    if status != "all":
        query = query.filter_by(status=status)

    campaign_id = request.args.get("campaign_id", type=int)
    if campaign_id:
        query = query.filter_by(campaign_id=campaign_id)

    mode = request.args.get("mode")
    if mode:
        query = query.filter_by(payment_mode=mode)

    date_from_raw = request.args.get("date_from") or ""
    date_to_raw = request.args.get("date_to") or ""
    try:
        if date_from_raw:
            date_from = datetime.datetime.strptime(date_from_raw, "%Y-%m-%d")
            query = query.filter(Donation.donation_date >= date_from)
        if date_to_raw:
            date_to = datetime.datetime.strptime(date_to_raw, "%Y-%m-%d")
            # donation_date carries a real time-of-day for online donations
            # (see below), so a plain <= comparison against midnight of the
            # "to" date would silently exclude every online donation made
            # later that same day. Push the upper bound to the start of the
            # next day instead.
            query = query.filter(Donation.donation_date < date_to + datetime.timedelta(days=1))
    except ValueError:
        date_from_raw = date_to_raw = ""

    return query, {
        "status": status, "campaign_id": campaign_id, "mode": mode,
        "date_from": date_from_raw, "date_to": date_to_raw,
    }


# Column-header sorting for the Donations Log. Sort key is a plain column
# name ("amount") for ascending, or the same name prefixed with "-" for
# descending ("-amount") -- mirrors the convention already used by
# Django/DRF-style APIs so it reads naturally in a URL. Donor/Campaign
# sort by the related table's name rather than the foreign key id, which
# needs a join.
_DONATIONS_SORT_COLUMNS = {"date", "donor", "campaign", "amount", "status"}


def _sorted_donations_query(query, sort_key):
    descending = sort_key.startswith("-")
    key = sort_key[1:] if descending else sort_key

    if key not in _DONATIONS_SORT_COLUMNS:
        # Unrecognised/typo'd sort param -- fall back to the historical
        # default rather than erroring the page out.
        key, descending = "date", True

    if key == "donor":
        query = query.join(Donor, Donation.donor_id == Donor.id)
        column = Donor.full_name
    elif key == "campaign":
        query = query.join(Campaign, Donation.campaign_id == Campaign.id)
        column = Campaign.name
    elif key == "amount":
        column = Donation.amount
    elif key == "status":
        column = Donation.status
    else:
        column = Donation.donation_date

    query = query.order_by(column.desc() if descending else column.asc())
    resolved_sort = ("-" if descending else "") + key
    return query, resolved_sort


@bp.route("/donations")
@login_required
def donations():
    query, filters = _apply_donations_filters(Donation.query)
    query, sort = _sorted_donations_query(query, request.args.get("sort", "-date"))
    page = request.args.get("page", 1, type=int)
    pagination = db.paginate(query, page=page, per_page=DONATIONS_PER_PAGE, error_out=False)
    campaigns = Campaign.query.order_by(Campaign.name).all()
    return render_template(
        "admin/donations.html", donations=pagination.items, pagination=pagination, campaigns=campaigns,
        sort=sort, **filters,
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


def _offline_donation_form_context():
    """Dropdown data the Single Entry tab needs -- shared between
    manual_donation() and bulk_import_donations() since both routes can
    render the merged offline_donation.html page (Single Entry + Bulk
    Upload live on one page/one nav tab now)."""
    return {
        "campaigns": Campaign.query.filter_by(is_active=True).order_by(Campaign.name).all(),
        "bace_properties": BaceProperty.query.filter_by(is_active=True).order_by(BaceProperty.name).all(),
        "festivals": Festival.query.filter_by(is_active=True).order_by(Festival.name).all(),
        "seva_types": SevaType.query.filter_by(is_active=True).order_by(SevaType.name).all(),
        "live_to_give_purposes": LiveToGivePurpose.query.filter_by(is_active=True).order_by(LiveToGivePurpose.name).all(),
        "today": datetime.date.today(),
    }


@bp.route("/donations/manual", methods=["GET", "POST"])
@login_required
def manual_donation():
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

    active_tab = "bulk" if request.args.get("tab") == "bulk" else "single"
    return render_template(
        "admin/offline_donation.html", active_tab=active_tab,
        bulk_results=None, bulk_created=None, bulk_skipped=None,
        **_offline_donation_form_context(),
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
        # Single Entry and Bulk Upload live on one merged page/nav tab now
        # (offline_donation.html, served by manual_donation()) -- this
        # route still owns the actual upload processing (POST below), but
        # a direct GET here just lands on that page with Bulk selected.
        return redirect(url_for("admin.manual_donation", tab="bulk"))

    file = request.files.get("csv_file")
    if not file or not file.filename:
        flash("Please choose a CSV file to upload.")
        return redirect(url_for("admin.manual_donation", tab="bulk"))

    send_notifications = request.form.get("send_notifications") == "yes"

    try:
        stream = io.TextIOWrapper(file.stream, encoding="utf-8-sig")
        reader = csv.DictReader(stream)
        fieldnames = {(f or "").strip() for f in (reader.fieldnames or [])}
    except Exception:
        flash("Couldn't read that file -- please upload a CSV (comma-separated) file.")
        return redirect(url_for("admin.manual_donation", tab="bulk"))

    missing = [c for c in BULK_IMPORT_REQUIRED_COLUMNS if c not in fieldnames]
    if missing:
        flash(
            "That CSV is missing required column(s): " + ", ".join(missing)
            + ". Download the demo file below for the full column list."
        )
        return redirect(url_for("admin.manual_donation", tab="bulk"))

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
    return render_template(
        "admin/offline_donation.html", active_tab="bulk",
        bulk_results=results, bulk_created=created, bulk_skipped=skipped,
        **_offline_donation_form_context(),
    )


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


# Donor-only master-data import -- no donation records are created here.
# Distinct from BULK_IMPORT_*/LEGACY_IMPORT_* above (which both import a
# donation transaction per row): this is for uploading/updating the
# contact + relationship + family fields on Donor records directly, e.g.
# a spreadsheet of existing known donors that predates this website.
DONOR_IMPORT_REQUIRED_COLUMNS = ["full_name"]
DONOR_IMPORT_COLUMNS = [
    "full_name", "phone", "whatsapp_number", "email", "pan",
    "address", "city", "state", "pincode",
    "donor_type", "connected_preacher_name", "donation_frequency", "gifts",
    "dob", "father_dob", "mother_dob", "wife_dob", "marriage_anniversary",
    "additional_info",
]
DONOR_IMPORT_DEMO_ROWS = [
    {
        "full_name": "Ramesh Kumar", "phone": "9876543210", "whatsapp_number": "", "email": "ramesh@example.com",
        "pan": "ABCDE1234F", "address": "12 MG Road", "city": "Delhi", "state": "Delhi", "pincode": "110001",
        "donor_type": "iyf", "connected_preacher_name": "", "donation_frequency": "monthly", "gifts": "",
        "dob": "1985-06-12", "father_dob": "", "mother_dob": "", "wife_dob": "", "marriage_anniversary": "",
        "additional_info": "",
    },
    {
        "full_name": "Sita Devi", "phone": "9123456780", "whatsapp_number": "", "email": "",
        "pan": "", "address": "45 Ring Road", "city": "Delhi", "state": "Delhi", "pincode": "110024",
        "donor_type": "live_to_give", "connected_preacher_name": "", "donation_frequency": "quarterly",
        "gifts": "Bhagavad Gita set", "dob": "1978-11-02", "father_dob": "", "mother_dob": "",
        "wife_dob": "", "marriage_anniversary": "2001-02-14", "additional_info": "Prefers WhatsApp contact",
    },
    {
        "full_name": "Amit Sharma", "phone": "", "whatsapp_number": "9988776655", "email": "amit@example.com",
        "pan": "FGHIJ5678K", "address": "", "city": "", "state": "", "pincode": "",
        "donor_type": "iyf", "connected_preacher_name": "", "donation_frequency": "one_time", "gifts": "",
        "dob": "", "father_dob": "", "mother_dob": "", "wife_dob": "", "marriage_anniversary": "",
        "additional_info": "",
    },
]


@bp.route("/donors/import/demo.csv")
@login_required
@admin_role_required
def import_donors_demo_csv():
    """A ready-to-edit template CSV -- every column the donor importer
    understands, header row plus a few example rows. Preacher names are
    left blank in the demo so it imports cleanly out of the box even
    before any Preachers have been added under Admin -> Preachers."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=DONOR_IMPORT_COLUMNS)
    writer.writeheader()
    writer.writerows(DONOR_IMPORT_DEMO_ROWS)
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=donor_data_demo.csv"},
    )


def _parse_import_date(raw, label, row_errors):
    """YYYY-MM-DD or blank. Blank means 'leave whatever's already on
    file untouched' (same convention as every other column here); a
    non-blank value that doesn't parse fails the whole row rather than
    being silently dropped."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        row_errors.append(f"invalid {label} '{raw}' (expected YYYY-MM-DD)")
        return None


def _resolve_preacher_import(preachers_by_name, raw_name, row_errors):
    """Returns (should_update, preacher_id_or_None) for the
    connected_preacher_name import column. Blank means 'leave the donor's
    existing preacher assignment untouched'; 'none' / 'not assigned' /
    'unassigned' explicitly clears it; anything else must case-insensitively
    match an existing Preacher's name (manage those under Admin -> Preachers
    first) or the row fails."""
    name = (raw_name or "").strip()
    if not name:
        return False, None
    if name.lower() in ("none", "not assigned", "unassigned"):
        return True, None
    match = preachers_by_name.get(name.lower())
    if not match:
        row_errors.append(f"preacher '{name}' not found")
        return False, None
    return True, match.id


@bp.route("/donors/import", methods=["GET", "POST"])
@login_required
@admin_role_required
def import_donors():
    """Bulk-imports/updates donor master data -- contact details plus the
    Donor Type / Connected Preacher / Donation Frequency / family-date
    fields normally filled in one-by-one under Admin -> Donors -> Edit --
    from a CSV. No donation records are created here.

    Existing donors are matched using the same PAN -> phone -> email
    priority as every other donor-touching form (find_or_create_donor from
    public.py), and a match updates that donor's fields following the same
    "new value wins, blank leaves the existing value alone" convention used
    everywhere donor data can be re-submitted -- so re-uploading the same
    file, or a file that only has a few columns filled in for some rows,
    never wipes out data that's already on file.
    """
    if request.method == "GET":
        return render_template("admin/import_donors.html", results=None)

    file = request.files.get("csv_file")
    if not file or not file.filename:
        flash("Please choose a CSV file to upload.")
        return redirect(url_for("admin.import_donors"))

    try:
        stream = io.TextIOWrapper(file.stream, encoding="utf-8-sig")
        reader = csv.DictReader(stream)
        fieldnames = {(f or "").strip() for f in (reader.fieldnames or [])}
    except Exception:
        flash("Couldn't read that file -- please upload a CSV (comma-separated) file.")
        return redirect(url_for("admin.import_donors"))

    missing = [c for c in DONOR_IMPORT_REQUIRED_COLUMNS if c not in fieldnames]
    if missing:
        flash(
            "That CSV is missing required column(s): " + ", ".join(missing)
            + ". Download the demo file below for the full column list."
        )
        return redirect(url_for("admin.import_donors"))

    preachers_by_name = {p.name.strip().lower(): p for p in Preacher.query.all()}

    results = []
    created = 0
    updated = 0

    for line_num, raw_row in enumerate(reader, start=2):  # header is line 1
        row = {(k or "").strip(): (v or "").strip() for k, v in raw_row.items() if k}
        row_errors = []

        full_name = row.get("full_name", "")
        if not full_name:
            row_errors.append("full_name is required")

        pan = row.get("pan", "").upper()
        if pan and not is_valid_pan(pan):
            row_errors.append(f"invalid PAN '{pan}'")
        row["pan"] = pan

        donor_type_raw = row.get("donor_type", "").lower()
        if donor_type_raw and donor_type_raw not in DONOR_TYPES:
            row_errors.append(f"donor_type must be one of {', '.join(DONOR_TYPES)} (got '{donor_type_raw}')")

        frequency_raw = row.get("donation_frequency", "").lower()
        if frequency_raw and frequency_raw not in DONATION_FREQUENCIES:
            row_errors.append(
                f"donation_frequency must be one of {', '.join(DONATION_FREQUENCIES)} (got '{frequency_raw}')"
            )

        update_preacher, preacher_id = _resolve_preacher_import(
            preachers_by_name, row.get("connected_preacher_name"), row_errors
        )

        dob = _parse_import_date(row.get("dob"), "dob", row_errors)
        father_dob = _parse_import_date(row.get("father_dob"), "father_dob", row_errors)
        mother_dob = _parse_import_date(row.get("mother_dob"), "mother_dob", row_errors)
        wife_dob = _parse_import_date(row.get("wife_dob"), "wife_dob", row_errors)
        marriage_anniversary = _parse_import_date(row.get("marriage_anniversary"), "marriage_anniversary", row_errors)

        if row_errors:
            results.append({"line": line_num, "name": full_name or "(blank)", "ok": False, "errors": row_errors})
            continue

        try:
            # Mirrors find_or_create_donor's own PAN -> phone -> email
            # matching so we know up front whether this row will create a
            # new donor or update an existing one (for the results table).
            phone_v = row.get("phone", "").strip()
            email_v = row.get("email", "").strip().lower()
            existing = None
            if pan:
                existing = Donor.query.filter_by(pan=pan).first()
            if existing is None and phone_v:
                existing = Donor.query.filter_by(phone=phone_v).first()
            if existing is None and email_v:
                existing = Donor.query.filter_by(email=email_v).first()
            was_new = existing is None

            donor = find_or_create_donor(row)

            if donor_type_raw:
                donor.donor_type = donor_type_raw
            if update_preacher:
                donor.connected_preacher_id = preacher_id
            if frequency_raw:
                donor.donation_frequency = frequency_raw
            gifts_raw = row.get("gifts", "")
            if gifts_raw:
                donor.gifts = gifts_raw[:500]
            if dob:
                donor.dob = dob
            if father_dob:
                donor.father_dob = father_dob
            if mother_dob:
                donor.mother_dob = mother_dob
            if wife_dob:
                donor.wife_dob = wife_dob
            if marriage_anniversary:
                donor.marriage_anniversary = marriage_anniversary
            additional_info_raw = row.get("additional_info", "")
            if additional_info_raw:
                donor.additional_info = additional_info_raw

            db.session.commit()
            results.append({
                "line": line_num, "name": full_name, "ok": True,
                "action": "created" if was_new else "updated", "donor_id": donor.id,
            })
            if was_new:
                created += 1
            else:
                updated += 1
        except Exception as exc:
            db.session.rollback()
            current_app.logger.exception("Donor import failed on row %s", line_num)
            results.append({
                "line": line_num, "name": full_name or "(blank)", "ok": False,
                "errors": [f"unexpected error -- row skipped ({exc})"],
            })

    skipped = len(results) - created - updated
    flash(f"Donor import finished: {created} created, {updated} updated, {skipped} skipped.")
    return render_template(
        "admin/import_donors.html", results=results, created=created, updated=updated, skipped=skipped
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


@bp.route("/preachers", methods=["GET", "POST"])
@login_required
def preachers():
    if request.method == "POST":
        if current_user.role != "admin":
            flash("That action requires an administrator account.")
            return redirect(url_for("admin.preachers"))
        name = request.form.get("name", "").strip()
        if not name:
            flash("Preacher name can't be blank.")
            return redirect(url_for("admin.preachers"))
        if Preacher.query.filter_by(name=name).first():
            flash(f"A preacher named '{name}' already exists.")
            return redirect(url_for("admin.preachers"))
        db.session.add(Preacher(name=name))
        db.session.commit()
        flash(f"Preacher '{name}' added.")
        return redirect(url_for("admin.preachers"))

    preacher_list = Preacher.query.order_by(Preacher.name).all()
    return render_template("admin/preachers.html", preachers=preacher_list)


@bp.route("/preachers/<int:preacher_id>/toggle", methods=["POST"])
@login_required
@admin_role_required
def toggle_preacher(preacher_id):
    preacher = Preacher.query.get_or_404(preacher_id)
    preacher.is_active = not preacher.is_active
    db.session.commit()
    return redirect(url_for("admin.preachers"))


@bp.route("/preachers/<int:preacher_id>/edit", methods=["GET", "POST"])
@login_required
@admin_role_required
def preacher_edit(preacher_id):
    preacher = Preacher.query.get_or_404(preacher_id)
    if request.method == "POST":
        new_name = request.form.get("name", "").strip()
        if not new_name:
            flash("Preacher name can't be blank.")
            return redirect(url_for("admin.preacher_edit", preacher_id=preacher_id))
        existing = Preacher.query.filter(Preacher.name == new_name, Preacher.id != preacher.id).first()
        if existing:
            flash(f"Another preacher is already named '{new_name}'.")
            return redirect(url_for("admin.preacher_edit", preacher_id=preacher_id))

        preacher.name = new_name
        db.session.commit()
        flash(f"Preacher renamed to '{preacher.name}'.")
        return redirect(url_for("admin.preachers"))

    return render_template("admin/preacher_edit.html", preacher=preacher)


@bp.route("/preachers/<int:preacher_id>/delete", methods=["POST"])
@login_required
@admin_role_required
def preacher_delete(preacher_id):
    preacher = Preacher.query.get_or_404(preacher_id)
    has_donors = Donor.query.filter_by(connected_preacher_id=preacher.id).first() is not None
    if has_donors:
        flash(
            f"Can't delete '{preacher.name}' -- donors are still connected to them. "
            "Deactivate instead to hide them from the Connected Preacher dropdown, "
            "or reassign those donors first."
        )
        return redirect(url_for("admin.preachers"))

    db.session.delete(preacher)
    db.session.commit()
    flash(f"Preacher '{preacher.name}' deleted.")
    return redirect(url_for("admin.preachers"))


def _next_occurrence(date_value, today):
    """Days until the next anniversary of date_value's month/day, ignoring
    the year -- handles the year-boundary wraparound (e.g. today is Dec
    25, the event is Jan 3) by trying this year first and rolling over to
    next year if that's already passed. A Feb 29 birthday in a non-leap
    target year is treated as Feb 28 rather than raising."""
    for year in (today.year, today.year + 1):
        try:
            candidate = date_value.replace(year=year)
        except ValueError:
            candidate = date_value.replace(year=year, day=28)
        if candidate >= today:
            return (candidate - today).days, candidate
    return None, None  # unreachable -- the year+1 branch always satisfies candidate >= today


@bp.route("/donor-insights")
@login_required
def donor_insights():
    """Aggregate reports over the relationship-management fields on Donor
    (donor type, connected preacher, donation frequency, family DOBs /
    anniversary) -- totals by type and by preacher, an unassigned-donors
    count, a donation-frequency breakdown, and an upcoming birthdays/
    anniversaries list for relationship building.
    """
    # --- donors + donation totals by donor type ---
    donor_type_counts = {t: 0 for t in DONOR_TYPES}
    donor_type_counts["_uncategorised"] = 0
    for donor_type, count in db.session.query(Donor.donor_type, func.count(Donor.id)).group_by(Donor.donor_type).all():
        if donor_type in donor_type_counts:
            donor_type_counts[donor_type] = count
        else:
            donor_type_counts["_uncategorised"] += count

    donor_type_totals = {t: 0.0 for t in DONOR_TYPES}
    rows = (
        db.session.query(Donor.donor_type, func.coalesce(func.sum(Donation.amount), 0))
        .join(Donation, Donation.donor_id == Donor.id)
        .filter(Donation.status == "success")
        .group_by(Donor.donor_type)
        .all()
    )
    for donor_type, total in rows:
        if donor_type in donor_type_totals:
            donor_type_totals[donor_type] = float(total)

    # --- donors + donation totals by preacher ---
    preacher_rows = (
        db.session.query(
            Preacher.id, Preacher.name,
            func.count(func.distinct(Donor.id)),
            func.coalesce(func.sum(Donation.amount), 0),
        )
        .join(Donor, Donor.connected_preacher_id == Preacher.id)
        .outerjoin(Donation, (Donation.donor_id == Donor.id) & (Donation.status == "success"))
        .group_by(Preacher.id, Preacher.name)
        .order_by(Preacher.name)
        .all()
    )
    preacher_stats = [
        {"id": pid, "name": name, "donor_count": donor_count, "total": float(total)}
        for pid, name, donor_count, total in preacher_rows
    ]
    unassigned_count = Donor.query.filter(Donor.connected_preacher_id.is_(None)).count()

    # --- donation frequency breakdown ---
    frequency_counts = {f: 0 for f in DONATION_FREQUENCIES}
    for freq, count in db.session.query(Donor.donation_frequency, func.count(Donor.id)).group_by(Donor.donation_frequency).all():
        if freq in frequency_counts:
            frequency_counts[freq] = count

    # --- upcoming birthdays / anniversaries ---
    upcoming_days = request.args.get("days", 30, type=int)
    today = datetime.date.today()
    donors_with_dates = Donor.query.filter(
        db.or_(
            Donor.dob.isnot(None), Donor.father_dob.isnot(None), Donor.mother_dob.isnot(None),
            Donor.wife_dob.isnot(None), Donor.marriage_anniversary.isnot(None),
        )
    ).all()

    upcoming = []
    for donor in donors_with_dates:
        for label, date_value in [
            ("Donor's Birthday", donor.dob), ("Marriage Anniversary", donor.marriage_anniversary),
            ("Father's Birthday", donor.father_dob), ("Mother's Birthday", donor.mother_dob),
            ("Wife's Birthday", donor.wife_dob),
        ]:
            if not date_value:
                continue
            days_until, occurrence_date = _next_occurrence(date_value, today)
            if days_until is not None and days_until <= upcoming_days:
                upcoming.append({"donor": donor, "label": label, "days_until": days_until, "date": occurrence_date})
    upcoming.sort(key=lambda u: u["days_until"])

    return render_template(
        "admin/donor_insights.html",
        donor_type_counts=donor_type_counts, donor_type_totals=donor_type_totals,
        donor_type_labels=DONOR_TYPE_LABELS, donor_types=DONOR_TYPES,
        preacher_stats=preacher_stats, unassigned_count=unassigned_count,
        frequency_counts=frequency_counts, donation_frequency_labels=DONATION_FREQUENCY_LABELS,
        donation_frequencies=DONATION_FREQUENCIES,
        upcoming=upcoming, upcoming_days=upcoming_days,
    )


@bp.route("/birthdays")
@login_required
def birthdays():
    """Dedicated birthday calendar -- donor's own DOB only (family DOBs /
    marriage anniversaries get the fuller treatment on Donor Insights,
    which covers a wider set of relationship-building dates). Today's
    birthdays are broken out separately; everything else within the
    selected window is listed soonest-first."""
    today = datetime.date.today()
    window_days = request.args.get("days", 60, type=int)

    donors_with_dob = Donor.query.filter(Donor.dob.isnot(None)).all()

    all_birthdays = []
    for donor in donors_with_dob:
        days_until, occurrence_date = _next_occurrence(donor.dob, today)
        all_birthdays.append({"donor": donor, "days_until": days_until, "date": occurrence_date})
    all_birthdays.sort(key=lambda u: u["days_until"])

    birthdays_today = [u for u in all_birthdays if u["days_until"] == 0]
    birthdays_upcoming = [u for u in all_birthdays if 0 < u["days_until"] <= window_days]

    return render_template(
        "admin/birthdays.html",
        birthdays_today=birthdays_today,
        birthdays_upcoming=birthdays_upcoming,
        window_days=window_days,
        donors_with_dob_count=len(donors_with_dob),
        total_donors=Donor.query.count(),
    )


@bp.route("/settings/backup")
@login_required
@admin_role_required
def data_backup():
    """On-demand full data backup (donors/donations/lookup lists as CSV,
    zipped -- see backup_utils.build_backup_zip). Complements
    backup_data.py, which is meant to run automatically on a weekly
    schedule (Render Cron Job or any external scheduler) -- this page is
    for "I want one right now" without waiting for that."""
    backups_dir = os.path.join(current_app.root_path, "instance", "backups")
    recent_backups = []
    if os.path.isdir(backups_dir):
        recent_backups = sorted(
            (f for f in os.listdir(backups_dir) if f.startswith("temple_data_backup_") and f.endswith(".zip")),
            reverse=True,
        )[:10]
    return render_template(
        "admin/data_backup.html", recent_backups=recent_backups,
        backup_email=current_app.config.get("BACKUP_EMAIL") or current_app.config.get("ORG_CONTACT_EMAIL"),
        smtp_configured=bool(current_app.config.get("SMTP_HOST")),
    )


@bp.route("/settings/backup/download")
@login_required
@admin_role_required
def download_backup():
    filename, zip_bytes = build_backup_zip()
    return Response(
        zip_bytes, mimetype="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


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


@bp.route("/export/donations")
@login_required
def export_donations():
    """CSV export of the Donations Log, honoring whatever campaign/mode/
    status filters are currently applied on that page -- unlike the
    on-screen table (paginated to DONATIONS_PER_PAGE rows), this exports
    every matching row. Carries the same full detail set as the Donations
    Log's "Full details" modal (donor contact info, PAN, address, payment
    reference, campaign-specific purpose, 80G status, who recorded it, and
    cancellation info where relevant) so this file alone answers "what did
    this donor actually enter" without having to click into each row.
    """
    query, filters = _apply_donations_filters(Donation.query)
    status = filters["status"]
    query, _sort = _sorted_donations_query(query, request.args.get("sort", "-date"))

    rows = query.all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Receipt No", "Date", "Status", "Donor Name", "Phone", "WhatsApp", "Email", "PAN",
        "Address", "City", "State", "Pincode",
        "Amount", "Payment Mode", "Reference", "Campaign", "Specific Purpose", "80G Eligible",
        "Recorded By", "Remarks", "Cancelled At", "Cancelled By", "Cancellation Reason",
    ])
    for d in rows:
        donor = d.donor
        specific_purpose = (
            (d.bace_property.name if d.bace_property else None)
            or (d.festival.name if d.festival else None)
            or (d.seva_type.name if d.seva_type else None)
            or (d.live_to_give_purpose.name if d.live_to_give_purpose else None)
            or ""
        )
        # Online donations carry a real time-of-day (set the instant the
        # payment was confirmed); offline entries are always saved at
        # midnight since only a date is captured for those, so a time
        # component would be misleading noise -- see the same convention
        # in the Donations Log table and detail modal.
        date_str = (
            d.donation_date.strftime("%d-%m-%Y %H:%M")
            if d.payment_mode == "online"
            else d.donation_date.strftime("%d-%m-%Y")
        )
        writer.writerow([
            d.receipt_number or "",
            date_str,
            d.status,
            donor.full_name,
            donor.phone or "",
            donor.whatsapp_number or "",
            donor.email or "",
            donor.pan or "",
            donor.address or "",
            donor.city or "",
            donor.state or "",
            donor.pincode or "",
            float(d.amount),
            d.payment_mode,
            d.reference_display or "",
            d.campaign.name,
            specific_purpose,
            "Yes" if d.effective_is_80g else "No",
            d.recorded_by or "",
            d.remarks or "",
            d.cancelled_at.strftime("%d-%m-%Y %H:%M") if d.cancelled_at else "",
            d.cancelled_by or "",
            d.cancellation_reason or "",
        ])

    filename_status = status if status != "all" else "all-statuses"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename=Donations_{filename_status}.csv"},
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
