"""Dates in uploaded CSVs, after Excel has had them.

Nobody types these files by hand. They're exported, opened in Excel or
Google Sheets for a quick review, and re-saved -- and on the way through,
the date cells get rewritten into the machine's locale format. 2024-01-22
comes back as 22/01/2024 or 22/01/24. The operator sees a file that looks
fine and an import that rejects every row, and the obvious next move --
retyping several thousand dates -- is worse than the problem.

The donor, camp and legacy importers already handled this. The bulk
donation importer did not: it accepted YYYY-MM-DD and nothing else, so the
same reviewed file imported through three tabs and failed on the fourth.
The legacy importer had its own copy-pasted version of the fallback. All
four now share _parse_import_date, and these tests hold them level -- a
format one accepts and another rejects is the bug this file exists for.
"""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import login

# What actually comes back out of a spreadsheet, and the date each means.
EXCEL_FORMATS = [
    ("2026-08-01", "the canonical format the templates use"),
    ("01/08/2026", "Excel, Indian/UK locale"),
    ("01-08-2026", "Excel, dashes instead of slashes"),
    ("1/8/2026", "Excel, leading zeros dropped"),
    ("01/08/26", "Excel, two-digit year"),
    ("2026/08/01", "Google Sheets, year first with slashes"),
]

REJECTED_FORMATS = [
    "Aug 1, 2026",   # a text cell, not a date -- too many readings to guess
    "01.08.2026.",   # trailing junk
    "not a date",
]


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


def _upload(client, url, csv_text):
    return client.post(
        url, data={"csv_file": (io.BytesIO(csv_text.encode()), "import.csv")},
        content_type="multipart/form-data", follow_redirects=True)


def _bulk(client, date_value, name="Ravi Sharma"):
    return _upload(client, "/admin/donations/bulk-import", (
        "full_name,campaign_name,amount,payment_mode,donation_date\n"
        f"{name},Annadan,1100,cash,{date_value}\n"))


def _legacy(client, date_value, name="Old Donor"):
    return _upload(client, "/admin/donations/import-legacy", (
        "full_name,campaign_name,amount,payment_mode,donation_date\n"
        f"{name},Annadan,1100,cash,{date_value}\n"))


def _camp(client, date_value, name="Anita Verma"):
    return _upload(client, "/admin/iyf-camps/bulk", (
        "full_name,amount,camp_name,batch_name,donation_date,payment_mode\n"
        f"{name},2100,Utkarsha 2026,Batch A,{date_value},cash\n"))


class TestTheBulkDonationImportTakesWhatExcelProduces:

    @pytest.mark.parametrize("date_value,description", EXCEL_FORMATS)
    def test_format_is_accepted_and_read_correctly(
            self, app, client, campaign_id, date_value, description):
        """Accepting the row isn't enough -- a date read as the wrong day
        decides the wrong financial year, which flows into Form 10BD."""
        import datetime
        from models import Donation
        login(client)
        _bulk(client, date_value)
        with app.app_context():
            donation = Donation.query.one()
            assert donation.donation_date.date() == datetime.date(2026, 8, 1), (
                f"{description}: '{date_value}' read as {donation.donation_date}"
            )

    @pytest.mark.parametrize("date_value", REJECTED_FORMATS)
    def test_unreadable_dates_are_refused_not_guessed(
            self, app, client, campaign_id, date_value):
        from models import Donation
        login(client)
        resp = _bulk(client, date_value)
        with app.app_context():
            assert Donation.query.count() == 0
        assert "donation_date" in resp.data.decode()

    def test_a_blank_date_is_refused_with_a_useful_message(self, app, client, campaign_id):
        """donation_date is a required column. Blank used to come through
        _parse_import_date as "nothing to do" -- for this importer that
        would mean a donation with no date at all."""
        from models import Donation
        login(client)
        resp = _bulk(client, "")
        with app.app_context():
            assert Donation.query.count() == 0
        assert "donation_date is required" in resp.data.decode()


class TestAllFourImportersAgree:
    """The bug was one importer disagreeing with the others, so parity is
    the thing worth testing, not any single importer."""

    @pytest.mark.parametrize("date_value,description", EXCEL_FORMATS)
    def test_the_legacy_importer_takes_the_same_formats(
            self, app, client, campaign_id, date_value, description):
        import datetime
        from models import Donation
        login(client)
        _legacy(client, date_value)
        with app.app_context():
            donation = Donation.query.one()
            assert donation.donation_date.date() == datetime.date(2026, 8, 1), description

    @pytest.mark.parametrize("date_value,description", EXCEL_FORMATS)
    def test_the_camp_importer_takes_the_same_formats(
            self, app, client, date_value, description):
        import datetime
        from models import Donation
        login(client)
        client.post("/admin/iyf-camps/manage", data={"name": "Utkarsha 2026"},
                    follow_redirects=True)
        _camp(client, date_value)
        with app.app_context():
            donation = Donation.query.one()
            assert donation.donation_date.date() == datetime.date(2026, 8, 1), description

    def test_no_importer_parses_dates_on_its_own(self):
        """The legacy importer used to carry its own copy of the fallback,
        which is how the two drifted. Nothing in an import route should be
        calling strptime directly any more."""
        import inspect
        import admin
        for name in ("bulk_import_donations", "import_legacy_donations",
                     "iyf_camp_bulk", "import_donors"):
            fn = getattr(admin, name, None)
            assert fn is not None, (
                f"admin.{name} no longer exists -- rename it here too, or this "
                "check quietly stops covering it"
            )
            source = inspect.getsource(fn)
            assert "strptime" not in source, (
                f"{name}() parses dates itself instead of using "
                "_parse_import_date -- that's how the formats drifted apart"
            )


class TestAmbiguousDatesAreFlagged:
    """01/08/2026 is 1 August day-first and 8 January month-first. Day-first
    is right for a file saved in an Indian locale and wrong for a US one,
    and there's no way to tell from the file. Guessing silently would put
    donations in the wrong financial year with nothing to notice."""

    def test_the_operator_is_told_when_a_guess_was_made(self, app, client, campaign_id):
        login(client)
        resp = _bulk(client, "01/08/2026")
        body = resp.data.decode()
        assert "read day-first" in body
        assert "01/08/2026" in body

    def test_an_unambiguous_day_first_date_is_not_flagged(self, app, client, campaign_id):
        """22/01/2024 can only be 22 January -- there's no 22nd month, so
        nothing was guessed and there's nothing to warn about. Warning on
        every D/M/Y date would train people to ignore the message."""
        login(client)
        resp = _bulk(client, "22/01/2024")
        assert "read day-first" not in resp.data.decode()

    def test_the_canonical_format_is_never_flagged(self, app, client, campaign_id):
        login(client)
        resp = _bulk(client, "2026-08-01")
        assert "read day-first" not in resp.data.decode()

    def test_the_legacy_importer_warns_too(self, app, client, campaign_id):
        """It's the importer being pointed at years of history, so it's the
        one where a silent month/day swap would do the most damage."""
        login(client)
        resp = _legacy(client, "01/08/2026")
        assert "read day-first" in resp.data.decode()
