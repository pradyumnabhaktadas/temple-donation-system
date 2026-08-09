"""Restores a full data backup (see backup_utils.py / backup_data.py) --
reads every CSV inside a backup ZIP and reloads it into the database,
table by table, in the exact same FK-safe order the backup was written in
(backup_utils._BACKUP_TABLES). Meant for disaster recovery: the database
was lost/corrupted/needs re-seeding on a new host, and you have a ZIP from
either the weekly Cron Job email or "Run Backup Now" in Admin -> Settings
-> Data Backup.

By default this UPSERTS -- rows are matched by id; a row whose id already
exists gets its columns overwritten with the backup's values, a row whose
id doesn't exist yet gets inserted. Nothing is deleted. Pass --wipe to
first delete every row from every backed-up table (in reverse FK order)
before restoring, for a true "make the database look exactly like this
backup, discarding anything newer" restore -- that's what you want when
restoring into a fresh/empty database (a new host, or recovering from
data loss); the default upsert mode is safer when the current database
already has rows you don't want to lose and you just want the backup's
rows layered in.

Two tables are deliberately never touched by this script, matching what
backup_utils.py deliberately excludes from the ZIP in the first place:
AdminUser and DonorLoginOTP (credentials/login secrets -- restoring these
from an old backup would silently roll back passwords/account lockouts,
which is almost never what you want; recreate/reset admin accounts via
Admin -> Manage Users instead). Donation.receipt_pdf and
razorpay_raw_payload also aren't in the backup and are left as they are
after restore -- receipt PDFs regenerate automatically the next time
they're downloaded/emailed (see pdf_utils.generate_receipt_pdf); nothing
else in the app depends on that column being pre-populated.

Usage (Render Shell / any host with the app's venv active):
    python restore_backup.py path/to/temple_data_backup_20260101_020000.zip --dry-run
    python restore_backup.py path/to/temple_data_backup_20260101_020000.zip
    python restore_backup.py backup.zip --wipe

Always take a fresh backup of the CURRENT database (Admin -> Settings ->
Data Backup -> Run Backup Now, or `python backup_data.py`) before running
this against production, in case something needs undoing afterwards --
this script does not do that for you automatically.
"""
import argparse
import csv
import datetime
import decimal
import io
import sys
import zipfile


def _convert_value(raw, py_type):
    """Turns one CSV string field back into the Python value SQLAlchemy
    expects, based on the column's declared Python type -- the exact
    inverse of how backup_utils._table_to_csv_bytes wrote it out via plain
    csv.writer(row) (which stringifies everything with str(), and writes
    None as an empty CSV field)."""
    if raw is None or raw == "":
        return None
    if py_type is bool:
        return raw.strip().lower() in ("true", "1", "t", "yes")
    if py_type is int:
        return int(raw)
    if py_type is float:
        return float(raw)
    if py_type is decimal.Decimal:
        return decimal.Decimal(raw)
    if py_type is datetime.datetime:
        return datetime.datetime.fromisoformat(raw)
    if py_type is datetime.date:
        return datetime.date.fromisoformat(raw)
    return raw


def _row_to_values(raw_row, columns):
    """`columns` is {column_name: SQLAlchemy Column}."""
    return {name: _convert_value(raw_row.get(name), col.type.python_type) for name, col in columns.items()}


def _restore_table(db, model, exclude_columns, csv_bytes, dry_run):
    columns = {c.name: c for c in model.__table__.columns if c.name not in exclude_columns}
    reader = csv.DictReader(io.StringIO(csv_bytes.decode("utf-8")))

    pk_name = list(model.__table__.primary_key.columns.keys())[0]
    inserted = updated = 0
    for raw_row in reader:
        values = _row_to_values(raw_row, columns)
        pk_value = values.get(pk_name)
        existing = model.query.get(pk_value) if pk_value is not None else None
        if existing:
            updated += 1
            if not dry_run:
                for name, value in values.items():
                    setattr(existing, name, value)
        else:
            inserted += 1
            if not dry_run:
                db.session.add(model(**values))

    if not dry_run:
        db.session.flush()
    return inserted, updated


def _sync_postgres_sequence(db, model):
    """After restoring rows with explicit id values, Postgres's own
    auto-increment sequence doesn't know about them and would try to
    reuse an id the next time a row is inserted normally -- bump it past
    the highest id actually present. No-op on SQLite, which doesn't use a
    separate sequence object for this."""
    if db.engine.dialect.name != "postgresql":
        return
    table = model.__table__.name
    pk_name = list(model.__table__.primary_key.columns.keys())[0]
    db.session.execute(
        db.text(
            f"SELECT setval(pg_get_serial_sequence('{table}', '{pk_name}'), "
            f"COALESCE((SELECT MAX({pk_name}) FROM {table}), 1))"
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("backup_zip", help="Path to a temple_data_backup_*.zip file")
    parser.add_argument(
        "--wipe", action="store_true",
        help="Delete all existing rows in each backed-up table before restoring (use for a fresh/empty database)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report what would happen without writing anything")
    args = parser.parse_args()

    from app import create_app
    from extensions import db
    from backup_utils import _BACKUP_TABLES

    app = create_app()
    with app.app_context():
        with zipfile.ZipFile(args.backup_zip) as zf:
            names = set(zf.namelist())
            tables = [(csv_name, model, exclude) for csv_name, model, exclude in _BACKUP_TABLES if csv_name in names]
            missing = [csv_name for csv_name, _, _ in _BACKUP_TABLES if csv_name not in names]
            if missing:
                print(f"Note: backup doesn't contain {', '.join(missing)} -- skipping (leaving existing data as-is).")

            if args.wipe and not args.dry_run:
                print("--wipe: deleting existing rows from every backed-up table first...")
                # Reverse order so FK-referencing tables (donations) are
                # cleared before the lookup tables they point at.
                for _, model, _ in reversed(tables):
                    model.query.delete()
                db.session.flush()

            for csv_name, model, exclude in tables:
                csv_bytes = zf.read(csv_name)
                inserted, updated = _restore_table(db, model, exclude, csv_bytes, dry_run=args.dry_run)
                if not args.dry_run:
                    _sync_postgres_sequence(db, model)
                verb = "Would restore" if args.dry_run else "Restored"
                print(f"{verb} {csv_name}: {inserted} to insert, {updated} to update"
                      if args.dry_run else f"{verb} {csv_name}: {inserted} inserted, {updated} updated")

        if args.dry_run:
            db.session.rollback()
            print("Dry run -- nothing was written. Re-run without --dry-run to actually restore.")
        else:
            db.session.commit()
            print("Restore complete.")


if __name__ == "__main__":
    sys.exit(main())
