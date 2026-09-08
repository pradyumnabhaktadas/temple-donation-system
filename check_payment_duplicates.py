"""Read-only check: is any gateway payment recorded on more than one donation?

Run this BEFORE the migration that puts a unique constraint on
donations.razorpay_payment_id. If duplicates already exist the migration
would fail partway through a deploy, which is the worst moment to find
out; this answers the question in advance and changes nothing.

WHY THE CONSTRAINT IS WANTED
----------------------------
Duplicate protection today is a query followed by a write:

    if Donation.query.filter(...).first(): return
    ... create the donation ...

Two writers can both pass that check before either writes. The reconcile
sweep is effectively the only writer today, so it has held -- but an admin
entering a backfill by hand (Admin -> Offline Donation) while the sweep
receipts the same payment is a real, reachable race, and pay_TUoekY3BLSiZXp
was exactly that scenario, just sequential.

What makes it consequential rather than untidy: ReceiptCounter *is*
properly locked, so the two writers would not collide on a number -- they
would each get their own. The result is two valid-looking receipts for one
payment, both landing in a Form 10BD filing. Not a crash. A correction at
the tax end.

WHAT COUNTS AS A DUPLICATE
--------------------------
The same non-empty id appearing on more than one donation, checked across
both columns that hold one:

  * razorpay_payment_id -- written by the gateway paths
  * bank_transaction_id -- written by hand for offline/backfilled entries

and checked *across* them too, since a hand-entered backfill records the
gateway id in the second column. That cross-column case is the one that
already caught us out.

"SIMULATED" is excluded. /donate/simulate sets that literal on every demo
donation, so it is legitimately repeated; the planned constraint excludes
it for the same reason. It is only reachable when Razorpay is not
configured, so production should have none -- any found are reported
separately as a curiosity rather than a problem.

Usage (Render Shell):

    python check_payment_duplicates.py

Exit code 0 means clean and the constraint can be added. Exit code 1 means
duplicates exist and need a decision first. Exit code 2 means it refused to
answer, because DATABASE_URL was not set and it would have been reporting on
a database that isn't yours -- see cli_safety.py.

Run it in the Shell of the WEB SERVICE. A cron job's shell has no
DATABASE_URL and will be refused.
"""
import collections
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

SIMULATED = "SIMULATED"


def find_duplicates(donations):
    """(duplicates, simulated_count) for an iterable of donations.

    Pure function over rows so it can be tested without a database.
    `duplicates` maps a payment id to the list of donations carrying it,
    for ids appearing more than once.
    """
    seen = collections.defaultdict(list)
    simulated = 0

    for donation in donations:
        ids = set()
        for column in ("razorpay_payment_id", "bank_transaction_id"):
            value = (getattr(donation, column, None) or "").strip()
            if not value:
                continue
            if value == SIMULATED:
                simulated += 1
                continue
            ids.add(value)
        # A single donation carrying the same id in both columns is one
        # donation, not a duplicate -- hence the set.
        for value in ids:
            seen[value].append(donation)

    return {k: v for k, v in seen.items() if len(v) > 1}, simulated


def main():
    from app import create_app
    from cli_safety import require_configured_database
    from models import Donation

    app = create_app()
    if not require_configured_database(
        app, purpose="say whether a unique constraint can be added to a production table"
    ):
        return 2

    with app.app_context():
        donations = Donation.query.order_by(Donation.id).all()
        duplicates, simulated = find_duplicates(donations)

        print(f"Checked {len(donations)} donation(s).")
        if simulated:
            print(f"  ({simulated} carry the {SIMULATED!r} placeholder from demo mode -- "
                  "expected, and excluded by the planned constraint.)")

        if not duplicates:
            print("\nNo payment id appears on more than one donation.")
            print("The unique constraint can be added safely.")
            return 0

        print(f"\n{len(duplicates)} payment id(s) appear on more than one donation:\n")
        for payment_id, rows in sorted(duplicates.items()):
            print(f"  {payment_id}")
            for d in rows:
                print(f"      donation #{d.id}  Rs. {d.amount}  "
                      f"receipt={d.receipt_number or '(none)'}  status={d.status}  "
                      f"mode={d.payment_mode}  date={d.donation_date}")
            print()

        print("Each of these is one payment recorded twice. Before the constraint can")
        print("be added, one row in each group has to be corrected or removed -- and if")
        print("both carry receipt numbers, that is a decision about a tax document,")
        print("not a cleanup. Send this output back.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
