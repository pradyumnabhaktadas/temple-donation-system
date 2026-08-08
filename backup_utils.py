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
