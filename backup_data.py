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
    from backup_utils import build_backup_zip
    from email_utils import send_backup_email

    app = create_app()
    with app.app_context():
        filename, zip_bytes = build_backup_zip()

        os.makedirs(args.dest, exist_ok=True)
        backup_path = os.path.join(args.dest, filename)
        with open(backup_path, "wb") as f:
            f.write(zip_bytes)
        print(f"Backed up to {backup_path} ({len(zip_bytes):,} bytes)")

        keep = app.config.get("BACKUP_RETENTION_COUNT", 12)
        backups = sorted(
            (f for f in os.listdir(args.dest) if f.startswith("temple_data_backup_") and f.endswith(".zip")),
            reverse=True,
        )
        for old in backups[keep:]:
            os.remove(os.path.join(args.dest, old))
            print(f"Pruned old backup: {old}")

        if not args.no_email:
            to_email = app.config.get("BACKUP_EMAIL") or app.config.get("ORG_CONTACT_EMAIL")
            sent = send_backup_email(app.config, to_email, filename, zip_bytes)
            if sent:
                print(f"Emailed backup to {to_email}")
            elif app.config.get("SMTP_HOST"):
                print(f"Could not email backup to {to_email} -- see logs above for the error.")
            else:
                print("SMTP not configured -- backup saved to disk only.")


if __name__ == "__main__":
    sys.exit(main())
