"""Computes and sends the daily collection report -- yesterday's, this
week's (calendar week to date), and this month's (calendar month to date)
collection totals, plus a campaign-wise breakdown for each -- to the
recipients configured under Admin -> Settings -> Daily Report Recipients
(see DailyReportRecipient in models.py).

Deliberately built as its own module + a thin CLI script (daily_report.py)
that calls into it, rather than a Flask route, mirroring backup_utils.py /
backup_data.py -- see render.yaml's "temple-daily-report" Cron Job, which
runs `python daily_report.py` once a day at 4:00 AM IST (22:30 UTC the
previous day).

Runs at 4 AM the morning *after* the day it reports on, so "today's
collection" in the report always means the day that just finished --
report_date defaults to yesterday (IST).

IMPORTANT: donation_date is stored as naive UTC (datetime.datetime.utcnow(),
same as everywhere else in this codebase), so every date this module buckets
donations into is computed via utils.to_ist(dt).date(), never dt.date()
directly -- otherwise a donation made between ~12:00 AM-5:30 AM IST would be
bucketed under the previous UTC day instead of its correct IST day.
"""
import datetime

from extensions import db
from models import AdminActivityLog, Campaign, DailyReportRecipient, Donation
from utils import format_inr, now_ist, to_ist

import email_utils
import whatsapp_utils


def _period_totals(rows, start, end):
    """rows: list of (amount, ist_date, campaign_name). Returns totals plus
    a campaign breakdown for the [start, end) window, with each campaign's
    percentage computed against this same period's grand total -- so the
    breakdown always ties out to the period total shown alongside it."""
    period_rows = [(amt, campaign) for amt, d, campaign in rows if start <= d < end]
    total = sum(amt for amt, _ in period_rows)
    count = len(period_rows)
    grand = total or 1.0

    campaign_totals = {}
    for amt, campaign in period_rows:
        bucket = campaign_totals.setdefault(campaign, {"amount": 0.0, "count": 0})
        bucket["amount"] += amt
        bucket["count"] += 1

    campaigns = sorted(
        [
            {
                "name": name,
                "amount": v["amount"],
                "count": v["count"],
                "pct": round(v["amount"] / grand * 100, 1),
            }
            for name, v in campaign_totals.items()
        ],
        key=lambda r: r["amount"],
        reverse=True,
    )
    return {"amount": total, "count": count, "campaigns": campaigns}


def compute_report(report_date=None):
    """Builds the full report data for `report_date` (an IST calendar date;
    defaults to yesterday, IST). Returns a dict with report_date/week_start/
    month_start plus "today"/"week"/"month" sub-dicts, each shaped like
    _period_totals()'s return value."""
    if report_date is None:
        report_date = now_ist().date() - datetime.timedelta(days=1)

    week_start = report_date - datetime.timedelta(days=report_date.weekday())  # Monday
    month_start = report_date.replace(day=1)
    report_end = report_date + datetime.timedelta(days=1)  # exclusive upper bound

    # Coarse SQL-side lower bound only (a few hours of slack either side of
    # month_start's IST midnight, converted loosely to UTC) -- the precise
    # per-row IST bucketing happens in Python below via to_ist(), same
    # division of labour as admin.analytics()'s all_pop_donations pull.
    query_lower_bound = datetime.datetime.combine(month_start, datetime.time.min) - datetime.timedelta(hours=12)

    rows = []
    for amt, dt, campaign_name in (
        db.session.query(Donation.amount, Donation.donation_date, Campaign.name)
        .outerjoin(Campaign, Campaign.id == Donation.campaign_id)
        .filter(Donation.status == "success", Donation.donation_date >= query_lower_bound)
        .all()
    ):
        rows.append((float(amt), to_ist(dt).date(), campaign_name or "Unknown"))

    return {
        "report_date": report_date,
        "week_start": week_start,
        "month_start": month_start,
        "today": _period_totals(rows, report_date, report_end),
        "week": _period_totals(rows, week_start, report_end),
        "month": _period_totals(rows, month_start, report_end),
    }


def _render_email_html(data, org_name):
    def campaign_rows_html(campaigns):
        if not campaigns:
            return '<tr><td colspan="3" style="color:#888;padding:6px 0;">No successful donations.</td></tr>'
        return "".join(
            f'<tr><td style="padding:4px 8px 4px 0;">{c["name"]}</td>'
            f'<td style="padding:4px 8px;text-align:right;">Rs. {format_inr(c["amount"])}</td>'
            f'<td style="padding:4px 0;text-align:right;color:#888;">{c["pct"]}%</td></tr>'
            for c in campaigns
        )

    def section_html(title, period):
        return f"""
        <h3 style="margin:22px 0 6px;color:#7a1f1f;">{title}</h3>
        <p style="margin:0 0 8px;font-size:15px;">
          <strong>Rs. {format_inr(period['amount'])}</strong> from {period['count']} donation(s)
        </p>
        <table style="width:100%;border-collapse:collapse;font-size:13px;">
          {campaign_rows_html(period['campaigns'])}
        </table>
        """

    return f"""
    <div style="font-family:Georgia,'Times New Roman',serif;color:#222;max-width:600px;">
      <h2 style="color:#7a1f1f;margin-bottom:4px;">{org_name} -- Daily Collection Report</h2>
      <p style="color:#555;margin-top:0;">For {data['report_date'].strftime('%d %b %Y')}</p>
      {section_html("Today's Collection", data['today'])}
      {section_html(f"This Week's Collection (since {data['week_start'].strftime('%d %b')})", data['week'])}
      {section_html(f"This Month's Collection ({data['month_start'].strftime('%B %Y')})", data['month'])}
      <p style="margin-top:24px;font-size:12px;color:#999;">
        This is an automated report generated at 4:00 AM. Manage recipients under
        Admin &rarr; Settings &rarr; Daily Report Recipients.
      </p>
    </div>
    """


def send_report(app, report_date=None, force=False):
    """Computes the report and delivers it to every active recipient.
    Returns a result dict (used by daily_report.py's printed summary and by
    tests). Idempotent per report_date unless force=True: re-running the
    job for a date it already ran for (e.g. a manual retry after a crash)
    won't send a duplicate -- checked via AdminActivityLog rather than a
    dedicated "last run" column, consistent with how this codebase already
    uses that table as a general audit trail.

    Writes the AdminActivityLog row directly (not via admin.log_activity())
    because this runs from a CLI script with only an app context, no
    request context -- log_activity() reads Flask-Login's current_user,
    which raises outside a request rather than resolving to "system"."""
    data = compute_report(report_date)
    date_key = int(data["report_date"].strftime("%Y%m%d"))

    if not force:
        already_sent = AdminActivityLog.query.filter_by(
            action="daily_report_sent", target_type="daily_report", target_id=date_key
        ).first()
        if already_sent:
            return {"skipped": "already_sent", "report_date": data["report_date"]}

    org_name = app.config.get("ORG_NAME") or "the temple"
    recipients = DailyReportRecipient.query.filter_by(is_active=True).all()
    email_recipients = [r.value for r in recipients if r.contact_type == "email"]
    whatsapp_recipients = [r.value for r in recipients if r.contact_type == "whatsapp"]

    email_sent = False
    email_error = None
    if email_recipients:
        try:
            email_sent = email_utils.send_daily_report_email(
                app.config, email_recipients, data, org_name
            )
        except Exception as exc:  # never let a send failure crash the whole job
            email_error = str(exc)
            app.logger.exception("Daily report email send failed")

    whatsapp_sent_count = 0
    whatsapp_error = None
    for number in whatsapp_recipients:
        try:
            if whatsapp_utils.send_daily_report_whatsapp(app.config, number, data, org_name):
                whatsapp_sent_count += 1
        except Exception as exc:
            whatsapp_error = str(exc)
            app.logger.exception("Daily report WhatsApp send failed for %s", number)

    details = (
        f"date={data['report_date']} email_sent={email_sent} "
        f"({len(email_recipients)} recipient(s)) "
        f"whatsapp_sent={whatsapp_sent_count}/{len(whatsapp_recipients)}"
    )
    db.session.add(
        AdminActivityLog(
            admin_username="system",
            action="daily_report_sent",
            target_type="daily_report",
            target_id=date_key,
            details=details,
        )
    )
    db.session.commit()

    return {
        "report_date": data["report_date"],
        "data": data,
        "email_sent": email_sent,
        "email_recipients": email_recipients,
        "email_error": email_error,
        "whatsapp_sent_count": whatsapp_sent_count,
        "whatsapp_recipients": whatsapp_recipients,
        "whatsapp_error": whatsapp_error,
    }
