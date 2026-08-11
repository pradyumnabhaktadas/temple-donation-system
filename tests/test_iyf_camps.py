"""Integration tests for the IYF Camps tab.

These drive the real routes through Flask's test client against a real
(in-memory) database, rather than checking the code reads correctly. Two
bugs in this feature reached the user before these existed -- a <form>
nested in a <tr> that meant Save posted nothing, and a delete confirmation
that vanished on a name containing an apostrophe -- and both would have
been caught here in seconds.
"""
import io
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import login


def _mk_camp(client, name="Utkarsha 2026"):
    return client.post("/admin/iyf-camps/manage", data={"name": name}, follow_redirects=True)


class TestCampList:
    def test_page_loads_with_no_camps(self, app, client):
        login(client)
        resp = client.get("/admin/iyf-camps/manage")
        assert resp.status_code == 200
        assert b"No camps yet" in resp.data

    def test_create_camp(self, app, client):
        from models import Camp
        login(client)
        _mk_camp(client)
        assert Camp.query.filter_by(name="Utkarsha 2026").count() == 1

    def test_duplicate_name_rejected_case_insensitively(self, app, client):
        """The list existing to prevent one camp having two spellings is
        worth nothing if the list itself accepts them."""
        from models import Camp
        login(client)
        _mk_camp(client, "Utkarsha 2026")
        _mk_camp(client, "utkarsha 2026")
        assert Camp.query.count() == 1

    def test_whitespace_is_normalised_on_create(self, app, client):
        from models import Camp
        login(client)
        _mk_camp(client, "  Utkarsha   2026 ")
        assert Camp.query.one().name == "Utkarsha 2026"

    def test_edit_form_actually_submits(self, app, client):
        """Regression: the row form used to be a <form> inside a <tr>,
        which browsers hoist out of the table -- leaving the name input
        outside any form, so Save posted nothing. Asserting the rendered
        markup wires the input to the form by id."""
        login(client)
        _mk_camp(client)
        from models import Camp
        camp = Camp.query.one()
        html = client.get("/admin/iyf-camps/manage").data.decode()
        assert f'form="camp-edit-{camp.id}"' in html
        assert f'id="camp-edit-{camp.id}"' in html
        # And the form must not sit inside the table markup.
        assert "<tr>\n          <form" not in html

    def test_rename_moves_existing_donations(self, app, client):
        """The whole reason renaming goes through a form: a corrected
        spelling must not split one camp's history into two totals."""
        from extensions import db
        from models import Camp, Donation
        login(client)
        _mk_camp(client, "Utkarsah 2026")          # misspelled
        camp = Camp.query.one()
        client.post("/admin/iyf-camps/single", data={
            "camp_name": "Utkarsah 2026", "full_name": "Ravi", "amount": "500",
        }, follow_redirects=True)
        assert Donation.query.filter_by(camp_name="Utkarsah 2026").count() == 1

        client.post(f"/admin/iyf-camps/manage/{camp.id}/edit",
                    data={"name": "Utkarsha 2026", "is_active": "yes"}, follow_redirects=True)
        db.session.expire_all()
        assert Donation.query.filter_by(camp_name="Utkarsah 2026").count() == 0
        assert Donation.query.filter_by(camp_name="Utkarsha 2026").count() == 1

    def test_delete_keeps_donations_and_totals(self, app, client):
        """Deleting a camp must only remove it from the dropdown."""
        from models import Camp, Donation
        login(client)
        _mk_camp(client)
        camp = Camp.query.one()
        client.post("/admin/iyf-camps/single", data={
            "camp_name": "Utkarsha 2026", "full_name": "Ravi", "amount": "700",
        }, follow_redirects=True)

        client.post(f"/admin/iyf-camps/manage/{camp.id}/delete", follow_redirects=True)
        assert Camp.query.count() == 0
        d = Donation.query.filter_by(camp_name="Utkarsha 2026").one()
        assert float(d.amount) == 700.0
        # Still reported on the collections page.
        assert b"Utkarsha 2026" in client.get("/admin/iyf-camps").data

    def test_retired_camp_hidden_from_dropdown_but_kept(self, app, client):
        from models import Camp
        login(client)
        _mk_camp(client)
        camp = Camp.query.one()
        client.post(f"/admin/iyf-camps/manage/{camp.id}/edit",
                    data={"name": "Utkarsha 2026"}, follow_redirects=True)  # no is_active -> retired
        assert Camp.query.one().is_active is False
        html = client.get("/admin/iyf-camps").data.decode()
        assert '<option value="Utkarsha 2026">' not in html

    def test_apostrophe_name_survives_render(self, app, client):
        """Regression: an apostrophe used to break the delete
        confirmation's JS string, silently removing the prompt."""
        login(client)
        _mk_camp(client, "Dev's Camp")
        html = client.get("/admin/iyf-camps/manage").data.decode()
        assert "data-camp-name=\"Dev&#39;s Camp\"" in html
        assert "onsubmit=" not in html


class TestSingleEntry:
    def test_records_donation_with_camp_and_batch(self, app, client):
        from models import Donation
        login(client)
        _mk_camp(client)
        resp = client.post("/admin/iyf-camps/single", data={
            "camp_name": "Utkarsha 2026", "batch_name": "Batch A",
            "full_name": "Ravi Sharma", "amount": "1100",
            "phone": "9876543210", "payment_mode": "cash",
        }, follow_redirects=True)
        assert resp.status_code == 200
        d = Donation.query.one()
        assert d.camp_name == "Utkarsha 2026"
        assert d.batch_name == "Batch A"
        assert d.receipt_number             # a real receipt was issued
        assert d.status == "success"
        assert d.campaign.name == "IYF Camps"
        assert d.campaign.is_80g is False   # camps are never 80G

    def test_unknown_camp_rejected(self, app, client):
        from models import Donation
        login(client)
        _mk_camp(client)
        client.post("/admin/iyf-camps/single", data={
            "camp_name": "Not A Camp", "full_name": "Ravi", "amount": "100",
        }, follow_redirects=True)
        assert Donation.query.count() == 0

    def test_bad_phone_rejected(self, app, client):
        from models import Donation
        login(client)
        _mk_camp(client)
        client.post("/admin/iyf-camps/single", data={
            "camp_name": "Utkarsha 2026", "full_name": "Ravi",
            "amount": "100", "phone": "123",
        }, follow_redirects=True)
        assert Donation.query.count() == 0

    def test_phone_is_optional(self, app, client):
        """A camp register often has only a name against a cash payment."""
        from models import Donation
        login(client)
        _mk_camp(client)
        client.post("/admin/iyf-camps/single", data={
            "camp_name": "Utkarsha 2026", "full_name": "Ravi", "amount": "100",
        }, follow_redirects=True)
        assert Donation.query.count() == 1

    def test_no_notifications_are_sent(self, app, client):
        """The promise on this tab is that students are never messaged."""
        from unittest.mock import patch
        login(client)
        _mk_camp(client)
        with patch("admin._send_receipt_notifications_background") as send:
            client.post("/admin/iyf-camps/single", data={
                "camp_name": "Utkarsha 2026", "full_name": "Ravi",
                "amount": "100", "email": "ravi@example.com",
            }, follow_redirects=True)
        send.assert_not_called()


class TestBulkImport:
    def _upload(self, client, csv_text):
        return client.post(
            "/admin/iyf-camps/bulk",
            data={"csv_file": (io.BytesIO(csv_text.encode()), "camps.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )

    def test_imports_rows(self, app, client):
        from models import Donation
        login(client)
        _mk_camp(client)
        resp = self._upload(client,
            "full_name,amount,camp_name,batch_name,donation_date\n"
            "Ravi Sharma,1100,Utkarsha 2026,Batch A,2026-08-01\n"
            "Anita Verma,2100,Utkarsha 2026,Batch B,2026-08-02\n")
        assert resp.status_code == 200
        assert Donation.query.count() == 2
        assert {d.batch_name for d in Donation.query.all()} == {"Batch A", "Batch B"}
        assert all(d.receipt_number for d in Donation.query.all())

    def test_camp_matched_case_insensitively_and_stored_canonically(self, app, client):
        """A Zoho export writing a different case must not start a second
        camp -- it files under the camp's own spelling."""
        from models import Donation
        login(client)
        _mk_camp(client, "Utkarsha 2026")
        self._upload(client, "full_name,amount,camp_name\nRavi,500,utkarsha  2026\n")
        assert Donation.query.one().camp_name == "Utkarsha 2026"

    def test_unknown_camp_row_is_skipped_and_named(self, app, client):
        from models import Donation
        login(client)
        _mk_camp(client)
        resp = self._upload(client, "full_name,amount,camp_name\nRavi,500,Mystery Camp\n")
        assert Donation.query.count() == 0
        assert b"Mystery Camp" in resp.data

    def test_row_with_unusable_phone_still_imports(self, app, client):
        """Losing a donation over a phone mangled by a spreadsheet
        round-trip would be the wrong trade -- the sample Zoho export had
        exactly that."""
        from models import Donation
        login(client)
        _mk_camp(client)
        resp = self._upload(client,
            "full_name,amount,camp_name,phone\nRavi,500,Utkarsha 2026,9.87654E+09\n")
        d = Donation.query.one()
        assert float(d.amount) == 500.0
        assert not d.donor.phone
        assert b"imported without it" in resp.data

    def test_retired_camp_still_matches(self, app, client):
        """Historical data must be uploadable after a camp finishes."""
        from models import Camp, Donation
        login(client)
        _mk_camp(client)
        camp = Camp.query.one()
        client.post(f"/admin/iyf-camps/manage/{camp.id}/edit",
                    data={"name": "Utkarsha 2026"}, follow_redirects=True)
        self._upload(client, "full_name,amount,camp_name\nRavi,500,Utkarsha 2026\n")
        assert Donation.query.count() == 1

    def test_missing_required_column_reported(self, app, client):
        login(client)
        resp = self._upload(client, "full_name,amount\nRavi,500\n")
        assert b"camp_name" in resp.data

    def test_bulk_sends_no_notifications(self, app, client):
        from unittest.mock import patch
        login(client)
        _mk_camp(client)
        with patch("admin._send_receipt_notifications_background") as send:
            self._upload(client,
                "full_name,amount,camp_name,email\nRavi,500,Utkarsha 2026,r@e.com\n")
        send.assert_not_called()

    def test_template_csv_downloads(self, app, client):
        login(client)
        resp = client.get("/admin/iyf-camps/template.csv")
        assert resp.status_code == 200
        assert b"camp_name" in resp.data and b"batch_name" in resp.data


class TestCampTotals:
    def test_totals_group_by_camp(self, app, client):
        login(client)
        _mk_camp(client, "Camp A")
        _mk_camp(client, "Camp B")
        for camp, amt in [("Camp A", 100), ("Camp A", 250), ("Camp B", 400)]:
            client.post("/admin/iyf-camps/single", data={
                "camp_name": camp, "full_name": "S", "amount": str(amt),
            }, follow_redirects=True)
        html = client.get("/admin/iyf-camps").data.decode()
        assert "350" in html and "400" in html      # per-camp
        assert "750" in html                        # all camps

    def test_camp_and_batch_in_donations_export(self, app, client):
        login(client)
        _mk_camp(client)
        client.post("/admin/iyf-camps/single", data={
            "camp_name": "Utkarsha 2026", "batch_name": "Batch A",
            "full_name": "Ravi", "amount": "500",
        }, follow_redirects=True)
        csv_out = client.get("/admin/export/donations?range=all").data.decode()
        assert "Camp" in csv_out.splitlines()[0]
        assert "Utkarsha 2026" in csv_out and "Batch A" in csv_out
