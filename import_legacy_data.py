"""One-time importer for historical donor/donation data from your old Zoho
Forms / Excel sheets.

Your old data lives in several different spreadsheets (one per campaign).
This script expects you to first consolidate them into ONE CSV with these
columns (any extra columns are ignored):

    full_name, phone, email, pan, address, city, state, pincode,
    campaign_name, amount, payment_mode, donation_date, receipt_number

Notes:
  - whatsapp_number (optional): only needed if a donor's WhatsApp number
    differs from their `phone` -- leave the column out entirely, or leave
    it blank per-row, and it'll just fall back to `phone`.
  - donation_date should be in YYYY-MM-DD format.
  - payment_mode should be one of: cash, cheque, bank_transfer, online
    (defaults to "cash" if blank -- adjust the CSV if that's wrong for a row).
  - campaign_name is matched case-insensitively against your existing
    Campaign records (Admin -> Campaigns). If a campaign_name in the CSV
    doesn't exist yet, the row is skipped and reported at the end -- create
    the campaign first (Admin -> Campaigns -> New Campaign), then re-run.
  - receipt_number: if your old records already have receipt numbers you
    want to preserve for continuity, put them in this column and they'll be
    kept as-is instead of generating new ones. Leave blank to auto-generate
    using the same numbering scheme as new donations.
  - Donor de-duplication uses the exact same PAN -> phone -> email matching
    logic as the live donation form, so importing won't create duplicates
    of donors who already exist (e.g. if some of this year's data has
    already come in through the new system).
  - This script does NOT generate PDF receipts for imported rows by default
    (it would be slow and pointless for old paper receipts). Pass
    --generate-receipts if you do want PDFs for every imported row.

Usage:
    python import_legacy_data.py path/to/consolidated_history.csv
    python import_legacy_data.py path/to/consolidated_history.csv --generate-receipts
    python import_legacy_data.py path/to/consolidated_history.csv --dry-run
"""
import argparse
import csv
import datetime
import sys

from app import create_app
from extensions import db
from models import Campaign, Donation, ReceiptCounter
from pdf_utils import generate_receipt_pdf
from public import find_or_create_donor, _org_cfg

VALID_MODES = {"cash", "cheque", "bank_transfer", "online"}


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("csv_path", help="Path to the consolidated legacy data CSV")
    p.add_argument("--generate-receipts", action="store_true", help="Generate a PDF receipt for every imported row")
    p.add_argument("--dry-run", action="store_true", help="Validate and report without writing to the database")
    return p.parse_args()


def main():
    args = parse_args()
    app = create_app()

    with app.app_context():
        campaigns_by_name = {c.name.strip().lower(): c for c in Campaign.query.all()}
        if not campaigns_by_name:
            print("No campaigns exist yet. Run `python seed.py` first (or create campaigns in the admin panel).")
            sys.exit(1)

        imported = 0
        skipped = []

        with open(args.csv_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            required = {"full_name", "campaign_name", "amount", "donation_date"}
            missing = required - set(reader.fieldnames or [])
            if missing:
                print(f"CSV is missing required column(s): {', '.join(sorted(missing))}")
                sys.exit(1)

            for i, row in enumerate(reader, start=2):  # start=2 to match spreadsheet row numbers (1 = header)
                campaign_name = (row.get("campaign_name") or "").strip()
                campaign = campaigns_by_name.get(campaign_name.lower())
                if campaign is None:
                    skipped.append((i, f"Unknown campaign '{campaign_name}' -- create it first"))
                    continue

                try:
                    amount = float(row["amount"])
                    if amount <= 0:
                        raise ValueError
                except (ValueError, KeyError):
                    skipped.append((i, f"Invalid amount '{row.get('amount')}'"))
                    continue

                try:
                    donation_date = datetime.datetime.strptime(row["donation_date"].strip(), "%Y-%m-%d")
                except (ValueError, KeyError, AttributeError):
                    skipped.append((i, f"Invalid donation_date '{row.get('donation_date')}' (expected YYYY-MM-DD)"))
                    continue

                if not (row.get("full_name") or "").strip():
                    skipped.append((i, "Missing full_name"))
                    continue

                payment_mode = (row.get("payment_mode") or "cash").strip().lower()
                if payment_mode not in VALID_MODES:
                    payment_mode = "cash"

                if args.dry_run:
                    imported += 1
                    continue

                donor = find_or_create_donor(row)

                donation = Donation(
                    donor_id=donor.id,
                    campaign_id=campaign.id,
                    amount=amount,
                    payment_mode=payment_mode,
                    status="success",
                    donation_date=donation_date,
                    remarks="Imported from legacy records",
                    recorded_by="import",
                )
                db.session.add(donation)
                db.session.flush()

                existing_receipt = (row.get("receipt_number") or "").strip()
                if existing_receipt:
                    donation.receipt_number = existing_receipt
                    from utils import get_financial_year
                    donation.financial_year = get_financial_year(donation_date)
                else:
                    receipt_number, fy = ReceiptCounter.next_receipt_number(campaign.is_80g, donation_date)
                    donation.receipt_number = receipt_number
                    donation.financial_year = fy

                if args.generate_receipts:
                    donation.receipt_pdf = generate_receipt_pdf(donation, donor, campaign, _org_cfg())

                db.session.commit()

                imported += 1

        if args.dry_run:
            print(f"[DRY RUN] Would import {imported} row(s). Nothing was written.")
        else:
            print(f"Imported {imported} donation(s).")

        if skipped:
            print(f"\nSkipped {len(skipped)} row(s):")
            for row_num, reason in skipped:
                print(f"  Row {row_num}: {reason}")


if __name__ == "__main__":
    main()
