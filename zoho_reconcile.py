"""Issues receipts for Zoho Forms payments Zoho never confirmed to us.

Zoho Forms fires its webhook once, at form-submission time -- before the
donor has paid -- so that call carries the whole form but no transaction
ID. Zoho's docs say a second call follows with the payment result. On this
account it repeatedly hasn't: the donor's own Zoho record goes on to show
"Payment Status: Completed" with a real Razorpay transaction ID, while the
only call this app ever received was the first one. The money arrives and
nothing in the app knows about it -- no donor, no donation, no receipt.

This job closes that gap without needing Zoho to behave: it takes the
donor details from the Google Sheet Zoho writes each submission into, and
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
import datetime
import os
import sys

import requests


def _print_ignored(result):
    """One quiet line per form marked as a test form under
    Admin -> Zoho Forms. Deliberately still shown: these are real charges
    on the live gateway, and a form ticked by mistake should appear as a
    suspiciously busy line here rather than disappearing entirely."""
    ignored = result.get("ignored_payments") or []
    if not ignored:
        return
    by_form = {}
    for p in ignored:
        by_form.setdefault(p.get("source_ref") or "(unnamed)", []).append(p["amount"])
    print("Ignored as test forms (Admin -> Zoho Forms):")
    for form, amounts in sorted(by_form.items()):
        print(f"  {form}: {len(amounts)} payment(s), Rs. {sum(amounts):,.2f}")
    print()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--lookback-days", type=int, default=3,
        help="How many days of Razorpay payments to scan (default 3).",
    )
    parser.add_argument(
        "--from", dest="from_date", default=None, metavar="YYYY-MM-DD",
        help=(
            "Scan a fixed historical window instead of the last N days. "
            "REPORT ONLY: it lists payments with no donation behind them and "
            "changes nothing, because pending submissions only exist from the "
            "day this feature was deployed -- matching them against an older "
            "window would close today's open ones as unpaid."
        ),
    )
    parser.add_argument(
        "--to", dest="to_date", default=None, metavar="YYYY-MM-DD",
        help="End of the window, inclusive. Defaults to today. Use with --from.",
    )
    args = parser.parse_args()

    for value in (args.from_date, args.to_date):
        if value:
            try:
                datetime.datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                print(f"Invalid date {value!r}, expected YYYY-MM-DD", file=sys.stderr)
                return 1
    if args.to_date and not args.from_date:
        print("--to needs --from as well.", file=sys.stderr)
        return 1

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
            json={
                "lookback_days": args.lookback_days,
                "from_date": args.from_date,
                "to_date": args.to_date,
            },
            headers={"X-Internal-Token": token},
            # Generous: a wide historical window pages through a lot of
            # Razorpay history before it answers.
            timeout=600,
        )
    except requests.RequestException as exc:
        print(f"Could not reach {base_url}: {exc}", file=sys.stderr)
        return 1

    if resp.status_code != 200:
        print(f"Reconciliation failed ({resp.status_code}): {resp.text[:500]}", file=sys.stderr)
        return 1

    result = resp.json()

    if result.get("report_only"):
        payments = result["orphan_payments"]
        window = f"{args.from_date} to {args.to_date or 'today'}"
        print(f"REPORT ONLY ({window}) -- nothing was created or changed.\n")
        _print_ignored(result)
        if not payments:
            print("No captured payments in this window are missing a donation.")
            return 0
        total = sum(p["amount"] for p in payments)
        print(f"{len(payments)} captured payment(s) with no donation behind them, Rs. {total:,.2f}\n")

        # Grouped by where the payment came from -- Razorpay's own notes
        # say, and they need completely different handling. Reading a flat
        # list of 141 mixed payments and guessing which are real is how a
        # backfill turns into duplicate receipts.
        groups = {}
        for p in payments:
            groups.setdefault((p.get("source") or "unknown", p.get("source_ref")), []).append(p)

        headings = {
            "zoho": "ZOHO FORM -- no receipt was ever issued. These need backfilling.",
            "website": "WEBSITE -- donation exists but never finished. Check it before creating anything.",
            "unknown": "UNIDENTIFIED -- no origin recorded. Investigate before acting.",
        }
        for source in ("zoho", "website", "unknown"):
            keys = sorted([k for k in groups if k[0] == source], key=lambda k: str(k[1] or ""))
            if not keys:
                continue
            print(headings[source])
            for key in keys:
                rows = groups[key]
                label = f"  form {key[1]}" if source == "zoho" else (
                    f"  donation #{key[1]}" if source == "website" else "  (no origin in notes)")
                print(f"{label} -- {len(rows)} payment(s), Rs. {sum(r['amount'] for r in rows):,.2f}")
                for p in rows:
                    print(f"      {p['payment_id']} Rs. {p['amount']:>10,.2f} ({p['contact'] or 'no contact'})")
            print()
        return 0

    _print_ignored(result)

    created = result["created"]
    if created:
        print(f"Issued {len(created)} receipt(s) for payments Zoho never confirmed:")
        for d in created:
            print(f"  Rs. {d['amount']:,.2f} -- receipt {d['receipt_number']} (donation #{d['donation_id']})")
    else:
        print("No new receipts to issue.")

    if result.get("sheet_error"):
        print(f"\nThe submissions sheet could not be read: {result['sheet_error']}")
        print("Nothing was named from it this run -- payments below are unreceipted, not lost.")

    if result.get("failed"):
        print(f"\n{len(result['failed'])} entry(ies) errored while being written -- retried next run:")
        for f in result["failed"]:
            print(f"  {f}")

    orphans = result.get("orphan_payments") or []
    if orphans:
        total = sum(p["amount"] for p in orphans)
        print(f"\n{len(orphans)} captured payment(s) still without a receipt, Rs. {total:,.2f}:")
        for p in orphans:
            why = p.get("why_not_receipted")
            print(f"  {p['payment_id']} Rs. {p['amount']:>10,.2f} ({p['contact'] or 'no contact'})"
                  + (f" -- {why}" if why else ""))

    return 0


if __name__ == "__main__":
    sys.exit(main())
