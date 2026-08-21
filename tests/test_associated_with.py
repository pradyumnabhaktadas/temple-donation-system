"""Integration tests for the "I am associated with" feature.

Requested by the user as a way to track which preaching program/devotee/
initiative a donation came through, kept deliberately separate from
Donation Purpose/Campaign (see AssociatedWith's docstring in models.py).
These drive the real routes through Flask's test client end to end --
admin CRUD, the public donation flow (create_order), admin offline entry
(single + bulk), the Donations Log filter/export, and the Associated With
Report -- rather than checking the code reads correctly.
"""
import io
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import login


def _mk_option(client, name="IYF Dwarka Temple Preaching"):
    return client.post("/admin/associated-with", data={"name": name}, follow_redirects=True)


class TestAdminCrud:
    def test_add_option(self, app, client):
        from models import AssociatedWith
        login(client)
        _mk_option(client)
        assert AssociatedWith.query.filter_by(name="IYF Dwarka Temple Preaching").count() == 1

    def test_duplicate_name_rejected(self, app, client):
        from models import AssociatedWith
        login(client)
        _mk_option(client, "Online Preaching")
        _mk_option(client, "Online Preaching")
        assert AssociatedWith.query.count() == 1

    def test_blank_name_rejected(self, app, client):
        from models import AssociatedWith
        login(client)
        _mk_option(client, "   ")
        assert AssociatedWith.query.count() == 0

    def test_new_option_defaults_active(self, app, client):
        from models import AssociatedWith
        login(client)
        _mk_option(client)
        item = AssociatedWith.query.one()
        assert item.is_active is True

    def test_new_options_land_after_existing_ones(self, app, client):
        """New entries append to the end of the arranged order rather than
        jumping to the front at display_order 0 -- otherwise adding a new
        option would silently reorder everything the office already
        arranged."""
        from models import AssociatedWith
        login(client)
        _mk_option(client, "First")
        _mk_option(client, "Second")
        items = AssociatedWith.query.order_by(AssociatedWith.display_order).all()
        assert [i.name for i in items] == ["First", "Second"]
        assert items[0].display_order < items[1].display_order

    def test_rename(self, app, client):
        from models import AssociatedWith
        login(client)
        _mk_option(client, "Old Name")
        item = AssociatedWith.query.one()
        client.post(f"/admin/associated-with/{item.id}/edit", data={"name": "New Name"}, follow_redirects=True)
        assert AssociatedWith.query.one().name == "New Name"

    def test_rename_to_existing_name_rejected(self, app, client):
        from models import AssociatedWith
        login(client)
        _mk_option(client, "Alpha")
        _mk_option(client, "Beta")
        beta = AssociatedWith.query.filter_by(name="Beta").one()
        client.post(f"/admin/associated-with/{beta.id}/edit", data={"name": "Alpha"}, follow_redirects=True)
        assert AssociatedWith.query.filter_by(name="Beta").count() == 1

    def test_toggle_active(self, app, client):
        from models import AssociatedWith
        login(client)
        _mk_option(client)
        item = AssociatedWith.query.one()
        assert item.is_active is True
        client.post(f"/admin/associated-with/{item.id}/toggle", follow_redirects=True)
        assert AssociatedWith.query.get(item.id).is_active is False
        client.post(f"/admin/associated-with/{item.id}/toggle", follow_redirects=True)
        assert AssociatedWith.query.get(item.id).is_active is True

    def test_move_up_and_down_swap_display_order(self, app, client):
        from models import AssociatedWith
        login(client)
        _mk_option(client, "First")
        _mk_option(client, "Second")
        _mk_option(client, "Third")
        first, second, third = AssociatedWith.query.order_by(AssociatedWith.display_order).all()

        client.post(f"/admin/associated-with/{second.id}/move", data={"direction": "up"}, follow_redirects=True)
        items = AssociatedWith.query.order_by(AssociatedWith.display_order).all()
        assert [i.name for i in items] == ["Second", "First", "Third"]

        client.post(f"/admin/associated-with/{items[0].id}/move", data={"direction": "down"}, follow_redirects=True)
        items = AssociatedWith.query.order_by(AssociatedWith.display_order).all()
        assert [i.name for i in items] == ["First", "Second", "Third"]

    def test_move_up_at_top_is_a_no_op(self, app, client):
        from models import AssociatedWith
        login(client)
        _mk_option(client, "First")
        _mk_option(client, "Second")
        first = AssociatedWith.query.order_by(AssociatedWith.display_order).first()
        client.post(f"/admin/associated-with/{first.id}/move", data={"direction": "up"}, follow_redirects=True)
        items = AssociatedWith.query.order_by(AssociatedWith.display_order).all()
        assert [i.name for i in items] == ["First", "Second"]

    def test_delete_blocked_when_donations_reference_it(self, app, client):
        from extensions import db
        from models import AssociatedWith, Campaign, Donor, Donation
        login(client)
        _mk_option(client, "In Use")
        item = AssociatedWith.query.one()
        campaign = Campaign.query.filter_by(name="Annadan").first()
        donor = Donor(full_name="Ref Donor", phone="9876500011")
        db.session.add(donor)
        db.session.flush()
        db.session.add(Donation(
            donor_id=donor.id, campaign_id=campaign.id, amount=500, payment_mode="cash",
            status="success", associated_with_id=item.id))
        db.session.commit()

        resp = client.post(f"/admin/associated-with/{item.id}/delete", follow_redirects=True)
        assert AssociatedWith.query.count() == 1
        assert b"deactivate" in resp.data.lower() or b"Can&#39;t delete" in resp.data or b"Can't delete" in resp.data

    def test_delete_succeeds_when_unused(self, app, client):
        from models import AssociatedWith
        login(client)
        _mk_option(client, "Unused")
        item = AssociatedWith.query.one()
        client.post(f"/admin/associated-with/{item.id}/delete", follow_redirects=True)
        assert AssociatedWith.query.count() == 0

    def test_staff_cannot_add_option(self, app, client):
        """Only admin-role users manage this list -- staff can still use it
        on the donation forms, but not edit the list itself."""
        from models import AssociatedWith
        login(client, username="teststaff")
        _mk_option(client, "Should Not Exist")
        assert AssociatedWith.query.count() == 0


class TestPublicDonationFlow:
    def test_create_order_stores_associated_with(self, app, client):
        from models import AssociatedWith, Campaign, Donation
        login(client)
        _mk_option(client, "IYF Dwarka Temple Preaching")
        option = AssociatedWith.query.one()
        campaign = Campaign.query.filter_by(name="Annadan").first()

        resp = client.post("/api/create-order", json={
            "amount": 501, "full_name": "Public Donor", "phone": "9876500022",
            "consent": "on", "campaign_id": campaign.id, "associated_with_id": option.id,
            "pan": "ABCDE1234F",
        })
        assert resp.status_code == 200
        donation = Donation.query.one()
        assert donation.associated_with_id == option.id

    def test_create_order_without_associated_with_is_fine(self, app, client):
        """Entirely optional -- omitting it must not block a donation."""
        from models import Campaign, Donation
        login(client)
        campaign = Campaign.query.filter_by(name="Annadan").first()
        resp = client.post("/api/create-order", json={
            "amount": 501, "full_name": "No Assoc Donor", "phone": "9876500033",
            "consent": "on", "campaign_id": campaign.id, "pan": "ABCDE1234F",
        })
        assert resp.status_code == 200
        assert Donation.query.one().associated_with_id is None

    def test_create_order_rejects_unknown_associated_with_id(self, app, client):
        from models import Campaign, Donation
        login(client)
        campaign = Campaign.query.filter_by(name="Annadan").first()
        resp = client.post("/api/create-order", json={
            "amount": 501, "full_name": "Bad Assoc Donor", "phone": "9876500044",
            "consent": "on", "campaign_id": campaign.id, "associated_with_id": 999999,
        })
        assert resp.status_code == 400
        assert Donation.query.count() == 0

    def test_associated_with_independent_of_donation_purpose(self, app, client):
        """The user's own example: Associated With and Donation Purpose
        must be recordable independently and never conflated -- one
        donation can carry both at once."""
        from extensions import db
        from models import AssociatedWith, Campaign, Donation, LiveToGivePurpose
        login(client)
        _mk_option(client, "IYF Dwarka Temple Preaching")
        option = AssociatedWith.query.one()
        campaign = Campaign.query.filter_by(name="Annadan").first()
        purpose = LiveToGivePurpose(name="Temple Construction", is_80g=True)
        db.session.add(purpose)
        db.session.commit()

        resp = client.post("/api/create-order", json={
            "amount": 501, "full_name": "Both Fields Donor", "phone": "9876500055",
            "consent": "on", "campaign_id": campaign.id,
            "associated_with_id": option.id, "live_to_give_purpose_id": purpose.id,
            "receipt_type": "non80g",
        })
        assert resp.status_code == 200
        donation = Donation.query.one()
        assert donation.associated_with_id == option.id
        assert donation.live_to_give_purpose_id == purpose.id

    def test_donate_page_lists_only_active_options(self, app, client):
        from models import AssociatedWith
        login(client)
        _mk_option(client, "Active Option")
        _mk_option(client, "Inactive Option")
        inactive = AssociatedWith.query.filter_by(name="Inactive Option").one()
        client.post(f"/admin/associated-with/{inactive.id}/toggle", follow_redirects=True)

        resp = client.get("/")
        html = resp.data.decode()
        assert "Active Option" in html
        assert "Inactive Option" not in html

    def test_festival_seva_page_offers_the_field(self, app, client):
        from extensions import db
        from models import AssociatedWith, Campaign
        login(client)
        campaign = Campaign.query.filter_by(name="Annadan").first()
        campaign.name = "Festivals"
        db.session.commit()
        _mk_option(client, "College Preaching")

        html = client.get("/festival-seva").data.decode()
        assert 'name="associated_with_id"' in html
        assert "College Preaching" in html


class TestAdminOfflineEntry:
    def test_single_entry_stores_associated_with(self, app, client):
        from models import AssociatedWith, Campaign, Donation
        login(client)
        _mk_option(client, "HG Achyutanand Pr")
        option = AssociatedWith.query.one()
        campaign = Campaign.query.filter_by(name="Annadan").first()

        client.post("/admin/donations/manual", data={
            "campaign_id": campaign.id, "amount": "1100", "full_name": "Offline Donor",
            "phone": "9876500066", "payment_mode": "cash", "associated_with_id": str(option.id),
        }, follow_redirects=True)
        donation = Donation.query.one()
        assert donation.associated_with_id == option.id

    def test_single_entry_rejects_unknown_associated_with(self, app, client):
        from models import Campaign, Donation
        login(client)
        campaign = Campaign.query.filter_by(name="Annadan").first()
        client.post("/admin/donations/manual", data={
            "campaign_id": campaign.id, "amount": "1100", "full_name": "Bad Offline Donor",
            "phone": "9876500077", "payment_mode": "cash", "associated_with_id": "999999",
        }, follow_redirects=True)
        assert Donation.query.count() == 0

    def test_bulk_import_resolves_associated_with_name(self, app, client):
        from models import AssociatedWith, Donation
        login(client)
        _mk_option(client, "Online Preaching")

        csv_text = (
            "full_name,campaign_name,amount,payment_mode,donation_date,associated_with_name\n"
            "Bulk Donor,Annadan,750,cash,2026-04-01,Online Preaching\n"
        )
        client.post("/admin/donations/bulk-import", data={
            "csv_file": (io.BytesIO(csv_text.encode()), "import.csv"),
            "action": "import",
        }, content_type="multipart/form-data", follow_redirects=True)

        donation = Donation.query.one()
        option = AssociatedWith.query.one()
        assert donation.associated_with_id == option.id

    def test_bulk_import_reports_unknown_associated_with_name(self, app, client):
        from models import Donation
        login(client)
        csv_text = (
            "full_name,campaign_name,amount,payment_mode,donation_date,associated_with_name\n"
            "Bulk Donor,Annadan,750,cash,2026-04-01,Nonexistent Program\n"
        )
        resp = client.post("/admin/donations/bulk-import", data={
            "csv_file": (io.BytesIO(csv_text.encode()), "import.csv"),
            "action": "import",
        }, content_type="multipart/form-data", follow_redirects=True)

        assert Donation.query.count() == 0
        assert b"not found" in resp.data

    def test_bulk_import_preview_also_validates_associated_with(self, app, client):
        """The dry-run preview must catch the same problems the real run
        would, or it promises an import the real run then refuses."""
        from models import Donation
        login(client)
        csv_text = (
            "full_name,campaign_name,amount,payment_mode,donation_date,associated_with_name\n"
            "Bulk Donor,Annadan,750,cash,2026-04-01,Nonexistent Program\n"
        )
        resp = client.post("/admin/donations/bulk-import", data={
            "csv_file": (io.BytesIO(csv_text.encode()), "import.csv"),
            "action": "preview",
        }, content_type="multipart/form-data", follow_redirects=True)

        assert Donation.query.count() == 0
        assert b"not found" in resp.data


class TestDonationsLog:
    def _seed_two_donations(self, db, campaign):
        from models import AssociatedWith, Donor, Donation
        opt_a = AssociatedWith(name="Option A")
        opt_b = AssociatedWith(name="Option B")
        db.session.add_all([opt_a, opt_b])
        db.session.flush()
        donor = Donor(full_name="Log Donor", phone="9876500088")
        db.session.add(donor)
        db.session.flush()
        db.session.add_all([
            Donation(donor_id=donor.id, campaign_id=campaign.id, amount=100, payment_mode="cash",
                      status="success", associated_with_id=opt_a.id),
            Donation(donor_id=donor.id, campaign_id=campaign.id, amount=200, payment_mode="cash",
                      status="success", associated_with_id=opt_b.id),
        ])
        db.session.commit()
        return opt_a, opt_b

    def test_filter_narrows_to_one_option(self, app, client):
        from extensions import db
        from models import Campaign
        login(client)
        campaign = Campaign.query.filter_by(name="Annadan").first()
        opt_a, opt_b = self._seed_two_donations(db, campaign)

        resp = client.get(f"/admin/donations?range=all&associated_with_id={opt_a.id}")
        html = resp.data.decode()
        # The filter dropdown itself always lists every option (including
        # Option B) regardless of the current filter -- so the assertion
        # has to look at the actual donation amounts in the results table,
        # not just whether "Option B" appears anywhere on the page.
        assert "Rs. 100" in html
        assert "Rs. 200" not in html

    def test_page_renders_fine_when_associated_with_unset(self, app, client):
        """A donation with no Associated With must not break the log page
        (the column/modal field both fall back to a plain '-')."""
        from extensions import db
        from models import Campaign, Donor, Donation
        login(client)
        campaign = Campaign.query.filter_by(name="Annadan").first()
        donor = Donor(full_name="No Assoc Log Donor", phone="9876500099")
        db.session.add(donor)
        db.session.flush()
        db.session.add(Donation(donor_id=donor.id, campaign_id=campaign.id, amount=300,
                                 payment_mode="cash", status="success"))
        db.session.commit()

        resp = client.get("/admin/donations?range=all")
        assert resp.status_code == 200
        assert "No Assoc Log Donor" in resp.data.decode()

    def test_csv_export_includes_associated_with_column(self, app, client):
        from extensions import db
        from models import Campaign
        login(client)
        campaign = Campaign.query.filter_by(name="Annadan").first()
        opt_a, _ = self._seed_two_donations(db, campaign)

        resp = client.get("/admin/export/donations?range=all")
        body = resp.data.decode()
        header = body.splitlines()[0]
        assert "Associated With" in header
        assert "Option A" in body


class TestAssociatedWithReport:
    def test_summary_totals_per_option(self, app, client):
        from extensions import db
        from models import AssociatedWith, Campaign, Donor, Donation
        login(client)
        campaign = Campaign.query.filter_by(name="Annadan").first()
        option = AssociatedWith(name="Reported Option")
        db.session.add(option)
        db.session.flush()
        donor = Donor(full_name="Report Donor", phone="9876511100")
        db.session.add(donor)
        db.session.flush()
        db.session.add_all([
            Donation(donor_id=donor.id, campaign_id=campaign.id, amount=1000, payment_mode="cash",
                      status="success", associated_with_id=option.id),
            Donation(donor_id=donor.id, campaign_id=campaign.id, amount=500, payment_mode="cash",
                      status="success", associated_with_id=option.id),
        ])
        db.session.commit()

        html = client.get("/admin/associated-with-report?range=all").data.decode()
        assert "Reported Option" in html
        assert "1,500" in html

    def test_unspecified_donations_are_broken_out_separately(self, app, client):
        from extensions import db
        from models import Campaign, Donor, Donation
        login(client)
        campaign = Campaign.query.filter_by(name="Annadan").first()
        donor = Donor(full_name="Unspecified Donor", phone="9876511111")
        db.session.add(donor)
        db.session.flush()
        db.session.add(Donation(donor_id=donor.id, campaign_id=campaign.id, amount=250,
                                 payment_mode="cash", status="success"))
        db.session.commit()

        html = client.get("/admin/associated-with-report?range=all").data.decode()
        assert "Not specified" in html

    def test_export_csv_includes_associated_with(self, app, client):
        from extensions import db
        from models import AssociatedWith, Campaign, Donor, Donation
        login(client)
        campaign = Campaign.query.filter_by(name="Annadan").first()
        option = AssociatedWith(name="Export Option")
        db.session.add(option)
        db.session.flush()
        donor = Donor(full_name="Export Donor", phone="9876511122")
        db.session.add(donor)
        db.session.flush()
        db.session.add(Donation(donor_id=donor.id, campaign_id=campaign.id, amount=700,
                                 payment_mode="cash", status="success", associated_with_id=option.id))
        db.session.commit()

        resp = client.get("/admin/associated-with-report/export?range=all")
        body = resp.data.decode()
        assert "Export Option" in body
        assert "700" in body
