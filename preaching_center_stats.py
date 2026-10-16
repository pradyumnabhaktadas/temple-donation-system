"""Collection figures for preaching centers, computed in the database.

Every function here uses grouped SQL over the narrow columns it needs. The
first version of the center pages loaded every Donation entity of a center
into Python (and, because receipt PDFs weren't deferred, every stored PDF
with it) just to add amounts up.

"Today", "this month" and "financial year" are IST calendar boundaries,
converted to UTC-naive bounds with utils.ist_day_bounds_utc because
Donation.donation_date is stored as naive UTC. Only successful donations
count -- pending/failed/cancelled never inflate a center's total.
"""
import datetime
from decimal import Decimal

from sqlalchemy import and_, case, func

from extensions import db
from models import Donation, Donor, PreachingCenter
from utils import ist_day_bounds_utc, now_ist, to_ist


def fy_start_for(day):
    """First day (1 April) of the Indian financial year containing `day`."""
    return datetime.date(day.year if day.month >= 4 else day.year - 1, 4, 1)


def _sum_where(condition):
    return func.coalesce(func.sum(case((condition, Donation.amount), else_=0)), 0)


def center_stats(today=None, center_ids=None):
    """{center_id: stats} for every center that has at least one successful donation.

    stats keys: today, month, fy, lifetime (Decimal), count, donors (distinct),
    last_donation (UTC-naive datetime or None).
    """
    today = today or now_ist().date()
    today_start, today_end = ist_day_bounds_utc(today)
    month_start, _ = ist_day_bounds_utc(today.replace(day=1))
    fy_start, _ = ist_day_bounds_utc(fy_start_for(today))

    query = db.session.query(
        Donation.preaching_center_id,
        _sum_where(and_(Donation.donation_date >= today_start, Donation.donation_date < today_end)),
        _sum_where(Donation.donation_date >= month_start),
        _sum_where(Donation.donation_date >= fy_start),
        func.coalesce(func.sum(Donation.amount), 0),
        func.count(Donation.id),
        func.count(func.distinct(Donation.donor_id)),
        func.max(Donation.donation_date),
    ).filter(Donation.status == "success", Donation.preaching_center_id.isnot(None))
    if center_ids is not None:
        query = query.filter(Donation.preaching_center_id.in_(list(center_ids)))
    stats = {}
    for cid, today_sum, month_sum, fy_sum, lifetime, count, donors, last in query.group_by(
        Donation.preaching_center_id
    ).all():
        stats[cid] = {
            "today": Decimal(today_sum), "month": Decimal(month_sum), "fy": Decimal(fy_sum),
            "lifetime": Decimal(lifetime), "count": count, "donors": donors, "last_donation": last,
        }
    return stats


EMPTY_STATS = {
    "today": Decimal("0"), "month": Decimal("0"), "fy": Decimal("0"), "lifetime": Decimal("0"),
    "count": 0, "donors": 0, "last_donation": None,
}


def target_progress(center, stats):
    """Target vs achieved for the center's own target period, or None.

    'monthly' compares against this month, 'financial_year' against the
    current financial year. pending is never negative; pct may exceed 100.
    """
    if not center.target_amount or not center.target_period:
        return None
    achieved = stats["month"] if center.target_period == "monthly" else stats["fy"]
    target = Decimal(center.target_amount)
    pct = (achieved / target * 100) if target else Decimal("0")
    return {
        "target": target, "achieved": achieved, "pending": max(target - achieved, Decimal("0")),
        "pct": min(pct, Decimal("999")), "period": center.target_period,
    }


def dashboard_rows(today=None, active_only=False):
    """One row per center (including centers with no donations yet), newest data first by name."""
    today = today or now_ist().date()
    query = PreachingCenter.query.order_by(PreachingCenter.name)
    if active_only:
        query = query.filter_by(is_active=True)
    centers = query.all()
    stats = center_stats(today, [c.id for c in centers] if centers else [])
    rows = []
    for center in centers:
        s = stats.get(center.id, EMPTY_STATS)
        rows.append({"center": center, "stats": s, "progress": target_progress(center, s)})
    return rows


def period_totals(start_date, end_date):
    """{center_id: (amount, count)} for IST dates in [start_date, end_date)."""
    start, _ = ist_day_bounds_utc(start_date)
    end, _ = ist_day_bounds_utc(end_date)
    rows = db.session.query(
        Donation.preaching_center_id, func.coalesce(func.sum(Donation.amount), 0), func.count(Donation.id)
    ).filter(
        Donation.status == "success", Donation.preaching_center_id.isnot(None),
        Donation.donation_date >= start, Donation.donation_date < end,
    ).group_by(Donation.preaching_center_id).all()
    return {cid: (Decimal(total), count) for cid, total, count in rows}


def report_section(report_date, week_start, month_start):
    """Per-center rows for the daily report: today / week / month, centers with any activity only."""
    end = report_date + datetime.timedelta(days=1)
    day, week, month = (period_totals(s, end) for s in (report_date, week_start, month_start))
    ids = set(day) | set(week) | set(month)
    if not ids:
        return []
    names = {c.id: c.name for c in PreachingCenter.query.filter(PreachingCenter.id.in_(ids)).all()}
    zero = (Decimal("0"), 0)
    return sorted(
        ({
            "name": names.get(cid, "Unknown center"),
            "today": day.get(cid, zero), "week": week.get(cid, zero), "month": month.get(cid, zero),
        } for cid in ids),
        key=lambda r: r["month"][0], reverse=True,
    )


def monthly_totals(center_id):
    """{'YYYY-MM': Decimal} newest first, grouped by IST month.

    Only (amount, date) tuples are fetched -- no entities -- and grouping is
    done in Python after to_ist() rather than with a database date function,
    which differs between SQLite (tests) and PostgreSQL (Render) and would
    put late-night donations in the wrong month.
    """
    totals = {}
    for amount, when in db.session.query(Donation.amount, Donation.donation_date).filter(
        Donation.preaching_center_id == center_id, Donation.status == "success",
        Donation.donation_date.isnot(None),
    ).all():
        key = to_ist(when).strftime("%Y-%m")
        totals[key] = totals.get(key, Decimal("0")) + Decimal(amount)
    return dict(sorted(totals.items(), reverse=True))


def top_donors(center_id, limit=10):
    rows = db.session.query(
        Donor.id, Donor.full_name, func.sum(Donation.amount), func.count(Donation.id)
    ).join(Donation, Donation.donor_id == Donor.id).filter(
        Donation.preaching_center_id == center_id, Donation.status == "success"
    ).group_by(Donor.id, Donor.full_name).order_by(func.sum(Donation.amount).desc()).limit(limit).all()
    return [{"id": i, "name": n, "amount": Decimal(a), "count": c} for i, n, a, c in rows]
