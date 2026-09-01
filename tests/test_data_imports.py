"""The three data-import paths, tested against what they actually promise.

These write donor records and donation rows carrying receipt numbers into
the legal record, and they're used exactly when someone is moving years of
history in one go -- the worst time to discover a column is silently
ignored or a date lands in the wrong year.

The existing upload tests only proved each route could read a file and
handle one happy row. These go through the documented columns, the
duplicate and update semantics, and the failure modes real spreadsheets
produce.
"""
import io
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import login


def _post(client, url, text):
    return client.post(
        url,
        data={"csv_file": (io.BytesIO(text.encode("utf-8")), "data.csv")},
        content_type="multipart/form-data", follow_redirects=True,
    )


# ---------------------------------------------------------------------------
# Donor master import
# ---------------------------------------------------------------------------

class TestDonorImport:
    URL = "/admin/donors/import"

    def test_all_documented_columns_are_stored(self, app, client):
        """Every column the page advertises must actually land on the
        donor -- a silently ignored column is worse than a rejected one,
        because nobody notices until the data is needed.

        donor_type/donation_frequency are closed vocabularies (see
        models.DONOR_TYPES); the importer rejects anything else, which is
        why this uses real values rather than plausible-looking ones."""
        from models import Donor
        login(client)
        _post(client, self.URL,
            "full_name,phone,whatsapp_number,email,pan,address,city,state,pincode,"
            "donor_type,donation_frequency,gifts,dob,marriage_anniversary,additional_info\n"
            "Ravi Sharma,9876543210,9811111111,ravi@example.com,ABCDE1234F,"
            "45 Preet Vihar,Delhi,Delhi,110092,"
            "iyf,monthly,Gita,1985-04-12,2010-11-25,Prefers evening calls\n")
        d = Donor.query.filter_by(full_name="Ravi Sharma").one()
        assert d.phone == "9876543210"
        assert d.whatsapp_number == "9811111111"
        assert d.email == "ravi@example.com"
        assert d.pan == "ABCDE1234F"
        assert d.city == "Delhi" and d.pincode == "110092"
        assert d.dob.isoformat() == "1985-04-12"
        assert d.marriage_anniversary.isoformat() == "2010-11-25"
        assert d.additional_info == "Prefers evening calls"

    def test_reimporting_updates_rather_than_duplicating(self, app, client):
        """Re-uploading a corrected sheet is normal. It must not double
        every donor."""
        from models import Donor
        login(client)
        _post(client, self.URL, "full_name,phone,city\nRavi Sharma,9876543210,Delhi\n")
        _post(client, self.URL, "full_name,phone,city\nRavi Sharma,9876543210,Mumbai\n")
        assert Donor.query.filter_by(phone="9876543210").count() == 1
        assert Donor.query.filter_by(phone="9876543210").one().city == "Mumbai"

    def test_blank_cells_do_not_erase_existing_values(self, app, client):
        """A partial sheet (say, just updating phones) must not wipe the
        addresses that aren't in it."""
        from models import Donor
        login(client)
        _post(client, self.URL, "full_name,phone,city,email\nRavi Sharma,9876543210,Delhi,r@e.com\n")
        _post(client, self.URL, "full_name,phone,city,email\nRavi Sharma,9876543210,,\n")
        d = Donor.query.filter_by(phone="9876543210").one()
        assert d.city == "Delhi", "a blank cell erased an existing value"
        assert d.email == "r@e.com"

    def test_excel_reformatted_dates_are_accepted(self, app, client):
        """Opening a CSV in Excel and saving turns 1985-04-12 into
        12/04/85. Rejecting that would fail most real uploads."""
        from models import Donor
        login(client)
        _post(client, self.URL, "full_name,phone,dob\nRavi Sharma,9876543210,12/04/1985\n")
        d = Donor.query.filter_by(phone="9876543210").one()
        assert d.dob.isoformat() == "1985-04-12", f"day-first date misread as {d.dob}"

    def test_unparseable_date_is_reported_not_silently_dropped(self, app, client):
        login(client)
        resp = _post(client, self.URL, "full_name,phone,dob\nRavi Sharma,9876543210,not-a-date\n")
        assert b"dob" in resp.data

    def test_bad_pan_is_rejected(self, app, client):
        login(client)
        resp = _post(client, self.URL, "full_name,phone,pan\nRavi,9876543210,NOTAPAN\n")
        assert b"PAN" in resp.data or b"pan" in resp.data

    def test_preacher_linked_by_name(self, app, client):
        from extensions import db
        from models import Donor, Preacher
        login(client)
        db.session.add(Preacher(name="HG Gopal Das"))
        db.session.commit()
        _post(client, self.URL,
              "full_name,phone,connected_preacher_name\nRavi,9876543210,HG Gopal Das\n")
        d = Donor.query.filter_by(phone="9876543210").one()
        assert d.connected_preacher_id is not None

    def test_row_without_a_name_is_skipped_not_crashed(self, app, client):
        from models import Donor
        login(client)
        resp = _post(client, self.URL, "full_name,phone\n,9876543210\nReal Donor,9811111111\n")
        assert resp.status_code == 200
        assert Donor.query.filter_by(full_name="Real Donor").count() == 1


# ---------------------------------------------------------------------------
# Bulk (current) donation import
# ---------------------------------------------------------------------------

class TestBulkDonationImport:
    URL = "/admin/donations/bulk-import"

    def test_receipt_numbers_are_issued_and_unique(self, app, client):
        from models import Donation
        login(client)
        rows = "".join(
            f"Donor {i},Annadan,{100+i},cash,2026-08-0{i}\n" for i in range(1, 6))
        _post(client, self.URL,
              "full_name,campaign_name,amount,payment_mode,donation_date\n" + rows)
        receipts = [d.receipt_number for d in Donation.query.all()]
        assert len(receipts) == 5
        assert all(receipts), "a donation was imported without a receipt number"
        assert len(set(receipts)) == 5, "receipt numbers were reused"

    def test_amounts_and_dates_land_correctly(self, app, client):
        from models import Donation
        login(client)
        _post(client, self.URL,
            "full_name,campaign_name,amount,payment_mode,donation_date\n"
            "Ravi Sharma,Annadan,1234.56,cash,2026-08-01\n")
        d = Donation.query.one()
        assert float(d.amount) == 1234.56, "amount drifted"
        assert d.donation_date.date().isoformat() == "2026-08-01"

    def test_unknown_campaign_is_rejected_not_invented(self, app, client):
        from models import Campaign, Donation
        login(client)
        resp = _post(client, self.URL,
            "full_name,campaign_name,amount,payment_mode,donation_date\n"
            "Ravi,No Such Campaign,100,cash,2026-08-01\n")
        assert Donation.query.count() == 0
        assert Campaign.query.filter_by(name="No Such Campaign").count() == 0
        assert b"No Such Campaign" in resp.data

    def test_bad_payment_mode_rejected(self, app, client):
        from models import Donation
        login(client)
        _post(client, self.URL,
            "full_name,campaign_name,amount,payment_mode,donation_date\n"
            "Ravi,Annadan,100,bitcoin,2026-08-01\n")
        assert Donation.query.count() == 0

    def test_negative_and_zero_amounts_rejected(self, app, client):
        from models import Donation
        login(client)
        _post(client, self.URL,
            "full_name,campaign_name,amount,payment_mode,donation_date\n"
            "A,Annadan,-100,cash,2026-08-01\nB,Annadan,0,cash,2026-08-01\n")
        assert Donation.query.count() == 0

    def test_one_bad_row_does_not_lose_the_good_ones(self, app, client):
        """A 500-row sheet with one typo must still import 499."""
        from models import Donation
        login(client)
        resp = _post(client, self.URL,
            "full_name,campaign_name,amount,payment_mode,donation_date\n"
            "Good One,Annadan,100,cash,2026-08-01\n"
            "Bad Row,Annadan,abc,cash,2026-08-01\n"
            "Good Two,Annadan,200,cash,2026-08-02\n")
        assert Donation.query.count() == 2
        assert b"Bad Row" in resp.data

    def test_cheque_and_reference_columns_are_stored(self, app, client):
        from models import Donation
        login(client)
        _post(client, self.URL,
            "full_name,campaign_name,amount,payment_mode,donation_date,cheque_number,cheque_bank_name\n"
            "Ravi,Annadan,500,cheque,2026-08-01,123456,HDFC Bank\n")
        d = Donation.query.one()
        assert d.cheque_number == "123456"
        assert d.cheque_bank_name == "HDFC Bank"

    def test_receipt_type_controls_80g(self, app, client):
        from models import Donation
        login(client)
        _post(client, self.URL,
            "full_name,campaign_name,amount,payment_mode,donation_date,receipt_type\n"
            "NonG,Annadan,500,cash,2026-08-01,non80g\n")
        assert Donation.query.one().effective_is_80g is False

    def test_notifications_off_by_checkbox(self, app, client):
        """Importing back-dated history must not email hundreds of donors."""
        from unittest.mock import patch
        login(client)
        with patch("admin._send_receipt_notifications_background") as send:
            client.post(self.URL, data={
                "csv_file": (io.BytesIO(
                    b"full_name,campaign_name,amount,payment_mode,donation_date\n"
                    b"Ravi,Annadan,100,cash,2026-08-01\n"), "d.csv"),
                # send_notifications deliberately absent = unticked
            }, content_type="multipart/form-data", follow_redirects=True)
        send.assert_not_called()

    def test_donors_are_reused_not_duplicated(self, app, client):
        from models import Donor, Donation
        login(client)
        _post(client, self.URL,
            "full_name,phone,campaign_name,amount,payment_mode,donation_date\n"
            "Ravi Sharma,9876543210,Annadan,100,cash,2026-08-01\n"
            "Ravi Sharma,9876543210,Annadan,200,cash,2026-08-02\n")
        assert Donation.query.count() == 2
        assert Donor.query.filter_by(phone="9876543210").count() == 1


# ---------------------------------------------------------------------------
# Historical / legacy import
# ---------------------------------------------------------------------------

class TestLegacyImport:
    URL = "/admin/donations/import-legacy"

    def test_existing_receipt_numbers_are_preserved(self, app, client):
        """The whole point of this importer: old receipts already exist on
        paper, and issuing new numbers for them would break the audit
        trail and the 10BD filing."""
        from models import Donation
        login(client)
        _post(client, self.URL,
            "full_name,campaign_name,amount,donation_date,receipt_number\n"
            "Gopal Das,Annadan,11000,2023-06-10,OLD/2023/00456\n")
        assert Donation.query.one().receipt_number == "OLD/2023/00456"

    def test_no_new_receipt_number_is_issued_when_blank(self, app, client):
        """A legacy row with no receipt number must stay blank rather than
        being handed a number from the current series -- that would
        interleave old donations into this year's numbering."""
        from models import Donation
        login(client)
        _post(client, self.URL,
            "full_name,campaign_name,amount,donation_date,receipt_number\n"
            "No Receipt,Annadan,500,2024-11-03,\n")
        assert not Donation.query.one().receipt_number

    def test_duplicate_receipt_number_is_reported(self, app, client):
        from models import Donation
        login(client)
        csv = ("full_name,campaign_name,amount,donation_date,receipt_number\n"
               "One,Annadan,100,2023-06-10,OLD/2023/00456\n")
        _post(client, self.URL, csv)
        resp = _post(client, self.URL, csv)
        assert Donation.query.count() == 1, "the same old receipt was imported twice"
        assert b"duplicate" in resp.data.lower() or b"OLD/2023/00456" in resp.data

    def test_historical_dates_keep_their_year(self, app, client):
        """A 2023 donation must not land in the current financial year."""
        from models import Donation
        login(client)
        _post(client, self.URL,
            "full_name,campaign_name,amount,donation_date,receipt_number\n"
            "Gopal Das,Annadan,11000,2023-06-10,OLD/2023/1\n")
        d = Donation.query.one()
        assert d.donation_date.year == 2023
        assert d.financial_year == "2023-24", f"wrong FY: {d.financial_year}"

    def test_excel_reformatted_dates_accepted(self, app, client):
        from models import Donation
        login(client)
        _post(client, self.URL,
            "full_name,campaign_name,amount,donation_date,receipt_number\n"
            "Gopal Das,Annadan,11000,10/06/2023,OLD/2023/2\n")
        assert Donation.query.one().donation_date.date().isoformat() == "2023-06-10"

    def test_legacy_import_never_notifies(self, app, client):
        """Emailing a receipt for a donation from 2023 would be alarming."""
        from unittest.mock import patch
        login(client)
        with patch("admin._send_receipt_notifications_background") as send:
            _post(client, self.URL,
                "full_name,campaign_name,amount,donation_date,receipt_number,email\n"
                "Gopal Das,Annadan,11000,2023-06-10,OLD/2023/3,gopal@example.com\n")
        send.assert_not_called()

    def test_is_80g_flag_is_honoured(self, app, client):
        from models import Donation
        login(client)
        _post(client, self.URL,
            "full_name,campaign_name,amount,donation_date,receipt_number,is_80g_requested\n"
            "A,Annadan,100,2024-03-14,OLD/1,no\n")
        assert Donation.query.one().effective_is_80g is False

    def test_totals_match_the_file(self, app, client):
        """What went in must equal what the sheet said -- the check anyone
        migrating years of history will actually do."""
        from extensions import db
        from models import Donation
        login(client)
        amounts = [11000, 2500, 750.50, 100]
        rows = "".join(
            f"Donor {i},Annadan,{a},2023-06-1{i},OLD/2023/1{i}\n"
            for i, a in enumerate(amounts))
        _post(client, self.URL,
              "full_name,campaign_name,amount,donation_date,receipt_number\n" + rows)
        total = db.session.query(db.func.sum(Donation.amount)).scalar()
        assert float(total) == sum(amounts), f"{total} != {sum(amounts)}"
        assert Donation.query.count() == len(amounts)


class TestLegacyImportSingleEntry:
    """The single-record counterpart to TestLegacyImport above -- same
    _create_legacy_donation() helper, so most of these mirror the bulk
    assertions one row at a time, plus the extra validation this entry
    point does inline instead of via a results table."""
    URL = "/admin/donations/import-legacy/single"

    def _campaign_id(self, name="Annadan"):
        from models import Campaign
        return Campaign.query.filter_by(name=name).one().id

    def _post(self, client, **overrides):
        data = {
            "full_name": "Gopal Das",
            "campaign_id": str(self._campaign_id()),
            "amount": "11000",
            "donation_date": "2023-06-10",
            "payment_mode": "cash",
        }
        data.update(overrides)
        return client.post(self.URL, data=data, follow_redirects=True)

    def test_existing_receipt_number_is_preserved(self, app, client):
        from models import Donation
        login(client)
        self._post(client, receipt_number="OLD/2023/00456")
        assert Donation.query.one().receipt_number == "OLD/2023/00456"

    def test_no_receipt_number_stays_blank(self, app, client):
        from models import Donation
        login(client)
        self._post(client)
        assert not Donation.query.one().receipt_number

    def test_full_name_is_required(self, app, client):
        from models import Donation
        login(client)
        resp = self._post(client, full_name="")
        assert Donation.query.count() == 0
        assert b"name" in resp.data.lower()

    def test_invalid_campaign_is_rejected(self, app, client):
        from models import Donation
        login(client)
        self._post(client, campaign_id="999999")
        assert Donation.query.count() == 0

    def test_invalid_pan_is_rejected(self, app, client):
        from models import Donation
        login(client)
        self._post(client, pan="NOTAPAN")
        assert Donation.query.count() == 0

    def test_invalid_amount_is_rejected(self, app, client):
        from models import Donation
        login(client)
        self._post(client, amount="not-a-number")
        assert Donation.query.count() == 0

    def test_invalid_date_is_rejected(self, app, client):
        from models import Donation
        login(client)
        self._post(client, donation_date="not-a-date")
        assert Donation.query.count() == 0

    def test_historical_date_keeps_its_year(self, app, client):
        from models import Donation
        login(client)
        self._post(client, donation_date="2023-06-10")
        d = Donation.query.one()
        assert d.donation_date.year == 2023
        assert d.financial_year == "2023-24"

    def test_duplicate_receipt_number_is_reported(self, app, client):
        from models import Donation
        login(client)
        self._post(client, receipt_number="OLD/2023/DUP")
        resp = self._post(client, receipt_number="OLD/2023/DUP", full_name="Second Donor")
        assert Donation.query.count() == 1, "the same old receipt was imported twice"
        assert b"duplicate" in resp.data.lower()

    def test_is_80g_flag_is_honoured(self, app, client):
        from models import Donation
        login(client)
        self._post(client, is_80g_requested="no")
        assert Donation.query.one().effective_is_80g is False

    def test_never_notifies(self, app, client):
        from unittest.mock import patch
        login(client)
        with patch("admin._send_receipt_notifications_background") as send:
            self._post(client, email="gopal@example.com")
        send.assert_not_called()

    def test_no_pdf_without_a_receipt_number_even_if_requested(self, app, client):
        """generate_pdfs is opt-in, but there's nothing to print a PDF
        receipt number on when the row has none -- same rule as the bulk
        importer's no_receipt_pdf_skipped counter."""
        from models import Donation
        login(client)
        self._post(client, generate_pdfs="yes")
        assert not Donation.query.one().receipt_pdf

    def test_pdf_generated_when_receipt_number_given_and_requested(self, app, client):
        from models import Donation
        login(client)
        self._post(client, receipt_number="OLD/2023/PDF1", generate_pdfs="yes")
        assert Donation.query.one().receipt_pdf

    def test_donor_is_deduplicated_with_existing_records(self, app, client):
        """Importing a second historical donation for the same person (same
        phone) must not create a second Donor row."""
        from models import Donor
        login(client)
        self._post(client, phone="9811122233", full_name="Gopal Das")
        self._post(client, phone="9811122233", full_name="Gopal Das", receipt_number="OLD/2023/DUP2")
        assert Donor.query.filter_by(phone="9811122233").count() == 1

    def test_staff_cannot_use_single_entry_import(self, app, client):
        from models import Donation
        login(client, username="teststaff")
        self._post(client)
        assert Donation.query.count() == 0, "staff was able to use the admin-only single-entry import"


class TestImportTemplates:
    """The demo/template files must match what the importers require, or
    someone downloads a template that gets rejected."""

    def test_donor_template_has_the_required_columns(self, app, client):
        login(client)
        header = client.get("/admin/donors/import/demo.csv").data.decode().splitlines()[0]
        assert "full_name" in header

    def test_bulk_template_matches_required_columns(self, app, client):
        login(client)
        header = client.get("/admin/donations/bulk-import/demo.csv").data.decode().splitlines()[0]
        for col in ["full_name", "campaign_name", "amount", "payment_mode", "donation_date"]:
            assert col in header, f"template missing required column {col}"

    def test_legacy_template_matches_required_columns(self, app, client):
        login(client)
        header = client.get("/admin/donations/import-legacy/demo.csv").data.decode().splitlines()[0]
        for col in ["full_name", "campaign_name", "amount", "donation_date"]:
            assert col in header, f"template missing required column {col}"

    def test_each_template_imports_cleanly_into_its_own_importer(self, app, client):
        """The strongest check available: download the template and feed it
        straight back in. If that fails, the template is wrong."""
        from models import Donation, Donor
        login(client)

        donors_before = Donor.query.count()
        tpl = client.get("/admin/donors/import/demo.csv").data.decode()
        resp = _post(client, "/admin/donors/import", tpl)
        assert b"Couldn't read" not in resp.data
        assert Donor.query.count() > donors_before, "donor template imported nothing"

        tpl = client.get("/admin/donations/bulk-import/demo.csv").data.decode()
        resp = _post(client, "/admin/donations/bulk-import", tpl)
        assert Donation.query.count() > 0, "bulk template imported nothing"

        before = Donation.query.count()
        tpl = client.get("/admin/donations/import-legacy/demo.csv").data.decode()
        resp = _post(client, "/admin/donations/import-legacy", tpl)
        assert Donation.query.count() > before, "legacy template imported nothing"
