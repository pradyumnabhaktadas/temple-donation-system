"""End-to-end coverage for preaching-center collection links and reports."""
from decimal import Decimal

from conftest import login


def _create_center(client, name="Dwarka Preaching", slug="dwarka"):
    return client.post("/admin/preaching-centers", data={
        "name": name, "slug": slug, "city": "New Delhi",
        "target_amount": "100000", "target_period": "monthly",
    }, follow_redirects=True)


class TestPreachingCenterAdministration:
    def test_admin_can_create_center_with_permanent_slug(self, app, client):
        from models import PreachingCenter, PreachingCenterPurpose
        login(client)
        response = _create_center(client)
        assert response.status_code == 200
        center = PreachingCenter.query.one()
        assert (center.name, center.slug, center.city) == ("Dwarka Preaching", "dwarka", "New Delhi")
        assert {link.purpose.name for link in PreachingCenterPurpose.query.filter_by(preaching_center_id=center.id)} == {
            "Deity Seva", "Monthly Nitya Seva"
        }

    def test_center_admin_can_add_any_new_purpose(self, app, client):
        from models import PreachingCenter, PreachingCenterPurpose
        login(client)
        _create_center(client)
        center = PreachingCenter.query.one()
        edit_page = client.get(f"/admin/preaching-centers/{center.id}/edit")
        assert edit_page.status_code == 200
        assert b"Deity Seva" in edit_page.data
        assert b"Monthly Nitya Seva" in edit_page.data
        response = client.post(f"/admin/preaching-centers/{center.id}/edit", data={
            "name": center.name, "new_purpose_name": "Book Distribution",
            "new_purpose_is_80g": "1",
            "purpose_ids": [str(link.live_to_give_purpose_id) for link in center.purpose_links],
        }, follow_redirects=True)
        assert response.status_code == 200
        links = {
            link.purpose.name: link.purpose.is_80g
            for link in PreachingCenterPurpose.query.filter_by(preaching_center_id=center.id)
        }
        assert "Book Distribution" in links
        assert links["Book Distribution"] is True

    def test_reserved_or_invalid_slug_is_rejected(self, app, client):
        from models import PreachingCenter
        login(client)
        _create_center(client, slug="admin")
        _create_center(client, slug="Not Valid")
        assert PreachingCenter.query.count() == 0

    def test_edit_does_not_allow_slug_changes(self, app, client):
        from models import PreachingCenter
        login(client)
        _create_center(client)
        center = PreachingCenter.query.one()
        response = client.post(f"/admin/preaching-centers/{center.id}/edit", data={
            "name": "Dwarka East", "city": "Delhi", "target_amount": "200000",
            "target_period": "financial_year", "slug": "changed-link",
        }, follow_redirects=True)
        assert response.status_code == 200
        center = PreachingCenter.query.one()
        assert center.name == "Dwarka East"
        assert center.slug == "dwarka"

    def test_staff_cannot_create_or_edit_center(self, app, client):
        from models import PreachingCenter
        login(client, username="teststaff")
        _create_center(client)
        assert PreachingCenter.query.count() == 0


class TestPreachingCenterPublicCollection:
    def _live_to_give(self, app):
        from extensions import db
        from models import Campaign, LiveToGivePurpose
        campaign = Campaign(name="Live To Give", is_80g=False)
        purpose = LiveToGivePurpose(name="Temple Seva", is_80g=False)
        db.session.add_all([campaign, purpose])
        db.session.commit()
        return campaign, purpose

    def test_center_link_keeps_purpose_and_hides_association(self, app, client):
        from models import PreachingCenter
        login(client)
        _create_center(client)
        self._live_to_give(app)
        response = client.get("/pc/dwarka")
        assert response.status_code == 200
        assert b"Donation Purpose" in response.data
        assert b"associated_with_id" not in response.data
        assert response.headers["X-Robots-Tag"] == "noindex, nofollow"
        assert PreachingCenter.query.one().is_active is True

    def test_public_navigation_lists_only_active_centers(self, app, client):
        from extensions import db
        from models import PreachingCenter
        login(client)
        _create_center(client)
        inactive = PreachingCenter(name="Inactive Center", slug="inactive-center", is_active=False)
        db.session.add(inactive)
        db.session.commit()

        response = client.get("/")
        assert response.status_code == 200
        assert b"Preaching Centers" in response.data
        assert b"/pc/dwarka" in response.data
        assert b"/pc/inactive-center" not in response.data

    def test_order_records_center_server_side(self, app, client):
        from models import Campaign, Donation, LiveToGivePurpose, PreachingCenter
        login(client)
        _create_center(client)
        self._live_to_give(app)
        campaign = Campaign.query.filter_by(name="Live To Give").one()
        purpose = LiveToGivePurpose.query.filter_by(name="Deity Seva").one()
        response = client.post("/api/create-order", json={
            "amount": 501, "full_name": "Center Donor", "phone": "9876501100",
            "consent": "on", "campaign_id": campaign.id, "live_to_give_purpose_id": purpose.id,
            "preaching_center_slug": "dwarka",
        })
        assert response.status_code == 200
        donation = Donation.query.one()
        center = PreachingCenter.query.one()
        assert donation.preaching_center_id == center.id
        assert donation.preaching_center_name == center.name

    def test_order_rejects_inactive_center(self, app, client):
        from models import Campaign, Donation, PreachingCenter
        from extensions import db
        login(client)
        _create_center(client)
        center = PreachingCenter.query.one()
        center.is_active = False
        db.session.commit()
        campaign = Campaign.query.filter_by(name="Annadan").one()
        response = client.post("/api/create-order", json={
            "amount": 501, "full_name": "Center Donor", "phone": "9876501101",
            "consent": "on", "campaign_id": campaign.id, "pan": "ABCDE1234F",
            "preaching_center_slug": "dwarka",
        })
        assert response.status_code == 400
        assert Donation.query.count() == 0

    def test_unknown_center_shows_unavailable_page(self, app, client):
        response = client.get("/pc/not-a-center")
        assert response.status_code == 404
        assert b"collection link unavailable" in response.data.lower()


class TestPreachingCenterOfflineCollection:
    """The counter form must be as safe as the public center link."""

    def test_offline_cash_donation_is_recorded_under_the_center_campaign(self, app, client):
        from models import Campaign, Donation, LiveToGivePurpose, PreachingCenter
        from public import _preaching_center_campaign

        login(client)
        _create_center(client)
        center = PreachingCenter.query.one()
        campaign = _preaching_center_campaign(create=True)
        purpose = LiveToGivePurpose.query.filter_by(name="Deity Seva").one()

        response = client.post("/admin/donations/manual", data={
            "campaign_id": campaign.id,
            "preaching_center_id": center.id,
            "live_to_give_purpose_id": purpose.id,
            "receipt_type": "non80g",
            "amount": "501",
            "payment_mode": "cash",
            "donation_date": "2026-10-19",
            "full_name": "Offline Center Donor",
            "phone": "9876501144",
        }, follow_redirects=False)

        assert response.status_code == 302
        donation = Donation.query.one()
        assert donation.campaign_id == campaign.id
        assert donation.preaching_center_id == center.id
        assert donation.live_to_give_purpose_id == purpose.id
        assert donation.effective_is_80g is False

    def test_offline_form_groups_80g_and_non_80g_purposes(self, app, client):
        from extensions import db
        from models import LiveToGivePurpose

        login(client)
        db.session.add_all([
            LiveToGivePurpose(name="Center 80G Seva", is_80g=True),
            LiveToGivePurpose(name="Center Regular Seva", is_80g=False),
        ])
        db.session.commit()

        response = client.get("/admin/donations/manual")
        assert response.status_code == 200
        assert b'<optgroup label="80G Eligible">' in response.data
        assert b'<optgroup label="Non-80G">' in response.data
        assert b"Center 80G Seva" in response.data
        assert b"data-is80g=\"1\"" in response.data

    def test_offline_center_donation_rejects_an_unassigned_purpose(self, app, client):
        from extensions import db
        from models import Campaign, Donation, LiveToGivePurpose, PreachingCenter
        from public import _preaching_center_campaign

        login(client)
        _create_center(client)
        center = PreachingCenter.query.one()
        campaign = _preaching_center_campaign(create=True)
        unrelated = LiveToGivePurpose(name="Not Available At This Center", is_80g=False)
        db.session.add(unrelated)
        db.session.commit()

        response = client.post("/admin/donations/manual", data={
            "campaign_id": campaign.id,
            "preaching_center_id": center.id,
            "live_to_give_purpose_id": unrelated.id,
            "receipt_type": "non80g", "amount": "501", "payment_mode": "cash",
            "donation_date": "2026-10-19", "full_name": "Center Donor", "phone": "9876501145",
        }, follow_redirects=True)

        assert response.status_code == 200
        assert b"not available for the selected preaching center" in response.data
        assert Donation.query.count() == 0

    def test_offline_80g_center_donation_requires_pan(self, app, client):
        from extensions import db
        from models import Campaign, Donation, LiveToGivePurpose, PreachingCenter, PreachingCenterPurpose
        from public import _preaching_center_campaign

        login(client)
        _create_center(client)
        center = PreachingCenter.query.one()
        campaign = _preaching_center_campaign(create=True)
        eligible = LiveToGivePurpose(name="Eligible Center Seva", is_80g=True)
        db.session.add(eligible)
        db.session.flush()
        db.session.add(PreachingCenterPurpose(
            preaching_center_id=center.id, live_to_give_purpose_id=eligible.id,
        ))
        db.session.commit()

        response = client.post("/admin/donations/manual", data={
            "campaign_id": campaign.id,
            "preaching_center_id": center.id,
            "live_to_give_purpose_id": eligible.id,
            "receipt_type": "80g", "amount": "501", "payment_mode": "cash",
            "donation_date": "2026-10-19", "full_name": "80G Center Donor", "phone": "9876501146",
        }, follow_redirects=True)

        assert response.status_code == 200
        assert b"PAN is required for an 80G receipt" in response.data
        assert Donation.query.count() == 0


class TestPreachingCenterReporting:
    def test_dashboard_shows_center_totals(self, app, client):
        from extensions import db
        from models import Campaign, Donor, Donation, PreachingCenter
        login(client)
        _create_center(client)
        center = PreachingCenter.query.one()
        campaign = Campaign.query.filter_by(name="Annadan").one()
        donor = Donor(full_name="Dashboard Donor", phone="9876501103")
        db.session.add(donor)
        db.session.flush()
        db.session.add(Donation(
            donor_id=donor.id, campaign_id=campaign.id, amount=Decimal("2000"),
            payment_mode="cash", status="success", preaching_center_id=center.id,
            preaching_center_name=center.name,
        ))
        db.session.commit()
        response = client.get("/admin/preaching-centers/dashboard")
        assert response.status_code == 200
        assert b"Center Collection Dashboard" in response.data
        assert b"Rs. 2,000" in response.data

    def test_report_counts_only_successful_donations(self, app, client):
        from extensions import db
        from models import Campaign, Donor, Donation, PreachingCenter
        login(client)
        _create_center(client)
        center = PreachingCenter.query.one()
        campaign = Campaign.query.filter_by(name="Annadan").one()
        donor = Donor(full_name="Report Donor", phone="9876501102")
        db.session.add(donor)
        db.session.flush()
        db.session.add_all([
            Donation(donor_id=donor.id, campaign_id=campaign.id, amount=Decimal("1000"), payment_mode="cash", status="success", preaching_center_id=center.id, preaching_center_name=center.name),
            Donation(donor_id=donor.id, campaign_id=campaign.id, amount=Decimal("500"), payment_mode="cash", status="pending", preaching_center_id=center.id, preaching_center_name=center.name),
        ])
        db.session.commit()
        report = client.get(f"/admin/preaching-centers/{center.id}")
        listing = client.get("/admin/preaching-centers")
        assert report.status_code == 200
        assert b"Rs. 1,000" in report.data
        assert b"Rs. 1,000" in listing.data
