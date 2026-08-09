"""Weekly full data backup -- donors, donations, and every admin-editable
lookup list, exported as a ZIP of CSV files (see backup_utils.py). Works
against either SQLite or Postgres since it goes through the SQLAlchemy ORM
rather than a database-specific dump tool.

Saves the ZIP to instance/backups/ (pruning old backups beyond
BACKUP_RETENTION_COUNT), and additionally emails it to BACKUP_EMAIL (or
ORG_CONTACT_EMAIL as a fallback) if SMTP is configured -- see config.py.
Neither step is required for the other to succeed: a save-only run (no SMTP
configured) is still a complete backup; an email failure doesn't undo the
file already written to disk.

Usage (Render Shell / any host with the app's venv active):
    python backup_data.py
    python backup_data.py --dest /path/to/external/backups
    python backup_data.py --no-email

Intended to run weekly via a scheduled job -- see render.yaml's
"temple-weekly-backup" Cron Job service (Render plans that support Cron
Jobs only; if yours doesn't, run this from any external scheduler -- e.g.
a free cron-job.org trigger hitting a protected endpoint, or your own
machine's crontab -- pointed at this same command).
"""
import argparse
import os
import sys

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DEFAULT_BACKUP_DIR = os.path.join(BASE_DIR, "instance", "backups")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dest", default=DEFAULT_BACKUP_DIR, help="Backup directory (default: instance/backups)")
    parser.add_argument("--no-email", action="store_true", help="Skip emailing the backup even if SMTP is configured")
    args = parser.parse_args()

    from app import create_app
    from backup_utils import run_backup

    app = create_app()
    with app.app_context():
        result = run_backup(app, dest_dir=args.dest, send_email=not args.no_email)

        print(f"Backed up to {result['saved_path']} ({result['size_bytes']:,} bytes)")
        for old in result["pruned"]:
            print(f"Pruned old backup: {old}")

        if result["email_sent"]:
            print(f"Emailed backup to {result['emailed_to']}")
        elif result["emailed_to"]:
            print(f"Could not email backup to {result['emailed_to']} -- see logs above for the error.")
        elif result["email_skipped_reason"]:
            print(result["email_skipped_reason"])


if __name__ == "__main__":
    sys.exit(main())
