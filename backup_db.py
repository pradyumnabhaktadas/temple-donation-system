"""Simple SQLite backup script -- donation and donor data is too important
to have zero backups. Copies instance/temple.db to instance/backups/ with a
timestamp, and prunes old backups beyond a retention count.

If you've moved to Postgres (via DATABASE_URL in .env), this script isn't
what you want -- use your host's managed backup feature or `pg_dump`
instead; this only handles the default SQLite setup.

Usage:
    python backup_db.py                  # keeps the last 30 backups
    python backup_db.py --keep 60        # keep more/fewer
    python backup_db.py --dest /path/to/external/backups

Suggested: run this daily via cron or your host's scheduled jobs, e.g.
    0 2 * * * cd /path/to/temple-donation-system && venv/bin/python backup_db.py
"""
import argparse
import datetime
import os
import shutil
import sys

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "instance", "temple.db")
DEFAULT_BACKUP_DIR = os.path.join(BASE_DIR, "instance", "backups")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--keep", type=int, default=30, help="Number of recent backups to retain (default: 30)")
    parser.add_argument("--dest", default=DEFAULT_BACKUP_DIR, help="Backup directory (default: instance/backups)")
    args = parser.parse_args()

    if not os.path.exists(DB_PATH):
        print(f"No database found at {DB_PATH} -- nothing to back up.")
        sys.exit(1)

    os.makedirs(args.dest, exist_ok=True)

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(args.dest, f"temple_{timestamp}.db")
    shutil.copy2(DB_PATH, backup_path)
    print(f"Backed up to {backup_path}")

    backups = sorted(
        (f for f in os.listdir(args.dest) if f.startswith("temple_") and f.endswith(".db")),
        reverse=True,
    )
    for old in backups[args.keep:]:
        os.remove(os.path.join(args.dest, old))
        print(f"Pruned old backup: {old}")


if __name__ == "__main__":
    main()
