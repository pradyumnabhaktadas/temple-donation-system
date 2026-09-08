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

    python zoho_probe.py --diagnose          <-- start here
    python zoho_probe.py --list
    python zoho_probe.py --form <form_link_name>
    python zoho_probe.py --form <form_link_name> --raw

--diagnose is the one to run first. It checks every form configured under
Admin -> Zoho Forms (or just --form <name>) and answers the three
questions the sync design rests on, none of which can be settled without
a live account:

  * what ORDER Zoho returns entries in -- the reconciler reads a bounded
    number of pages, so oldest-first ordering on a busy form would mean
    recent payments are never reached, while the report says "Zoho has no
    entry carrying this transaction id", which reads like the entry
    doesn't exist rather than like we stopped looking;
  * how many entries each form actually holds, which decides whether that
    matters today or is only a latent risk;
  * whether the transaction id and donor name can be extracted from a
    real entry -- so far that parser has only ever run on hand-written
    payloads.

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


# --- diagnose ---------------------------------------------------------
#
# --form/--list answer "what does an entry look like". They do not answer
# the three questions the sync design actually rests on, each of which can
# only be settled against a live account:
#
#   1. What ORDER does /records return entries in? The reconciler reads a
#      bounded number of pages. If Zoho returns oldest first, a form with
#      more entries than that bound would never have its recent payments
#      matched -- silently, and while reporting "Zoho has no entry
#      carrying this transaction id", which reads like the entry doesn't
#      exist rather than like we didn't look far enough.
#   2. How many entries does a form actually hold? If every form is well
#      under the bound, question 1 is moot today, and the fix is a guard
#      rather than a redesign.
#   3. Does zoho_sync's parser find a real transaction id in a real entry?
#      Every test of it so far has run against payloads I wrote.
#
# Read-only, like the rest of this script.

DIAGNOSE_PAGE = 200
DIAGNOSE_MAX_PAGES = 25          # 5,000 entries -- enough to size any of
                                 # these forms without crawling forever


def _entry_times(records):
    """Parsed Added Time for each record, in the order Zoho returned them.
    None for anything unparseable rather than dropped, so positions still
    line up with the records."""
    import zoho_sync
    return [zoho_sync.parse_added_time(r) for r in records]


def _describe_order(times):
    known = [t for t in times if t is not None]
    if len(known) < 2:
        return "unknown", "too few parseable timestamps to tell"
    ascending = all(a <= b for a, b in zip(known, known[1:]))
    descending = all(a >= b for a, b in zip(known, known[1:]))
    if descending and not ascending:
        return "newest-first", "newest entries come back first"
    if ascending and not descending:
        return "oldest-first", "OLDEST entries come back first"
    if ascending and descending:
        return "unknown", "every timestamp is identical -- can't tell"
    return "unsorted", "neither ascending nor descending"


def _diagnose_form(config, form_name, deep=True):
    import zoho_sync

    print("=" * 72)
    print(f"FORM: {form_name}")
    print("=" * 72)

    # --- 1. can we reach it at all, and what shape comes back ---
    try:
        first, envelope = zoho_api.entries(
            config, form_name, start_index=1, limit=DIAGNOSE_PAGE,
        )
    except zoho_api.ZohoApiError as exc:
        print(f"  UNREACHABLE: {exc}")
        print("  Nothing below could be checked for this form.\n")
        return {"form": form_name, "reachable": False, "error": str(exc)}

    print(f"  Reachable. Envelope key {envelope!r}, first page returned {len(first)} record(s).")
    if not first:
        print("  No entries at all -- nothing further to check.\n")
        return {"form": form_name, "reachable": True, "total": 0}

    # _records_from() is deliberately forgiving about the envelope, so it
    # can hand back a list holding things that aren't entries. Reported
    # rather than crashed on: if this account returns a shape we don't
    # expect, that is exactly what this script exists to discover, and it
    # is useless if it dies while discovering it.
    non_dict = [r for r in first if not isinstance(r, dict)]
    if non_dict:
        print(f"  NOTE: {len(non_dict)}/{len(first)} items on the first page are not "
              f"objects (e.g. {non_dict[0]!r:.60}).")
        print("  The envelope is probably being read wrongly -- re-run with --raw.")
    first = [r for r in first if isinstance(r, dict)]
    if not first:
        print("  Nothing usable on the first page.\n")
        return {"form": form_name, "reachable": True, "total": 0, "bad_shape": True}

    # --- 2. ordering ---
    times = _entry_times(first)
    order, why = _describe_order(times)
    parseable = sum(1 for t in times if t is not None)
    print(f"  Added Time parsed on {parseable}/{len(first)} records.")
    print(f"  ORDER: {order}  ({why})")
    if times and times[0] is not None and times[-1] is not None:
        print(f"    first record: {times[0]}")
        print(f"    last  record: {times[-1]}")

    # --- 3. how many entries are there ---
    total = len(first)
    hit_cap = False
    if deep and len(first) == DIAGNOSE_PAGE:
        for page in range(2, DIAGNOSE_MAX_PAGES + 1):
            try:
                more, _ = zoho_api.entries(
                    config, form_name,
                    start_index=total + 1, limit=DIAGNOSE_PAGE,
                )
            except zoho_api.ZohoApiError as exc:
                print(f"  Paging stopped early at page {page}: {exc}")
                break
            if not more:
                break
            total += len(more)
            if len(more) < DIAGNOSE_PAGE:
                break
        else:
            hit_cap = True
    print(f"  TOTAL ENTRIES: {'more than ' if hit_cap else ''}{total}")

    # --- 4. does our parser find real transaction ids ---
    with_id = [(r, zoho_sync.payment_ids(r)[0]) for r in first]
    found = [(r, pid) for r, pid in with_id if pid]
    print(f"  Transaction ids extracted from first page: {len(found)}/{len(first)}")
    for record, pid in found[:3]:
        name = zoho_sync.donor_name(record)
        raw = zoho_sync.field(record, "transaction")
        status = zoho_sync.field(record, "status")
        print(f"    {pid}  name={name!r}  status={status!r}")
        if raw and raw != pid:
            print(f"      (raw field value: {raw!r})")
    if not found:
        print("    NONE. Either these entries predate the payment field, or the")
        print("    transaction is stored somewhere payment_ids() doesn't look.")
        print("    Re-run with --form <name> --raw and send the output.")

    # --- 5. the verdict that matters for the page cap ---
    print()
    if order == "oldest-first" and total > 1000:
        print("  >>> ACTION NEEDED: oldest-first ordering with more than 1000 entries.")
        print("  >>> The current bounded crawl would never reach recent payments here.")
    elif order == "oldest-first":
        print(f"  >>> Oldest-first, but only {total} entries -- within the current bound,")
        print("  >>> so nothing is being missed today. Still worth making order-agnostic.")
    elif order == "newest-first":
        print("  >>> Newest-first: the current bounded crawl is safe for this form.")
    else:
        print("  >>> Ordering could not be determined -- treat the crawl as unsafe")
        print("  >>> and make it order-agnostic.")
    print()

    return {
        "form": form_name, "reachable": True, "total": total,
        "order": order, "ids_on_first_page": len(found), "hit_cap": hit_cap,
    }


def _forms_to_diagnose(config, explicit):
    """The forms to look at: whatever was asked for, else every active
    form configured under Admin -> Zoho Forms."""
    if explicit:
        return [explicit]

    from flask import current_app
    from models import ZohoForm

    def _names():
        return [f.api_link_name for f in ZohoForm.query.filter_by(is_active=True).all()
                if f.form_key]

    # Reuse the application context if there already is one -- creating a
    # second app here would quietly bind to a different database than the
    # caller's, which is a confusing way for a read-only script to report
    # "no forms configured".
    if current_app:
        return _names()
    from app import create_app
    with create_app().app_context():
        return _names()


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
    parser.add_argument(
        "--diagnose", action="store_true",
        help=("Settle the questions the sync design depends on: record ordering, "
              "how many entries each form holds, and whether a real transaction id "
              "can be extracted. With no --form, checks every active form "
              "configured under Admin -> Zoho Forms. Read-only."),
    )
    parser.add_argument(
        "--shallow", action="store_true",
        help="With --diagnose, skip counting all entries (one page per form only).",
    )
    args = parser.parse_args()

    if not args.form and not args.list and not args.diagnose:
        parser.print_help()
        return 1

    config = _config()

    if args.diagnose:
        forms = _forms_to_diagnose(config, args.form)
        if not forms:
            print("No forms to check: pass --form <link_name>, or configure one under")
            print("Admin -> Settings -> Zoho Forms first.")
            return 1
        print(f"Checking {len(forms)} form(s). This only reads.\n")
        results = [_diagnose_form(config, f, deep=not args.shallow) for f in forms]

        print("=" * 72)
        print("SUMMARY")
        print("=" * 72)
        for r in results:
            if not r.get("reachable"):
                print(f"  {r['form']:<45} UNREACHABLE")
                continue
            total = f"{'>' if r.get('hit_cap') else ''}{r.get('total', 0)}"
            print(f"  {r['form']:<45} {r.get('order','?'):<13} "
                  f"{total:>7} entries, {r.get('ids_on_first_page', 0)} id(s) on page 1")
        print("\nSend this whole output back -- the sync gets built from it rather")
        print("than from an assumption about how Zoho paginates.")
        return 0

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
