"""import_legacy_data.py -- the CLI historical importer.

Exists mainly to pin two things that were wrong in it and are easy for a
CLI script to drift back into, since nothing exercises it on every change
the way the web routes are exercised by clicking around the admin panel:

1. It used to mint a real receipt number from this site's own sequence
   (032511/ISK500000...) for a row with none in the CSV. That misrepresents
   an old paper donation as an official receipt this site issued, and it
   burns numbers out of the live series for donations that predate the
   site. The admin panel's own Historical Import has never done this;
   the CLI script was the one path that disagreed.
2. It parsed donation_date with a bare strptime("%Y-%m-%d"), so a file
   that had been opened and re-saved in Excel -- which reformats dates to
   the machine's locale, e.g. 22/01/2024 -- imported cleanly through the
   admin panel and was rejected row-by-row here.

Both are fixed by routing through the same helpers the web importers use
(_import_datetime / _parse_import_date in admin.py), so this file is
mostly a parity check: whatever the CLI does, it should do the same thing
the browser-based importer does for the identical CSV.
"""
import csv
import io
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _write_csv(path, rows, fieldnames=None):
    fieldnames = fieldnames or [
        "full_name", "campaign_name", "amount", "donation_date",
        "receipt_number", "phone", "payment_mode",
    ]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _run(app, csv_path, extra_argv=()):
    import import_legacy_data
    argv = ["import_legacy_data.py", str(csv_path), *extra_argv]
    with patch.object(sys, "argv", argv), \
         patch("import_legacy_data.create_app", return_value=app):
        import_legacy_data.main()


@pytest.fixture
def campaign(app):
    from extensions import db
    from models import Campaign
    with app.app_context():
        c = Campaign.query.filter_by(name="Annadan").first()
        if c is None:
            c = Campaign(name="Annadan", is_80g=True)
            db.session.add(c)
            db.session.commit()
        return c.id


class TestNoReceiptNumberIsMinted:
    """The regression this file exists to catch."""

    def test_a_row_with_no_receipt_number_gets_none(self, app, campaign, tmp_path):
        from models import Donation
        path = tmp_path / "history.csv"
        _write_csv(path, [
            {"full_name": "Old Donor", "campaign_name": "Annadan", "amount": "500",
             "donation_date": "2024-05-01", "receipt_number": "", "phone": "9811111111",
             "payment_mode": "cash"},
        ])
        _run(app, path)
        with app.app_context():
            donation = Donation.query.one()
            assert donation.receipt_number is None
            assert donation.financial_year == "2024-25"

    def test_the_live_receipt_series_is_never_touched(self, app, campaign, tmp_path):
        """The strongest form of the assertion: not just "no number was
        printed on this donation" but "nothing was taken from the counter
        that a real donation might have needed."""
        from models import ReceiptCounter
        path = tmp_path / "history.csv"
        _write_csv(path, [
            {"full_name": f"Old Donor {i}", "campaign_name": "Annadan", "amount": "500",
             "donation_date": "2024-05-01", "receipt_number": "", "phone": "",
             "payment_mode": "cash"}
            for i in range(5)
        ])
        _run(app, path)
        with app.app_context():
            assert ReceiptCounter.query.count() == 0

    def test_an_existing_receipt_number_is_preserved_verbatim(self, app, campaign, tmp_path):
        from models import Donation
        path = tmp_path / "history.csv"
        _write_csv(path, [
            {"full_name": "Old Donor", "campaign_name": "Annadan", "amount": "500",
             "donation_date": "2024-05-01", "receipt_number": "OLD/2024/00456",
             "phone": "", "payment_mode": "cash"},
        ])
        _run(app, path)
        with app.app_context():
            assert Donation.query.one().receipt_number == "OLD/2024/00456"

    def test_no_pdf_is_generated_for_a_row_with_no_receipt_number(self, app, campaign, tmp_path):
        """Even with --generate-receipts: there's no number to print on
        the PDF, so generating one would be a receipt for nothing."""
        from models import Donation
        path = tmp_path / "history.csv"
        _write_csv(path, [
            {"full_name": "Old Donor", "campaign_name": "Annadan", "amount": "500",
             "donation_date": "2024-05-01", "receipt_number": "", "phone": "",
             "payment_mode": "cash"},
        ])
        _run(app, path, extra_argv=("--generate-receipts",))
        with app.app_context():
            assert Donation.query.one().receipt_pdf is None

    def test_generate_receipts_still_works_when_a_number_exists(self, app, campaign, tmp_path):
        from models import Donation
        path = tmp_path / "history.csv"
        _write_csv(path, [
            {"full_name": "Old Donor", "campaign_name": "Annadan", "amount": "500",
             "donation_date": "2024-05-01", "receipt_number": "OLD/2024/00456",
             "phone": "", "payment_mode": "cash"},
        ])
        _run(app, path, extra_argv=("--generate-receipts",))
        with app.app_context():
            pdf = Donation.query.one().receipt_pdf
            assert pdf and pdf.startswith(b"%PDF")


class TestExcelReformattedDatesAreAccepted:
    """The second regression: this parser used to be a bare strptime."""

    @pytest.mark.parametrize("date_value,expected", [
        ("2024-05-01", "2024-05-01"),
        ("01/05/2024", "2024-05-01"),   # Excel, day-first
        ("22/01/2024", "2024-01-22"),   # unambiguous day-first (no 22nd month)
        ("01/05/24", "2024-05-01"),     # two-digit year
    ])
    def test_date_formats_excel_produces(self, app, campaign, tmp_path, date_value, expected):
        import datetime
        from models import Donation
        path = tmp_path / "history.csv"
        _write_csv(path, [
            {"full_name": "Old Donor", "campaign_name": "Annadan", "amount": "500",
             "donation_date": date_value, "receipt_number": "", "phone": "",
             "payment_mode": "cash"},
        ])
        _run(app, path)
        with app.app_context():
            donation = Donation.query.one()
            assert donation.donation_date.date() == datetime.date.fromisoformat(expected), (
                f"'{date_value}' should read as {expected}"
            )

    def test_an_unparseable_date_is_skipped_not_crashed_on(self, app, campaign, tmp_path, capsys):
        path = tmp_path / "history.csv"
        _write_csv(path, [
            {"full_name": "Old Donor", "campaign_name": "Annadan", "amount": "500",
             "donation_date": "not a date", "receipt_number": "", "phone": "",
             "payment_mode": "cash"},
        ])
        _run(app, path)
        from models import Donation
        with app.app_context():
            assert Donation.query.count() == 0
        assert "donation_date" in capsys.readouterr().out


class TestDryRunWritesNothing:

    def test_dry_run_does_not_touch_the_database(self, app, campaign, tmp_path):
        from models import Donation, Donor
        path = tmp_path / "history.csv"
        _write_csv(path, [
            {"full_name": "Old Donor", "campaign_name": "Annadan", "amount": "500",
             "donation_date": "2024-05-01", "receipt_number": "", "phone": "9811111111",
             "payment_mode": "cash"},
        ])
        _run(app, path, extra_argv=("--dry-run",))
        with app.app_context():
            assert Donation.query.count() == 0
            assert Donor.query.count() == 0

    def test_dry_run_reports_the_count_it_would_import(self, app, campaign, tmp_path, capsys):
        path = tmp_path / "history.csv"
        _write_csv(path, [
            {"full_name": "Old Donor", "campaign_name": "Annadan", "amount": "500",
             "donation_date": "2024-05-01", "receipt_number": "", "phone": "",
             "payment_mode": "cash"},
        ])
        _run(app, path, extra_argv=("--dry-run",))
        assert "Would import 1 row" in capsys.readouterr().out


class TestUnknownCampaignIsSkippedNotFatal:

    def test_one_bad_row_does_not_block_the_rest_of_the_file(self, app, campaign, tmp_path):
        from models import Donation
        path = tmp_path / "history.csv"
        _write_csv(path, [
            {"full_name": "Good Donor", "campaign_name": "Annadan", "amount": "500",
             "donation_date": "2024-05-01", "receipt_number": "", "phone": "",
             "payment_mode": "cash"},
            {"full_name": "Bad Donor", "campaign_name": "Nonexistent", "amount": "500",
             "donation_date": "2024-05-01", "receipt_number": "", "phone": "",
             "payment_mode": "cash"},
        ])
        _run(app, path)
        with app.app_context():
            names = {d.donor.full_name for d in Donation.query.all()}
        assert names == {"Good Donor"}
