"""Thin Render Cron client for daily admin birthday reminders.

The actual delivery runs inside the web service, so SMTP/Airtel use the same
working network path as receipts. Requires PUBLIC_BASE_URL and
INTERNAL_TASK_TOKEN, and is scheduled at 3:30 AM IST in render.yaml.
"""
import argparse
import datetime
import os
import sys

import requests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="IST date, YYYY-MM-DD (defaults to today)")
    parser.add_argument("--force", action="store_true", help="Re-send even if already sent")
    args = parser.parse_args()
    if args.date:
        try:
            datetime.datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            print("Invalid --date; expected YYYY-MM-DD", file=sys.stderr)
            return 1
    base_url = (os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")
    token = os.environ.get("INTERNAL_TASK_TOKEN") or ""
    if not base_url or not token:
        print("PUBLIC_BASE_URL and INTERNAL_TASK_TOKEN must be set.", file=sys.stderr)
        return 1
    payload = {"force": args.force}
    if args.date:
        payload["date"] = args.date
    try:
        response = requests.post(f"{base_url}/internal/birthday-alerts/send", json=payload,
                                 headers={"X-Internal-Token": token}, timeout=60)
    except requests.RequestException as exc:
        print(f"Could not reach {base_url}: {exc}", file=sys.stderr)
        return 1
    if response.status_code != 200:
        print(f"Birthday alert failed ({response.status_code}): {response.text[:500]}", file=sys.stderr)
        return 1
    print(response.text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
