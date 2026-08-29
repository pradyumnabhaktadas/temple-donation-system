"""Daily collection report -- yesterday's / this week's / this month's
collection totals plus a campaign-wise breakdown, emailed (and, once a
report template is approved with Airtel, sent via WhatsApp) to whoever is
listed under Admin -> Settings -> Daily Report Recipients. See
daily_report_utils.py for the actual computation/send logic.

Usage (Render Shell / any host with the app's venv active):
    python daily_report.py
    python daily_report.py --date 2026-08-28   # re-run for a specific date
    python daily_report.py --force             # re-send even if already sent today

Intended to run once a day at 4:00 AM IST -- see render.yaml's
"temple-daily-report" Cron Job service (schedule is in UTC: 30 22 * * *,
i.e. 10:30 PM UTC the previous day = 4:00 AM IST). If your Render plan
doesn't support Cron Jobs, run this from any external scheduler (a free
cron-job.org trigger, your own crontab, etc.) pointed at this same command,
same as backup_data.py's weekly backup.
"""
import argparse
import datetime
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", default=None, help="Report date (YYYY-MM-DD, IST). Defaults to yesterday.")
    parser.add_argument("--force", action="store_true", help="Send even if this date's report was already sent")
    args = parser.parse_args()

    report_date = datetime.datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else None

    from app import create_app
    from daily_report_utils import send_report

    app = create_app()
    with app.app_context():
        result = send_report(app, report_date=report_date, force=args.force)

        if result.get("skipped"):
            print(f"Skipped -- daily report for {result['report_date']} was already sent. Use --force to re-send.")
            return

        print(f"Daily report for {result['report_date']}:")
        print(f"  Today:      Rs. {result['data']['today']['amount']:,.2f} ({result['data']['today']['count']} donations)")
        print(f"  This week:  Rs. {result['data']['week']['amount']:,.2f} ({result['data']['week']['count']} donations)")
        print(f"  This month: Rs. {result['data']['month']['amount']:,.2f} ({result['data']['month']['count']} donations)")

        if result["email_recipients"]:
            status = "sent" if result["email_sent"] else ("FAILED -- " + result["email_error"] if result["email_error"] else "skipped (SMTP not configured)")
            print(f"  Email to {len(result['email_recipients'])} recipient(s): {status}")
        else:
            print("  Email: no active email recipients configured.")

        if result["whatsapp_recipients"]:
            print(f"  WhatsApp: sent to {result['whatsapp_sent_count']}/{len(result['whatsapp_recipients'])} recipient(s)"
                  + (f" -- error: {result['whatsapp_error']}" if result["whatsapp_error"] else ""))
        else:
            print("  WhatsApp: no active WhatsApp recipients configured.")


if __name__ == "__main__":
    sys.exit(main())
