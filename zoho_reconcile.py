"""Issues receipts for Zoho Forms payments Zoho never confirmed to us.

Zoho Forms fires its webhook once, at form-submission time -- before the
donor has paid -- so that call carries the whole form but no transaction
ID. Zoho's docs say a second call follows with the payment result. On this
account it repeatedly hasn't: the donor's own Zoho record goes on to show
"Payment Status: Completed" with a real Razorpay transaction ID, while the
only call this app ever received was the first one. The money arrives and
nothing in the app knows about it -- no donor, no donation, no receipt.

This job closes that gap without needing Zoho to behave: it takes the
donor details from that first call (stored as PendingZohoSubmission) and
matches them against the payments Razorpay itself confirms it captured,
then creates the donation and issues the receipt exactly as the webhook
would have. See public.reconcile_zoho_submissions() for the matching rules
and why they're deliberately strict.

Thin HTTP client on purpose, same as daily_report.py -- it POSTs to the
already-running web app rather than doing the work itself, because that
Cron Job container's outbound networking has proven unreliable while the
web service's is fine (see daily_report.py's docstring for that history).

Usage (Render Shell / any host with the app's venv active):
    python zoho_reconcile.py
    python zoho_reconcile.py --lookback-days 7   # widen the scan

Requires PUBLIC_BASE_URL and INTERNAL_TASK_TOKEN in the environment,
shared with the web service -- see render.yaml.

Intended to run hourly -- see render.yaml's "temple-zoho-reconcile" Cron
Job. Hourly rather than daily because a donor who paid should get their
receipt while they still remember donating, not the next morning.
"""
import argparse
import os
import sys

import requests


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--lookback-days", type=int, default=3,
        help="How many days of Razorpay payments to scan (default 3).",
    )
    args = parser.parse_args()

    base_url = (os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")
    token = os.environ.get("INTERNAL_TASK_TOKEN") or ""

    if not base_url:
        print("PUBLIC_BASE_URL is not set -- don't know which app to trigger.", file=sys.stderr)
        return 1
    if not token:
        print("INTERNAL_TASK_TOKEN is not set -- refusing to call an unauthenticated endpoint.", file=sys.stderr)
        return 1

    try:
        resp = requests.post(
            f"{base_url}/internal/zoho-reconcile",
            json={"lookback_days": args.lookback_days},
            headers={"X-Internal-Token": token},
            timeout=120,
        )
    except requests.RequestException as exc:
        print(f"Could not reach {base_url}: {exc}", file=sys.stderr)
        return 1

    if resp.status_code != 200:
        print(f"Reconciliation failed ({resp.status_code}): {resp.text[:500]}", file=sys.stderr)
        return 1

    result = resp.json()

    created = result["created"]
    if created:
        print(f"Issued {len(created)} receipt(s) for payments Zoho never confirmed:")
        for d in created:
            print(f"  Rs. {d['amount']:,.2f} -- receipt {d['receipt_number']} (donation #{d['donation_id']})")
    else:
        print("No new receipts to issue.")

    if result["ambiguous"]:
        print(f"\n{len(result['ambiguous'])} submission(s) need a human -- couldn't be matched safely:")
        for s in result["ambiguous"]:
            print(f"  #{s['pending_id']} {s['name']} Rs. {s['amount']}: {s['note']}")

    if result.get("failed"):
        print(f"\n{len(result['failed'])} submission(s) errored while being written -- retried next run:")
        for f in result["failed"]:
            print(f"  pending #{f['pending_id']}: {f['error']}")

    if result["orphan_payments"]:
        print(f"\n{len(result['orphan_payments'])} captured payment(s) with nothing in this app to match them to:")
        for p in result["orphan_payments"]:
            print(f"  {p['payment_id']} Rs. {p['amount']:,.2f} ({p['contact'] or 'no contact'})")

    print(f"\nStill awaiting payment: {result['still_waiting']}. Closed as unpaid: {result['unpaid']}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
