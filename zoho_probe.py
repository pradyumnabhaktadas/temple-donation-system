"""Read-only look at what the Zoho Forms API actually returns.

Run this BEFORE trusting any field mapping. Zoho names entry fields after
each form's own labels, so "Payment Transaction ID" on one form may be
"payment_transaction_id", "Payment_Transaction_ID" or something else
entirely on another, and the response envelope differs by API version.
Guessing at that is how a sync ends up silently importing nothing, or
worse, importing the wrong column.

This changes nothing and writes nothing. It fetches a couple of entries
and prints them, so the mapping can be built against what this account
really sends.

Usage (Render Shell, or anywhere the app's env is set):

    python zoho_probe.py --list
    python zoho_probe.py --form <form_link_name>
    python zoho_probe.py --form <form_link_name> --raw

--list tries to enumerate the forms on the account. --form prints the
field names and values of the most recent entries for one form, with a
best guess at which fields matter flagged. --raw dumps the untouched JSON
for when the guesses need checking.

Requires ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET / ZOHO_REFRESH_TOKEN, and
ZOHO_ACCOUNTS_BASE / ZOHO_API_BASE pointing at the right data centre
(.in / .com / .eu -- the wrong one fails like a bad credential).
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import zoho_api


# Substrings worth flagging when they turn up in a field name -- these are
# what the sync needs to find. Purely advisory: nothing is skipped or
# renamed on the strength of them, they just save reading fifty fields by
# eye to work out which four matter.
INTERESTING = {
    "transaction": "the Razorpay payment id -- the single most important field",
    "payment status": "whether Zoho thinks it completed (advisory only)",
    "payment amount": "amount",
    "total amount": "amount (some forms use this instead)",
    "name": "donor name",
    "phone": "donor phone",
    "email": "donor email",
    "added time": "when it was submitted",
    "pan": "PAN, if the form collects one",
}


def _config():
    from app import create_app
    app = create_app()
    return app.config


def _flag(field_name):
    lowered = field_name.replace("_", " ").lower()
    for needle, why in INTERESTING.items():
        if needle in lowered:
            return why
    return None


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--form", help="Form link name to inspect.")
    parser.add_argument("--list", action="store_true", help="Try to list the forms on the account.")
    parser.add_argument("--count", type=int, default=3, help="How many entries to show (default 3).")
    parser.add_argument("--raw", action="store_true", help="Dump untouched JSON.")
    args = parser.parse_args()

    if not args.form and not args.list:
        parser.print_help()
        return 1

    config = _config()

    try:
        if args.list:
            print("Asking Zoho for the forms on this account...\n")
            body = zoho_api.get(config, "/api/forms")
            print(json.dumps(body, indent=2)[:6000])
            print(
                "\nLook for each form's link name in the above -- that's what --form takes, "
                "and what the sync will be configured with."
            )
            return 0

        print(f"Fetching up to {args.count} entries for form {args.form!r}...\n")
        records, envelope = zoho_api.entries(config, args.form, start_index=1, limit=args.count)
        print(f"Envelope key: {envelope!r}   Records returned: {len(records)}\n")

        if not records:
            print(
                "No records came back. Either the form has no entries, the link name is wrong,\n"
                "or the response is shaped differently than expected -- re-run with --raw to see."
            )
            if args.raw:
                print(json.dumps(zoho_api.get(config, f"/api/{args.form}/records"), indent=2)[:6000])
            return 0

        if args.raw:
            print(json.dumps(records, indent=2)[:12000])
            return 0

        for i, record in enumerate(records, start=1):
            print(f"--- entry {i} " + "-" * 50)
            for key, value in record.items():
                why = _flag(key)
                shown = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
                if len(shown) > 120:
                    shown = shown[:117] + "..."
                marker = f"   <== {why}" if why else ""
                print(f"  {key:<40} {shown}{marker}")
            print()

        print(
            "Send this output back and the field mapping gets built from it.\n"
            "The fields marked above are the ones the sync needs; anything unmarked is\n"
            "form-specific and ignored."
        )
        return 0

    except zoho_api.ZohoApiError as exc:
        print(f"\nZoho API error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
