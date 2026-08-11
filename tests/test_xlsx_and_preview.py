"""Uploading the Excel file itself, and looking before you leap.

Two changes tested together because they answer the same complaint. A CSV
has already thrown away everything the spreadsheet knew: a date has become
"01/08/2026" with no record of which number was the month, 1100 has become
"1,100", and a phone number has become 9.87654e+09. Reading the .xlsx
instead means none of that happens -- the cell still knows it holds a
date. And the preview means an import that issues real receipt numbers and
emails donors is something you look at before it happens, not after.

The preview tests are the load-bearing ones. A preview that writes
anything is worse than no preview at all, and a preview that disagrees
with the real run is worse still, because it teaches people to trust it.
"""
import datetime
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import login

openpyxl = pytest.importorskip(
    "openpyxl", reason="openpyxl is in requirements.txt; run pip install -r requirements.txt")


@pytest.fixture
def campaign_id(app):
    from extensions import db
    from models import Campaign
    with app.app_context():
        campaign = Campaign.query.filter_by(name="Annadan").first()
        if campaign is None:
            campaign = Campaign(name="Annadan", is_80g=True)
            db.session.add(campaign)
            db.session.commit()
        return campaign.id


def _workbook(rows, headers=None):
    """An .xlsx as bytes, with values stored the way Excel stores them --
    real dates, real numbers -- not pre-stringified."""
    headers = headers or [
        "full_name", "campaign_name", "amount", "payment_mode",
        "donation_date", "phone", "bank_transaction_id",
    ]
    wb = openpyxl.Workbook()
    sheet = wb.active
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _upload(client, data, filename="donations.xlsx", url="/admin/donations/bulk-import",
            preview=False):
    payload = {"csv_file": (io.BytesIO(data), filename)}
    if preview:
        payload["action"] = "preview"
    return client.post(url, data=payload, content_type="multipart/form-data",
                       follow_redirects=True)


class TestExcelFilesUploadDirectly:

    def test_a_real_date_cell_needs_no_guessing(self, app, client, campaign_id):
        """The whole point. In a CSV, 08/01/2026 is either 8 January or
        1 August and the file doesn't say. In an .xlsx the cell holds a
        date, so this is exact."""
        from models import Donation
        login(client)
        _upload(client, _workbook([
            ["Ravi Sharma", "Annadan", 1100, "cash",
             datetime.datetime(2026, 1, 8), None, None],
        ]))
        with app.app_context():
            assert Donation.query.one().donation_date.date() == datetime.date(2026, 1, 8)

    def test_a_numeric_amount_survives(self, app, client, campaign_id):
        """Excel writes 1100 as the number 1100. Exported to CSV in an
        Indian locale it can come out "1,100", which float() rejects."""
        from models import Donation
        login(client)
        _upload(client, _workbook([
            ["Ravi Sharma", "Annadan", 1100, "cash", datetime.date(2026, 8, 1), None, None],
        ]))
        with app.app_context():
            assert float(Donation.query.one().amount) == 1100.0

    def test_a_phone_stored_as_a_number_is_not_mangled(self, app, client, campaign_id):
        """A phone number in a General-formatted cell is a float.
        Rendered naively it becomes "9876543210.0" or "9.87654321e+09",
        and either one fails phone validation and loses the row."""
        from models import Donor
        login(client)
        _upload(client, _workbook([
            ["Ravi Sharma", "Annadan", 1100, "cash",
             datetime.date(2026, 8, 1), 9876543210, None],
        ]))
        with app.app_context():
            assert Donor.query.one().phone == "9876543210"

    def test_decimal_amounts_are_not_rounded_away(self, app, client, campaign_id):
        from models import Donation
        login(client)
        _upload(client, _workbook([
            ["Ravi Sharma", "Annadan", 1100.50, "cash",
             datetime.date(2026, 8, 1), None, None],
        ]))
        with app.app_context():
            assert float(Donation.query.one().amount) == 1100.50

    def test_blank_rows_are_ignored(self, app, client, campaign_id):
        """Excel files are full of these -- a row that once had content,
        or the sheet's used range running past the data."""
        from models import Donation
        login(client)
        resp = _upload(client, _workbook([
            ["Ravi Sharma", "Annadan", 1100, "cash", datetime.date(2026, 8, 1), None, None],
            [None, None, None, None, None, None, None],
            [None, None, None, None, None, None, None],
        ]))
        with app.app_context():
            assert Donation.query.count() == 1
        assert "full_name is required" not in resp.data.decode(), \
            "blank trailing rows were reported as errors"

    def test_a_csv_still_works(self, app, client, campaign_id):
        """Adding .xlsx mustn't cost the format everyone already uses."""
        from models import Donation
        login(client)
        csv_text = (
            "full_name,campaign_name,amount,payment_mode,donation_date\n"
            "Ravi Sharma,Annadan,1100,cash,2026-08-01\n"
        ).encode()
        _upload(client, csv_text, filename="donations.csv")
        with app.app_context():
            assert Donation.query.count() == 1

    def test_an_xlsx_renamed_to_csv_says_so(self, app, client, campaign_id):
        """People do this. Through the CSV path it decodes to binary
        gibberish and every row fails validation for no visible reason."""
        login(client)
        resp = _upload(client, _workbook([
            ["Ravi Sharma", "Annadan", 1100, "cash", datetime.date(2026, 8, 1), None, None],
        ]), filename="donations.csv")
        from models import Donation
        with app.app_context():
            assert Donation.query.count() == 1, (
                "an xlsx named .csv should still be read as xlsx -- it's sniffed by content"
            )

    def test_a_csv_renamed_to_xlsx_says_so(self, app, client, campaign_id):
        login(client)
        resp = _upload(client, b"full_name,campaign_name\nX,Annadan\n",
                       filename="donations.xlsx")
        # Flashed through Jinja, so the apostrophe arrives escaped.
        assert "named like an Excel workbook" in resp.data.decode()


class TestPreviewWritesNothing:
    """The property that matters most. Everything else about a preview is
    presentation; this is the part that would make it dangerous."""

    @pytest.mark.parametrize("url,csv_text", [
        ("/admin/donations/bulk-import",
         "full_name,campaign_name,amount,payment_mode,donation_date\n"
         "Bulk One,Annadan,1100,cash,2026-08-01\n"),
        ("/admin/donations/import-legacy",
         "full_name,campaign_name,amount,payment_mode,donation_date\n"
         "Legacy One,Annadan,500,cash,2024-05-01\n"),
        ("/admin/donors/import",
         "full_name,phone\nDonor One,9811111111\n"),
    ])
    def test_nothing_reaches_the_database(self, app, client, campaign_id, url, csv_text):
        from models import Donation, Donor
        login(client)
        resp = _upload(client, csv_text.encode(), filename="f.csv", url=url, preview=True)
        assert "Preview only" in resp.data.decode()
        with app.app_context():
            assert Donation.query.count() == 0
            assert Donor.query.count() == 0

    def test_camp_preview_writes_nothing(self, app, client):
        from models import Donation, Donor
        login(client)
        client.post("/admin/iyf-camps/manage", data={"name": "Utkarsha 2026"},
                    follow_redirects=True)
        csv_text = (
            "full_name,amount,camp_name,batch_name,donation_date,payment_mode\n"
            "Camp One,2100,Utkarsha 2026,Batch A,2026-08-01,cash\n"
        ).encode()
        resp = _upload(client, csv_text, filename="f.csv",
                       url="/admin/iyf-camps/bulk", preview=True)
        assert "Preview only" in resp.data.decode()
        with app.app_context():
            assert Donation.query.count() == 0
            assert Donor.query.count() == 0

    def test_no_receipt_numbers_are_consumed(self, app, client, campaign_id):
        """Receipt numbers come from a shared counter that only goes
        forward. A preview that burned numbers would leave permanent gaps
        in the series, which is exactly the sort of thing an auditor asks
        about."""
        from models import ReceiptCounter
        login(client)
        csv_text = (
            "full_name,campaign_name,amount,payment_mode,donation_date\n"
            "Bulk One,Annadan,1100,cash,2026-08-01\n"
        ).encode()
        _upload(client, csv_text, filename="f.csv", preview=True)
        with app.app_context():
            assert ReceiptCounter.query.count() == 0

    def test_no_notifications_are_sent(self, app, client, campaign_id, monkeypatch):
        """Previewing a file with send_notifications ticked must not email
        anyone -- the donor can't un-receive a receipt for a donation that
        was never recorded."""
        import admin
        sent = []
        monkeypatch.setattr(admin, "_finalize_success",
                            lambda *a, **k: sent.append(a))
        login(client)
        csv_text = (
            "full_name,campaign_name,amount,payment_mode,donation_date,email\n"
            "Bulk One,Annadan,1100,cash,2026-08-01,donor@example.com\n"
        ).encode()
        client.post("/admin/donations/bulk-import", data={
            "csv_file": (io.BytesIO(csv_text), "f.csv"),
            "action": "preview", "send_notifications": "yes",
        }, content_type="multipart/form-data", follow_redirects=True)
        assert sent == []


class TestPreviewTellsTheTruth:
    """A preview that disagrees with the real run is worse than none --
    it's the same wrong answer, now trusted."""

    def _file(self):
        return (
            "full_name,campaign_name,amount,payment_mode,donation_date,bank_transaction_id\n"
            "Good One,Annadan,1100,cash,2026-08-01,\n"
            "Good Two,Annadan,900,online,2026-08-02,pay_X1\n"
            "No Reference,Annadan,700,online,2026-08-03,\n"
            "Bad Campaign,Nonexistent,500,cash,2026-08-04,\n"
        ).encode()

    def test_the_same_rows_import_as_the_preview_promised(self, app, client, campaign_id):
        from models import Donation
        login(client)
        preview = _upload(client, self._file(), filename="f.csv", preview=True)
        body = preview.data.decode()
        assert body.count("Would import") == 2
        assert "No Reference" in body and "Bad Campaign" in body

        _upload(client, self._file(), filename="f.csv")
        with app.app_context():
            names = {d.donor.full_name for d in Donation.query.all()}
        assert names == {"Good One", "Good Two"}, (
            "the real import didn't match what the preview showed"
        )

    def test_the_missing_reference_is_caught_in_preview_too(self, app, client, campaign_id):
        """This rule lives inside _create_offline_donation, which a
        preview never reaches -- so the preview has to check it itself or
        it would promise a row the real run refuses."""
        login(client)
        resp = _upload(client, self._file(), filename="f.csv", preview=True)
        assert "needs its transaction" in resp.data.decode()

    def test_the_total_is_shown_so_it_can_be_checked(self, app, client, campaign_id):
        """A row count doesn't catch an amount column that's a factor of
        ten out; a total checked against the cash book does."""
        login(client)
        resp = _upload(client, self._file(), filename="f.csv", preview=True)
        assert "2,000.00" in resp.data.decode()  # 1100 + 900

    def test_the_donor_preview_says_create_or_update(self, app, client):
        """The donor import overwrites existing records, so which rows
        land on someone already on file is the thing worth seeing first."""
        from extensions import db
        from models import Donor
        login(client)
        with app.app_context():
            db.session.add(Donor(full_name="Existing Donor", phone="9811111111"))
            db.session.commit()
        csv_text = (
            "full_name,phone\nExisting Donor,9811111111\nBrand New,9822222222\n"
        ).encode()
        resp = _upload(client, csv_text, filename="f.csv",
                       url="/admin/donors/import", preview=True)
        body = resp.data.decode()
        assert "Would update an existing donor" in body
        assert "Would be created" in body
