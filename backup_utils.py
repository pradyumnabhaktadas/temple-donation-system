"""Builds a full data backup as a ZIP of CSV files, one per table -- donors,
donations, campaigns, and every admin-editable lookup list (BACE
properties, festivals, seva types, Live To Give purposes, preachers,
associated-with options).

Deliberately built on the SQLAlchemy ORM rather than a database-specific
dump tool (pg_dump, sqlite3 .dump, etc.) so it works identically whether
the app is running on SQLite (local dev) or Postgres (production on
Render) -- see backup_db.py for a SQLite-file-copy alternative that's
faster for local dev but explicitly doesn't work against Postgres.

Two intentional exclusions, both documented inline below:
- AdminUser / DonorLoginOTP tables -- credentials/login secrets, not
  "data" in the sense this backup is protecting, and shouldn't leave the
  database in a portable file.
- Donation.receipt_pdf / Donation.razorpay_raw_payload -- large binary/blob
  columns that would bloat every CSV row for little benefit; the
  receipt PDF can always be regenerated from the row's own data via
  pdf_utils.generate_receipt_pdf, and the raw payload is a debugging aid,
  not something a restore actually needs.
"""
import csv
import datetime
import decimal
import io
import os
import zipfile

from models import (
    Donor, Donation, Campaign, BaceProperty, Festival, SevaType,
    LiveToGivePurpose, Preacher, AssociatedWith, ReceiptCounter, DailyReportRecipient,
)

# (CSV filename, model, columns-to-exclude) -- one entry per table included
# in the backup, in a sensible read-back order (lookup tables before the
# donations that reference them).
_BACKUP_TABLES = [
    ("preachers.csv", Preacher, []),
    ("campaigns.csv", Campaign, []),
    ("bace_properties.csv", BaceProperty, []),
    ("festivals.csv", Festival, []),
    ("seva_types.csv", SevaType, []),
    ("live_to_give_purposes.csv", LiveToGivePurpose, []),
    ("associated_withs.csv", AssociatedWith, []),
    ("daily_report_recipients.csv", DailyReportRecipient, []),
    ("donors.csv", Donor, []),
    ("donations.csv", Donation, ["receipt_pdf", "razorpay_raw_payload"]),
    ("receipt_counters.csv", ReceiptCounter, []),
]


def _table_to_csv_bytes(model, exclude_columns):
    columns = [c.name for c in model.__table__.columns if c.name not in exclude_columns]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    for row in model.query.order_by(model.id).all():
        writer.writerow([getattr(row, col) for col in columns])
    return buf.getvalue().encode("utf-8")


def build_backup_zip():
    """Returns (filename, zip_bytes) for a complete backup taken right now."""
    timestamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"temple_data_backup_{timestamp}.zip"

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for csv_name, model, exclude in _BACKUP_TABLES:
            zf.writestr(csv_name, _table_to_csv_bytes(model, exclude))
        zf.writestr(
            "README.txt",
            "Temple donation system -- full data backup\n"
            f"Generated: {datetime.datetime.utcnow().isoformat()}Z\n\n"
            "Each CSV is a full snapshot of one table at the time this backup was taken. "
            "Admin login credentials, donor OTP codes, and stored receipt PDFs are "
            "deliberately not included -- receipt PDFs can be regenerated from a "
            "donation's own data, and credentials shouldn't leave the database.\n",
        )

    return filename, zip_buf.getvalue()


def run_backup(app, dest_dir=None, send_email=True):
    """Runs a full backup end-to-end: build the ZIP, save it to disk,
    prune old backups beyond BACKUP_RETENTION_COUNT, and (optionally) email
    it. Shared by backup_data.py (the weekly Cron Job script) and the
    admin "Run Backup Now" button (admin.trigger_backup) so both go through
    the exact same routine instead of two copies drifting apart.

    Must be called inside an app context (or pass the already-created
    `app` and call from within `with app.app_context():` -- either way
    `app` is used only for its .config, not to push a context itself,
    since the caller may already be inside one).

    Returns a dict describing what happened -- callers decide how to show
    it (print() for the CLI, flash messages for the admin UI):
        {
            "filename": str,
            "size_bytes": int,
            "saved_path": str,
            "pruned": [str, ...],
            "emailed_to": str | None,
            "email_sent": bool,
            "email_skipped_reason": str | None,
        }
    """
    from email_utils import send_backup_email

    filename, zip_bytes = build_backup_zip()

    # BACKUP_DIR lets the destination be redirected without changing any
    # caller -- the test suite points it at a temp directory so running the
    # tests doesn't leave real backup ZIPs in the developer's instance/
    # folder, which is what happened before it existed.
    dest = dest_dir or app.config.get("BACKUP_DIR") or os.path.join(
        app.root_path, "instance", "backups"
    )
    os.makedirs(dest, exist_ok=True)
    saved_path = os.path.join(dest, filename)
    with open(saved_path, "wb") as f:
        f.write(zip_bytes)

    keep = app.config.get("BACKUP_RETENTION_COUNT", 12)
    backups = sorted(
        (f for f in os.listdir(dest) if f.startswith("temple_data_backup_") and f.endswith(".zip")),
        reverse=True,
    )
    pruned = []
    for old in backups[keep:]:
        os.remove(os.path.join(dest, old))
        pruned.append(old)

    result = {
        "filename": filename,
        "size_bytes": len(zip_bytes),
        "saved_path": saved_path,
        "pruned": pruned,
        "emailed_to": None,
        "email_sent": False,
        "email_skipped_reason": None,
    }

    if send_email:
        to_email = app.config.get("BACKUP_EMAIL") or app.config.get("ORG_CONTACT_EMAIL")
        if not app.config.get("SMTP_HOST"):
            result["email_skipped_reason"] = "SMTP not configured"
        elif not to_email:
            result["email_skipped_reason"] = "No BACKUP_EMAIL/ORG_CONTACT_EMAIL configured"
        else:
            sent = send_backup_email(app.config, to_email, filename, zip_bytes)
            result["emailed_to"] = to_email
            result["email_sent"] = sent
            if not sent:
                result["email_skipped_reason"] = "SMTP send failed -- see logs"
    else:
        result["email_skipped_reason"] = "Email skipped (--no-email)"

    return result


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------
# The inverse of build_backup_zip(). Lives here, next to the code that
# writes the backup, so the two can't drift: a column added to a backup
# CSV is read back by the same table definition that wrote it.
#
# Used by both restore_backup.py (CLI, for disaster recovery from a shell)
# and admin.restore_backup_upload (the Data Backup page). One
# implementation, two front ends -- this codebase has been bitten before by
# the same logic existing twice and the copies diverging.

def _convert_value(raw, py_type):
    """Turn one CSV string field back into the value SQLAlchemy expects.

    The exact inverse of _table_to_csv_bytes above, which stringifies
    everything with csv.writer and writes None as an empty field.
    """
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


def _restore_table(db, model, exclude_columns, csv_bytes, dry_run):
    """Upsert one table's CSV. Rows are matched by primary key: an
    existing id has its columns overwritten, a new id is inserted. Nothing
    is deleted here -- that's what wipe does, at the caller's level."""
    columns = {c.name: c for c in model.__table__.columns if c.name not in exclude_columns}
    reader = csv.DictReader(io.StringIO(csv_bytes.decode("utf-8", errors="replace")))
    pk_name = list(model.__table__.primary_key.columns.keys())[0]

    inserted = updated = 0
    for raw_row in reader:
        values = {
            name: _convert_value(raw_row.get(name), col.type.python_type)
            for name, col in columns.items()
        }
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
    """Rows restored with explicit ids leave Postgres's auto-increment
    sequence behind, so the next normal insert would collide on an id that
    already exists. Bump it past the highest id present. No-op on SQLite,
    which has no separate sequence object."""
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


def restore_backup_zip(db, zip_bytes, wipe=False, dry_run=True):
    """Restore a backup ZIP into the database.

    dry_run=True (the default, deliberately) reports what would change and
    writes nothing -- the caller decides whether to commit to it. Any
    caller that wants the destructive version has to ask for it explicitly.

    wipe=True clears every backed-up table first, in reverse order so
    donations go before the lookup tables they reference, giving a true
    "make the database look exactly like this backup" restore. Without it
    the backup's rows are layered over whatever is already there, which is
    the safer choice when the current database holds rows the backup
    predates.

    AdminUser and DonorLoginOTP are never touched, matching what
    build_backup_zip deliberately leaves out: restoring credentials from an
    old backup would silently roll back passwords.

    Returns {"tables": [(csv_name, inserted, updated)], "missing": [...]}.
    Raises on a file that isn't a readable ZIP -- the caller reports that.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = set(zf.namelist())
        tables = [(n, m, e) for n, m, e in _BACKUP_TABLES if n in names]
        missing = [n for n, _, _ in _BACKUP_TABLES if n not in names]

        if not tables:
            raise ValueError(
                "That ZIP doesn't contain any recognised backup CSVs "
                "(expected files like donations.csv, donors.csv)."
            )

        if wipe and not dry_run:
            for _, model, _ in reversed(tables):
                model.query.delete()
            db.session.flush()

        results = []
        for csv_name, model, exclude in tables:
            inserted, updated = _restore_table(db, model, exclude, zf.read(csv_name), dry_run)
            if not dry_run:
                _sync_postgres_sequence(db, model)
            results.append((csv_name, inserted, updated))

    return {"tables": results, "missing": missing}
