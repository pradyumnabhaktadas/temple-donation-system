"""Tests for Admin -> Settings -> Zoho Diagnostics.

This page exists because the shell didn't work. The same checks were written
as CLI scripts first, and getting an answer out of them took three attempts:
one run in the cron job's shell reported confidently on an empty SQLite file
it had just created, and two more were lost to the Render web shell mangling
a pasted multi-line command and then dropping the connection.

None of that was a real constraint. Every fact here is available to the web
app, and whoever needs it is already signed in.

The thing most worth testing: this page must never hang or 500. Zoho hanging
is precisely the condition it is there to report, and a diagnostic that dies
on the fault it is diagnosing is worthless.
"""
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import login

URL = "/admin/settings/zoho-diagnostics"


@pytest.fixture(autouse=True)
def _logged_in(client):
    login(client)


def _configured(app):
    app.config.update(ZOHO_CLIENT_ID="cid", ZOHO_CLIENT_SECRET="csec",
                      ZOHO_REFRESH_TOKEN="rtok")


def _form(app, form_key="EBG", campaign_name="BACE Contribution", is_test=False):
    from extensions import db
    from models import Campaign, ZohoForm
    campaign = Campaign.query.filter_by(name=campaign_name).first()
    entry = ZohoForm(form_key=form_key, campaign_id=campaign.id if campaign else None,
                     is_test=is_test)
    db.session.add(entry)
    db.session.commit()
    return entry


def _entry(payment_id="pay_TYkwQc4pqV9HNu", name="Jatin, saini", added="04-Sep-2026 21:55:22"):
    return {
        "Name": name, "Phone": "9650150283", "Payment Amount": "100",
        "Payment Status": "Completed", "Added Time": added,
        "Payment Transaction ID": f"Txn ID : {payment_id} Order ID : order_x",
    }


def _api(records):
    return patch("zoho_api.entries",
                 side_effect=lambda c, f, start_index=1, limit=200, timeout=None:
                 ((list(records) if start_index == 1 else []), "records"))


class TestItNeverDiesOnTheFaultItIsDiagnosing:
    def test_an_unreachable_zoho_still_renders(self, client, app):
        import zoho_api
        _configured(app)
        _form(app)
        with patch("zoho_api.entries", side_effect=zoho_api.ZohoApiError("connection timed out")):
            resp = client.get(URL)
        assert resp.status_code == 200
        assert b"connection timed out" in resp.data
        assert b"Unreachable" in resp.data

    def test_a_zoho_error_is_reported_as_zohos_fault_not_ours(self, client, app):
        """A generic handler below catches everything, so the page renders
        either way -- but "Unexpected:" means our code broke, and reading
        that when Zoho simply refused sends the next person looking in the
        wrong place."""
        import zoho_api
        _configured(app)
        _form(app)
        with patch("zoho_api.entries", side_effect=zoho_api.ZohoApiError("refresh token revoked")):
            resp = client.get(URL)
        assert b"refresh token revoked" in resp.data
        assert b"Unexpected" not in resp.data

    def test_an_unexpected_exception_still_renders(self, client, app):
        """The shape of Zoho's response differs by API version. A page that
        500s on a surprise tells us nothing about the surprise."""
        _configured(app)
        _form(app)
        with patch("zoho_api.entries", side_effect=TypeError("unexpected shape")):
            resp = client.get(URL)
        assert resp.status_code == 200
        assert b"Unexpected" in resp.data

    def test_zoho_calls_are_given_a_bounded_timeout(self, client, app):
        """zoho_api defaults to 60s per read plus 30s for a token refresh,
        past gunicorn's 30s worker timeout. Without a bound this page would
        502 instead of reporting that Zoho is slow -- which is the exact
        failure that sent us here."""
        _configured(app)
        _form(app)
        with _api([_entry()]) as api:
            client.get(URL)
        timeout = api.call_args.kwargs.get("timeout")
        assert timeout is not None and 0 < timeout < 30

    def test_one_broken_form_does_not_hide_the_others(self, client, app):
        import zoho_api
        _configured(app)
        _form(app, form_key="GOOD")
        _form(app, form_key="BROKEN")

        def _flaky(config, form, start_index=1, limit=200, timeout=None):
            if form == "BROKEN":
                raise zoho_api.ZohoApiError("nope")
            return ([_entry()], "records")

        with patch("zoho_api.entries", side_effect=_flaky):
            resp = client.get(URL)

        assert resp.status_code == 200
        assert b"GOOD" in resp.data and b"BROKEN" in resp.data
        assert b"pay_TYkwQc4pqV9HNu" in resp.data

    def test_it_renders_with_nothing_configured_at_all(self, client, app):
        assert client.get(URL).status_code == 200


class TestWhatItReports:
    def test_unconfigured_credentials_are_named_as_the_blocker(self, client, app):
        resp = client.get(URL)
        assert b"ZOHO_CLIENT_ID" in resp.data
        assert b"Not configured" in resp.data

    def test_it_shows_what_the_parser_extracts_from_real_entries(self, client, app):
        """The single most valuable line on the page: that parser has only
        ever run against payloads written by hand."""
        _configured(app)
        _form(app)
        with _api([_entry()]):
            resp = client.get(URL)
        assert b"pay_TYkwQc4pqV9HNu" in resp.data
        assert b"Jatin Saini" in resp.data          # "Jatin, saini" -> First Last

    def test_it_reports_the_record_ordering(self, client, app):
        _configured(app)
        _form(app)
        records = [_entry(f"pay_{d}", added=f"{d:02d}-Sep-2026 10:00:00")
                   for d in (10, 9, 8)]
        with _api(records):
            resp = client.get(URL)
        assert b"newest-first" in resp.data

    def test_entries_carrying_no_payment_id_are_called_out(self, client, app):
        _configured(app)
        _form(app)
        plain = {"Name": "Someone", "Added Time": "04-Sep-2026 21:55:22"}
        with _api([plain, plain]):
            resp = client.get(URL)
        assert b"none found" in resp.data

    def test_the_database_is_named_without_its_password(self, client, app):
        app.config["SQLALCHEMY_DATABASE_URI"] = "postgresql://u:sup3rs3cret@host/db"
        resp = client.get(URL)
        assert b"sup3rs3cret" not in resp.data
        assert b"host/db" in resp.data

    def test_a_form_with_no_campaign_is_flagged(self, client, app):
        _configured(app)
        _form(app, campaign_name="__none__")
        with _api([_entry()]):
            resp = client.get(URL)
        assert b"No campaign" in resp.data


class TestDuplicatePayments:
    def _two_on_one_payment(self, app):
        from extensions import db
        from models import Campaign, Donation, Donor
        donor = Donor(full_name="Donor", phone="9000000001")
        db.session.add(donor)
        db.session.flush()
        cid = Campaign.query.first().id
        db.session.add(Donation(donor_id=donor.id, campaign_id=cid, amount=100,
                                status="success", payment_mode="online",
                                razorpay_payment_id="pay_Dup1", receipt_number="R1"))
        db.session.add(Donation(donor_id=donor.id, campaign_id=cid, amount=100,
                                status="success", payment_mode="offline",
                                bank_transaction_id="pay_Dup1", receipt_number="R2"))
        db.session.commit()

    def test_a_clean_table_says_so(self, client, app):
        resp = client.get(URL)
        assert b"No payment id appears on more than one donation" in resp.data

    def test_a_cross_column_duplicate_is_found(self, client, app):
        """A donation entered by hand records the gateway id in
        bank_transaction_id. Checking one column at a time misses exactly
        the duplicate that has already happened here."""
        self._two_on_one_payment(app)
        resp = client.get(URL)
        assert b"pay_Dup1" in resp.data
        assert b"more than one donation" in resp.data
        assert b"R1" in resp.data and b"R2" in resp.data


class TestAccess:
    def test_staff_cannot_reach_it(self, client, app):
        """It names the database and lists donor-linked payments."""
        client.get("/admin/logout", follow_redirects=True)
        login(client, username="teststaff")
        resp = client.get(URL, follow_redirects=True)
        assert b"Zoho Diagnostics" not in resp.data

    def test_it_is_reachable_from_the_settings_menu(self, client, app):
        resp = client.get("/admin/settings/zoho-forms")
        assert b"zoho-diagnostics" in resp.data
