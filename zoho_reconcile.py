"""Runs the durable Zoho Forms entry reconciliation queue.

Zoho Forms fires its webhook once, at form-submission time -- before the
donor has paid -- so that call carries the whole form but no transaction
ID. Zoho's docs say a second call follows with the payment result. On this
account it repeatedly hasn't: the donor's own Zoho record goes on to show
"Payment Status: Completed" with a real Razorpay transaction ID, while the
only call this app ever received was the first one. The money arrives and
nothing in the app knows about it -- no donor, no donation, no receipt.

The webhook is gone entirely, and nothing depends on it any more. This job
asks Razorpay which payments it actually captured, then asks Zoho's own
API who made each one, matching **on the transaction ID and nothing
else** -- no phone, no amount, no timing. A match names the donor; the
donation and receipt are created from there. Anything unmatched is printed
with the reason rather than guessed at. See
public.reconcile_zoho_submissions() for why the rules are that strict.

Requires the Zoho API credentials (ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET /
ZOHO_REFRESH_TOKEN). Without them every payment is simply reported, as it
would be for any other unresolved payment -- nothing breaks, but nothing
is receipted automatically either.

Thin HTTP client on purpose, same as daily_report.py -- it POSTs to the
already-running web app rather than doing the work itself, because that
Cron Job container's outbound networking has proven unreliable while the
web service's is fine (see daily_report.py's docstring for that history).

Usage (Render Shell / any host with the app's venv active):
    python zoho_reconcile.py
    python zoho_reconcile.py

Requires PUBLIC_BASE_URL and INTERNAL_TASK_TOKEN in the environment,
shared with the web service -- see render.yaml.

Intended to run every 15 minutes -- see render.yaml's "temple-zoho-sync"
Cron Job. Receipt issuing is controlled only by ZOHO_AUTOMATION_MODE on
the web service: off (default), shadow (verify but don't issue), or live.
"""
import argparse
import json
import os
import sys

import requests


def main():
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()

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
            f"{base_url}/internal/zoho-entry-sync",
            json={},
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

    print(json.dumps(result, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
