"""Daily collection report -- yesterday's / this week's / this month's
collection totals plus a campaign-wise breakdown, emailed (and, once a
report template is approved with Airtel, sent via WhatsApp) to whoever is
listed under Admin -> Settings -> Daily Report Recipients. See
daily_report_utils.py for the actual computation/send logic.

This script is deliberately a thin HTTP client, not the thing that
actually sends email/WhatsApp. Every automatic run of the Cron Job that
used to do the sending in-process failed both channels
(2026-08-29, 08-30, 08-31), while manual re-runs and every donor-facing
receipt this app sends all day, every day succeed without issue --
pointing at this Cron Job container's own outbound networking, not a bug
in the send functions (adding retries there, see utils.retry(), didn't
change the outcome). So instead of sending anything itself, this script
just POSTs to the already-running web app's own
/internal/daily-report/send and prints whatever it reports back -- the
actual SMTP/WhatsApp calls happen from that always-on process instead of
this one. See config.py's INTERNAL_TASK_TOKEN for the auth story.

Usage (Render Shell / any host with the app's venv active):
    python daily_report.py
    python daily_report.py --date 2026-08-28   # re-run for a specific date
    python daily_report.py --force             # re-send even if already sent today

Requires PUBLIC_BASE_URL (the web app's own URL) and INTERNAL_TASK_TOKEN
(shared with the web service -- see render.yaml) in this script's
environment.

Intended to run once a day at 4:00 AM IST -- see render.yaml's
"temple-daily-report" Cron Job service (schedule is in UTC: 30 22 * * *,
i.e. 10:30 PM UTC the previous day = 4:00 AM IST). If your Render plan
doesn't support Cron Jobs, run this from any external scheduler (a free
cron-job.org trigger, your own crontab, etc.) pointed at this same command,
same as backup_data.py's weekly backup.
"""
import argparse
import datetime
import os
import sys

import requests


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", default=None, help="Report date (YYYY-MM-DD, IST). Defaults to yesterday.")
    parser.add_argument("--force", action="store_true", help="Send even if this date's report was already sent")
    args = parser.parse_args()

    if args.date:
        # Validated here too (not just server-side) so a typo fails fast
        # with a clear message instead of a 400 from across the network.
        try:
            datetime.datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            print(f"Invalid --date {args.date!r}, expected YYYY-MM-DD", file=sys.stderr)
            return 1

    base_url = (os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")
    token = os.environ.get("INTERNAL_TASK_TOKEN") or ""

    if not base_url:
        print("PUBLIC_BASE_URL is not set -- don't know which app to trigger.", file=sys.stderr)
        return 1
    if not token:
        print("INTERNAL_TASK_TOKEN is not set -- refusing to call an unauthenticated endpoint.", file=sys.stderr)
        return 1

    payload = {"force": args.force}
    if args.date:
        payload["date"] = args.date

    try:
        resp = requests.post(
            f"{base_url}/internal/daily-report/send",
            json=payload,
            headers={"X-Internal-Token": token},
            timeout=60,
        )
    except requests.RequestException as exc:
        print(f"Could not reach {base_url}: {exc}", file=sys.stderr)
        return 1

    if resp.status_code != 200:
        print(f"Daily report send failed ({resp.status_code}): {resp.text[:500]}", file=sys.stderr)
        return 1

    result = resp.json()

    if result.get("skipped"):
        print(f"Skipped -- daily report for {result['report_date']} was already sent. Use --force to re-send.")
        return 0

    print(f"Daily report for {result['report_date']}:")
    print(f"  Today:      Rs. {result['data']['today']['amount']:,.2f} ({result['data']['today']['count']} donations)")
    print(f"  This week:  Rs. {result['data']['week']['amount']:,.2f} ({result['data']['week']['count']} donations)")
    print(f"  This month: Rs. {result['data']['month']['amount']:,.2f} ({result['data']['month']['count']} donations)")

    if result["email_recipients_count"]:
        status = "sent" if result["email_sent"] else ("FAILED -- " + result["email_error"] if result["email_error"] else "skipped (SMTP not configured)")
        print(f"  Email to {result['email_recipients_count']} recipient(s): {status}")
    else:
        print("  Email: no active email recipients configured.")

    if result["whatsapp_recipients_count"]:
        print(f"  WhatsApp: sent to {result['whatsapp_sent_count']}/{result['whatsapp_recipients_count']} recipient(s)"
              + (f" -- error: {result['whatsapp_error']}" if result["whatsapp_error"] else ""))
    else:
        print("  WhatsApp: no active WhatsApp recipients configured.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
