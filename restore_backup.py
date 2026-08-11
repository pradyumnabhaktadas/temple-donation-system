"""Restores a full data backup (see backup_utils.py / backup_data.py) --
reads every CSV inside a backup ZIP and reloads it into the database,
table by table, in the FK-safe order the backup was written in.

The same restore is available from the web UI at Admin -> Settings -> Data
Backup, which is the easier route for most cases and takes a safety backup
of the current data automatically. This script exists for the case where
the web UI isn't reachable -- a new host, or a database so broken the app
won't start -- and for scripted recovery.

Both front ends call backup_utils.restore_backup_zip(), so there is one
implementation of the restore logic, not two that can drift apart.

By default this UPSERTS -- rows are matched by id; an existing id gets its
columns overwritten with the backup's values, a new id gets inserted.
Nothing is deleted. Pass --wipe to first delete every row from every
backed-up table (in reverse FK order), for a true "make the database look
exactly like this backup, discarding anything newer" restore -- what you
want when restoring into a fresh or empty database. The default upsert is
safer when the current database has rows you don't want to lose.

AdminUser and DonorLoginOTP are never touched, matching what the backup
deliberately excludes: restoring credentials from an old backup would
silently roll back passwords and account lockouts. Recreate or reset admin
accounts via Admin -> Manage Users instead. Donation.receipt_pdf and
razorpay_raw_payload aren't in the backup either and are left alone --
receipt PDFs regenerate on demand (see public.download_receipt).

Usage (Render Shell / any host with the app's venv active):
    python restore_backup.py backup.zip --dry-run
    python restore_backup.py backup.zip
    python restore_backup.py backup.zip --wipe

Take a fresh backup of the CURRENT database before running this against
production (`python backup_data.py`, or Run Backup Now in the admin) --
unlike the web UI, this script does not do that for you.
"""
import argparse
import sys


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("backup_zip", help="Path to a temple_data_backup_*.zip file")
    parser.add_argument(
        "--wipe", action="store_true",
        help="Delete all existing rows in each backed-up table before restoring "
             "(use for a fresh/empty database)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would happen without writing anything",
    )
    args = parser.parse_args()

    from app import create_app
    from extensions import db
    from backup_utils import restore_backup_zip

    with open(args.backup_zip, "rb") as f:
        zip_bytes = f.read()

    app = create_app()
    with app.app_context():
        try:
            result = restore_backup_zip(db, zip_bytes, wipe=args.wipe, dry_run=args.dry_run)
        except Exception as exc:
            db.session.rollback()
            print(f"Restore failed: {exc}", file=sys.stderr)
            return 1

        if result["missing"]:
            print("Note: backup doesn't contain "
                  f"{', '.join(result['missing'])} -- skipped (existing data left as-is).")

        verb = "Would restore" if args.dry_run else "Restored"
        for csv_name, inserted, updated in result["tables"]:
            print(f"{verb} {csv_name}: {inserted} inserted, {updated} updated")

        if args.dry_run:
            db.session.rollback()
            print("Dry run -- nothing was written. Re-run without --dry-run to restore.")
        else:
            db.session.commit()
            print("Restore complete.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
