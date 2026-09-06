"""Tests for Admin -> Settings -> Zoho Forms.

One row per Zoho form that takes money: which campaign its donations
belong to, and which Google Sheet holds the donor's name. This is what
lets reconciliation issue receipts without a person.

It replaced two environment settings that assumed one sheet for
everything. There are six forms already and a new one appears whenever the
temple runs a programme, so adding a seminar shouldn't need a redeploy --
and whoever later wonders why a form's donations aren't being receipted
should be able to see the answer rather than read a semicolon-delimited
string in a dashboard.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import login

URL = "/admin/settings/zoho-forms"


@pytest.fixture(autouse=True)
def _logged_in(client):
    login(client)


def _campaign_id(name="BACE Contribution"):
    from models import Campaign
    return Campaign.query.filter_by(name=name).first().id


def _add(client, **overrides):
    data = {
        "form_key": "EssenceofBhagavadGitaOnlyForBOYSP",
        "display_name": "Essence of Bhagavad Gita (Only For BOYS)",
        "campaign_id": str(_campaign_id()),
        "sheet_csv_url": "https://docs.google.com/spreadsheets/d/x/pub?output=csv",
    }
    data.update(overrides)
    return client.post(URL, data=data, follow_redirects=True)


class TestAdding:
    def test_a_form_can_be_set_up(self, client, app):
        from models import ZohoForm

        _add(client)

        entry = ZohoForm.query.one()
        assert entry.form_key == "EssenceofBhagavadGitaOnlyForBOYSP"
        assert entry.campaign.name == "BACE Contribution"
        assert entry.sheet_csv_url.endswith("output=csv")
        assert entry.is_active and not entry.is_test

    def test_a_fully_configured_form_reports_that_it_can_receipt(self, client, app):
        """The badge on the page, and the condition reconciliation checks."""
        from models import ZohoForm

        _add(client)
        assert ZohoForm.query.one().can_receipt is True

    def test_a_form_without_a_sheet_cannot_receipt_yet(self, client, app):
        """Valid and useful -- it means "report this form's payments, don't
        receipt them" while its sheet is being set up."""
        from models import ZohoForm

        _add(client, sheet_csv_url="")
        entry = ZohoForm.query.one()
        assert entry.sheet_csv_url is None
        assert entry.can_receipt is False

    def test_a_form_without_a_campaign_cannot_receipt(self, client, app):
        from models import ZohoForm

        _add(client, campaign_id="")
        assert ZohoForm.query.one().can_receipt is False

    def test_a_test_form_never_receipts_however_complete(self, client, app):
        from models import ZohoForm

        _add(client, form_key="TestingWebsitewithFormsintergration", is_test="yes")
        entry = ZohoForm.query.one()
        assert entry.is_test and entry.can_receipt is False

    def test_a_blank_form_key_is_refused(self, client, app):
        from models import ZohoForm

        _add(client, form_key="   ")
        assert ZohoForm.query.count() == 0

    def test_the_same_form_cannot_be_added_twice(self, client, app):
        from models import ZohoForm

        _add(client)
        resp = _add(client)
        assert ZohoForm.query.count() == 1
        assert b"already set up" in resp.data

    def test_an_unknown_campaign_is_refused(self, client, app):
        from models import ZohoForm

        _add(client, campaign_id="99999")
        assert ZohoForm.query.count() == 0


class TestEditing:
    def test_a_sheet_url_can_be_added_later(self, client, app):
        from models import ZohoForm

        _add(client, sheet_csv_url="")
        entry = ZohoForm.query.one()
        assert entry.can_receipt is False

        client.post(f"{URL}/{entry.id}/update", data={
            "display_name": entry.display_name,
            "campaign_id": str(_campaign_id()),
            "sheet_csv_url": "https://docs.google.com/spreadsheets/d/y/pub?output=csv",
            "is_active": "yes",
        }, follow_redirects=True)

        assert ZohoForm.query.one().can_receipt is True

    def test_a_form_can_be_marked_as_a_test_form(self, client, app):
        from models import ZohoForm

        _add(client)
        entry = ZohoForm.query.one()
        client.post(f"{URL}/{entry.id}/update", data={
            "campaign_id": str(_campaign_id()),
            "sheet_csv_url": entry.sheet_csv_url,
            "is_test": "yes", "is_active": "yes",
        }, follow_redirects=True)

        assert ZohoForm.query.one().is_test is True

    def test_deactivating_stops_it_being_used(self, client, app):
        from models import ZohoForm

        _add(client)
        entry = ZohoForm.query.one()
        client.post(f"{URL}/{entry.id}/update", data={
            "campaign_id": str(_campaign_id()),
            "sheet_csv_url": entry.sheet_csv_url,
        }, follow_redirects=True)

        assert ZohoForm.query.one().is_active is False


class TestRemoving:
    def test_a_form_can_be_removed(self, client, app):
        from models import ZohoForm

        _add(client)
        entry = ZohoForm.query.one()
        client.post(f"{URL}/{entry.id}/delete", follow_redirects=True)
        assert ZohoForm.query.count() == 0

    def test_removing_a_form_leaves_its_donations_alone(self, client, app):
        """Removing the row stops future payments being receipted
        automatically. It must not touch anything already receipted."""
        from extensions import db
        from models import Campaign, Donation, Donor, ZohoForm

        _add(client)
        donor = Donor(full_name="Past Donor", phone="9811100011")
        db.session.add(donor)
        db.session.flush()
        db.session.add(Donation(
            donor_id=donor.id, campaign_id=Campaign.query.first().id, amount=100,
            status="success", payment_mode="online", receipt_number="032511/ISK500900",
        ))
        db.session.commit()

        client.post(f"{URL}/{ZohoForm.query.one().id}/delete", follow_redirects=True)

        assert Donation.query.count() == 1
        assert Donation.query.one().receipt_number == "032511/ISK500900"


class TestAccess:
    def test_staff_cannot_reach_it(self, client, app):
        """Setting these decides which campaign donations get filed under
        and issues receipts off the back of it -- an admin action."""
        client.get("/admin/logout", follow_redirects=True)
        login(client, username="teststaff")
        resp = client.get(URL, follow_redirects=True)
        assert b"Add a form" not in resp.data

    def test_it_is_logged_in_the_activity_log(self, client, app):
        from models import AdminActivityLog

        _add(client)
        entry = AdminActivityLog.query.filter_by(action="zoho_form_add").one()
        assert "EssenceofBhagavadGitaOnlyForBOYSP" in entry.details
