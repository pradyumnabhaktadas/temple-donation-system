"""Wipes ALL data from the database -- every donor, donation, receipt,
campaign, BACE property, festival, seva type, Live To Give purpose,
activity log entry, and admin login. This is IRREVERSIBLE. Meant for
clearing out seed/test data before importing the real donor/donation
history for go-live.

Only deletes rows -- does not touch the schema (tables/columns), so there's
no need to re-run migrations afterward.

SAFETY -- please read before running:
  1. Take a backup first: Admin -> Data Backup -> "Run Backup Now", even if
     you believe everything currently in the database is test data. It
     takes a few seconds and costs nothing; this script can't undo itself.
  2. This refuses to run unless you pass --yes on the command line AND then
     type the confirmation phrase when prompted.
  3. It always prints exactly what it's about to delete (row counts) before
     asking for that confirmation.

After running this, your admin login is gone too. Run `python seed.py`
immediately afterward -- it recreates the default campaigns, BACE
properties, festivals, seva types, and Live To Give purposes, plus a
default admin login (username: admin, password: ChangeMe123!, forced
password change on first login) since none will exist anymore.

Usage (run on the machine/shell that has your production DATABASE_URL set,
e.g. Render's shell -- NOT a random local machine pointed at a dev DB by
mistake):
    python reset_data.py --yes
"""
import sys

from app import create_app
from extensions import db
from models import (
    Camp, Donation, Donor, Campaign, BaceProperty, Festival, SevaType,
    LiveToGivePurpose, Preacher, AssociatedWith, ReceiptCounter, DonorLoginOTP,
    AdminActivityLog, AdminUser,
)

CONFIRMATION_PHRASE = "DELETE ALL DATA"

# Deletion order matters -- children before parents, so foreign key
# constraints never block a delete (Donation references donors/campaigns/
# bace_properties/festivals/seva_types/live_to_give_purposes/
# associated_withs; Donor references preachers).
#
# Every model in models.py must appear here. tests/test_reset_data.py
# enforces that by comparing this list against SQLAlchemy's own registry --
# Camp was added to the app and missed here, so a reset silently left test
# camps behind and didn't even mention them in the summary it printed
# before asking for confirmation. A list maintained by hand needs something
# checking it.
MODELS_IN_DELETE_ORDER = [
    Donation,
    DonorLoginOTP,
    AdminActivityLog,
    ReceiptCounter,
    Donor,
    Campaign,
    BaceProperty,
    Festival,
    SevaType,
    LiveToGivePurpose,
    Preacher,
    AssociatedWith,
    Camp,
    AdminUser,
]


def main():
    app = create_app()
    with app.app_context():
        counts = {m: m.query.count() for m in MODELS_IN_DELETE_ORDER}
        total = sum(counts.values())

        print("This will PERMANENTLY delete:")
        for m, c in counts.items():
            print(f"  {m.__tablename__:>22}: {c} row(s)")
        print(f"  {'TOTAL':>22}: {total} row(s)")

        if total == 0:
            print("\nDatabase is already empty. Nothing to do.")
            return

        if "--yes" not in sys.argv:
            print(
                "\nNothing has been touched. Take a backup first (Admin -> Data "
                "Backup -> Run Backup Now), then re-run with --yes to proceed to "
                "the confirmation prompt."
            )
            sys.exit(1)

        print(
            f"\nType exactly \"{CONFIRMATION_PHRASE}\" (without quotes) to confirm, "
            "or anything else to cancel:"
        )
        typed = input("> ").strip()
        if typed != CONFIRMATION_PHRASE:
            print("Confirmation phrase didn't match. Cancelled -- nothing was deleted.")
            sys.exit(1)

        for m in MODELS_IN_DELETE_ORDER:
            m.query.delete()
        db.session.commit()

        print(
            "\nAll data deleted. Run `python seed.py` now to restore the default "
            "campaigns, BACE properties, festivals, seva types, Live To Give "
            "purposes, associated-with options, and a default admin login."
        )


if __name__ == "__main__":
    main()
