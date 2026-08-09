"""Builds a full data backup as a ZIP of CSV files, one per table -- donors,
donations, campaigns, and every admin-editable lookup list (BACE
properties, festivals, seva types, Live To Give purposes, preachers).

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
import io
import os
import zipfile

from models import (
    Donor, Donation, Campaign, BaceProperty, Festival, SevaType,
    LiveToGivePurpose, Preacher, ReceiptCounter,
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

    dest = dest_dir or os.path.join(app.root_path, "instance", "backups")
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
