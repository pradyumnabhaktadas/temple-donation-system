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

    def reconciliation_html(rec):
        """The part of this email that exists because payments were being
        lost. Only rendered when there's something to say -- a clean run
        with nothing stranded stays quiet rather than training people to
        scroll past a box that always says "0"."""
        if not rec:
            return ""

        blocks = []

        if rec.get("error"):
            blocks.append(
                '<p style="margin:0 0 8px;"><strong>The payment reconciliation check could not run:</strong> '
                f'{rec["error"]}<br><span style="color:#555;">Zoho Forms payments may be unreceipted until '
                'this succeeds. It retries automatically every hour.</span></p>'
            )

        if rec.get("created"):
            rows = "".join(
                f'<li>Rs. {format_inr(float(d.amount))} -- receipt {d.receipt_number}</li>'
                for d in rec["created"]
            )
            blocks.append(
                f'<p style="margin:0 0 4px;"><strong>{len(rec["created"])} receipt(s) issued for Zoho Forms '
                'payments Zoho never confirmed to us.</strong> These were real, captured payments matched '
                f'against Razorpay directly:</p><ul style="margin:0 0 10px;">{rows}</ul>'
            )

        if rec.get("orphan_payments"):
            # why_not_receipted is the point of this block. "pay_X -- Rs.
            # 100" tells the reader money is unaccounted for but not what
            # to do; "form 'MYTE' isn't set up" tells them exactly what to
            # do, and is the difference between a report that gets acted
            # on and one people learn to scroll past.
            rows = "".join(
                f'<li>{p["payment_id"]} -- Rs. {format_inr(p["amount"])} '
                f'({p["contact"] or "no contact number"})'
                + (f'<br><span style="color:#555;">{p["why_not_receipted"]}</span>'
                   if p.get("why_not_receipted") else "")
                + '</li>'
                for p in rec["orphan_payments"]
            )
            blocks.append(
                f'<p style="margin:0 0 4px;"><strong>{len(rec["orphan_payments"])} captured payment(s) with '
                'no receipt issued.</strong> Money Razorpay received that no donation '
                f'accounts for -- worth checking:</p><ul style="margin:0 0 10px;">{rows}</ul>'
            )

        if not blocks:
            return ""

        return f"""
        <div style="margin:22px 0 0;padding:12px 14px;background:#fff8e6;border-left:4px solid #d19b2f;">
          <h3 style="margin:0 0 8px;color:#7a1f1f;">Payment Reconciliation</h3>
          <div style="font-size:14px;">{"".join(blocks)}</div>
        </div>
        """

    return f"""
    <div style="font-family:Georgia,'Times New Roman',serif;color:#222;max-width:600px;">
      <h2 style="color:#7a1f1f;margin-bottom:4px;">{org_name} -- Daily Collection Report</h2>
      <p style="color:#555;margin-top:0;">For {data['report_date'].strftime('%d %b %Y')}</p>
      {reconciliation_html(data.get('reconciliation'))}
      {section_html("Today's Collection", data['today'])}
      {section_html(f"This Week's Collection (since {data['week_start'].strftime('%d %b')})", data['week'])}
      {section_html(f"This Month's Collection ({data['month_start'].strftime('%B %Y')})", data['month'])}
      <p style="margin-top:24px;font-size:12px;color:#999;">
        This is an automated report generated at 4:00 AM. Manage recipients under
        Admin &rarr; Settings &rarr; Daily Report Recipients.
      </p>
    </div>
    """


def _recipient_is_due(recipient, report_date):
    """Whether this recipient should receive the report for report_date,
    given their configured frequency. The cron job still runs every day --
    this is what turns that daily trigger into a weekly/fortnightly/monthly
    one per recipient.

    Anchors: weekly and fortnightly fire on Mondays (weekday() == 0);
    fortnightly further restricts to even ISO week numbers, so it's every
    other Monday rather than "every Monday" (which would just be weekly).
    Monthly fires on the 1st of the calendar month. daily always fires."""
    frequency = getattr(recipient, "frequency", "daily") or "daily"
    if frequency == "daily":
        return True
    if frequency == "weekly":
        return report_date.weekday() == 0
    if frequency == "fortnightly":
        return report_date.weekday() == 0 and report_date.isocalendar()[1] % 2 == 0
    if frequency == "monthly":
        return report_date.day == 1
    return True  # unknown frequency value -- fail open rather than silently drop


def run_reconciliation_safely(app):
    """Runs the Zoho/Razorpay reconciliation as part of the daily report,
    and returns its summary for the report to display.

    Belt and braces: reconciliation has its own hourly Cron Job
    (render.yaml's "temple-zoho-reconcile"), but Cron Jobs need a Render
    plan that supports them, and this project's *other* cron job spent
    three consecutive days silently failing to do its work (see this
    module's docstring and daily_report.py's). Hanging reconciliation off
    the daily report as well means the worst case is a receipt issued the
    next morning instead of within the hour -- not a payment lost
    indefinitely, which is the failure this whole feature exists to end.

    Never raises: the daily report must still go out even if Razorpay is
    unreachable or reconciliation hits something unexpected. A failure
    here is reported inside the email rather than taking the email down."""
    try:
        from public import reconcile_zoho_submissions
        return reconcile_zoho_submissions(app.config)
    except Exception as exc:
        app.logger.exception("Zoho reconciliation from the daily report failed")
        # Same keys reconcile_zoho_submissions returns, so the email
        # renders a failed run the same way it renders a clean one. This
        # dict drifted once already, keeping keys ("unpaid",
        # "still_waiting", "expired") from a queue that no longer exists.
        return {
            "created": [], "orphan_payments": [], "ignored_payments": [],
            "report_only": False, "pruned": 0, "error": str(exc),
        }


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
    # Before computing totals, not after: a receipt issued by
    # reconciliation right now is a real donation that belongs in today's
    # numbers, so sweeping first means the report and the ledger agree.
    reconciliation = run_reconciliation_safely(app)

    data = compute_report(report_date)
    data["reconciliation"] = reconciliation
    date_key = int(data["report_date"].strftime("%Y%m%d"))

    if not force:
        already_sent = AdminActivityLog.query.filter_by(
            action="daily_report_sent", target_type="daily_report", target_id=date_key
        ).first()
        if already_sent:
            return {"skipped": "already_sent", "report_date": data["report_date"]}

    org_name = app.config.get("ORG_NAME") or "the temple"
    recipients = DailyReportRecipient.query.filter_by(is_active=True).all()
    due_recipients = [r for r in recipients if _recipient_is_due(r, data["report_date"])]
    email_recipients = [r.value for r in due_recipients if r.contact_type == "email"]
    whatsapp_recipients = [r.value for r in due_recipients if r.contact_type == "whatsapp"]

    email_sent = False
    email_error = None
    if email_recipients:
        try:
            email_sent, email_error = email_utils.send_daily_report_email(
                app.config, email_recipients, data, org_name
            )
        except Exception as exc:  # last-resort safety net -- send_daily_report_email
            # itself already catches and reports its own failures via the
            # (sent, error) tuple above; this only fires for something truly
            # unanticipated (e.g. a bug in that function itself).
            email_error = str(exc)
            app.logger.exception("Daily report email send failed")

    whatsapp_sent_count = 0
    whatsapp_error = None
    for number in whatsapp_recipients:
        try:
            sent, error = whatsapp_utils.send_daily_report_whatsapp(app.config, number, data, org_name)
            if sent:
                whatsapp_sent_count += 1
            elif error:
                # Keeps the most recent real failure, if more than one
                # recipient fails -- enough to point at "what's wrong",
                # without needing a per-recipient error list on this
                # already-terse audit-log line.
                whatsapp_error = error
        except Exception as exc:
            whatsapp_error = str(exc)
            app.logger.exception("Daily report WhatsApp send failed for %s", number)

    # email_error/whatsapp_error used to be computed but never actually
    # written here -- the 2026-08-29/08-30/08-31 incident showed up in
    # AdminActivityLog as just "email_sent=False", with the real reason
    # only ever reaching this function's own app.logger calls, which
    # aren't visible anywhere in the admin UI. Including them here means
    # the Activity Log alone can now answer "why", not just "whether".
    #
    # Truncated to 150 chars each: AdminActivityLog.details is a
    # String(500), and Postgres (production) enforces that length --
    # unlike SQLite, an over-length value here would raise on commit and
    # take down the whole report run, exactly the kind of failure this
    # change exists to make visible rather than hide.
    details = (
        f"date={data['report_date']} email_sent={email_sent} "
        f"({len(email_recipients)} recipient(s))"
        + (f" email_error={email_error[:150]!r}" if email_error else "")
        + f" whatsapp_sent={whatsapp_sent_count}/{len(whatsapp_recipients)}"
        + (f" whatsapp_error={whatsapp_error[:150]!r}" if whatsapp_error else "")
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
