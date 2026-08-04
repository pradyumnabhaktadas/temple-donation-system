import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import login
from models import Donation, Campaign, AdminUser


class TestPublicPages:
    def test_donation_form_loads(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"Purpose" in resp.data or b"campaign" in resp.data.lower()

    def test_donor_lookup_page_loads(self, client):
        resp = client.get("/my-donations/")
        assert resp.status_code == 200


class TestDemoModeDonationFlow:
    """With no Razorpay keys configured, the app runs in demo mode: donations
    go straight to 'success' via /api/simulate-payment. This is the same
    code path a real payment takes after Razorpay verification, minus the
    signature check, so it's a solid end-to-end smoke test.

    Note: the `app` fixture (conftest.py) keeps a single app context open
    for the whole test, so ORM objects fetched here (e.g. `campaign`) stay
    attached to a live session for the duration of the test -- no need to
    re-open app_context() per query.
    """

    def test_full_donation_creates_donor_and_receipt(self, app, client):
        campaign = Campaign.query.filter_by(name="Annadan").first()

        order_resp = client.post(
            "/api/create-order",
            json={
                "campaign_id": campaign.id,
                "amount": 501,
                "full_name": "Test Donor",
                "phone": "9876500000",
                "email": "testdonor@example.com",
                "pan": "ABCDE1234F",
                "consent": "on",
            },
        )
        assert order_resp.status_code == 200
        donation_id = order_resp.get_json()["donation_id"]

        sim_resp = client.post("/api/simulate-payment", json={"donation_id": donation_id})
        assert sim_resp.status_code == 200
        receipt_number = sim_resp.get_json()["receipt_number"]
        assert receipt_number.startswith("032511/ISK")

        donation = Donation.query.get(donation_id)
        assert donation.status == "success"
        assert donation.donor.full_name == "Test Donor"

    def test_missing_consent_is_rejected(self, app, client):
        campaign = Campaign.query.filter_by(name="Annadan").first()

        resp = client.post(
            "/api/create-order",
            json={
                "campaign_id": campaign.id, "amount": 100,
                "full_name": "No Consent", "phone": "9111100000",
            },
        )
        assert resp.status_code == 400

    def test_invalid_pan_is_rejected(self, app, client):
        campaign = Campaign.query.filter_by(name="Annadan").first()

        resp = client.post(
            "/api/create-order",
            json={
                "campaign_id": campaign.id, "amount": 100,
                "full_name": "Bad Pan", "phone": "9111100001",
                "pan": "NOTAPAN", "consent": "on",
            },
        )
        assert resp.status_code == 400


class TestAdminAuth:
    def test_dashboard_requires_login(self, client):
        resp = client.get("/admin/dashboard")
        assert resp.status_code in (302, 401, 403)

    def test_login_with_correct_credentials(self, client):
        resp = login(client)
        assert resp.status_code == 200
        assert b"Dashboard" in resp.data or b"Collection" in resp.data

    def test_login_with_wrong_password_fails(self, client):
        resp = client.post(
            "/admin/login", data={"username": "testadmin", "password": "WrongPassword"}, follow_redirects=True
        )
        assert b"Invalid username or password" in resp.data

    def test_account_locks_after_max_failed_attempts(self, app, client):
        for _ in range(5):
            client.post("/admin/login", data={"username": "testadmin", "password": "wrong"})

        user = AdminUser.query.filter_by(username="testadmin").first()
        assert user.is_locked()

        # Even the correct password should now be rejected while locked.
        resp = client.post(
            "/admin/login", data={"username": "testadmin", "password": "TestPass123!"}, follow_redirects=True
        )
        assert b"Too many failed attempts" in resp.data


class TestRoleEnforcement:
    def test_staff_cannot_create_campaign(self, client):
        login(client, username="teststaff")
        resp = client.post(
            "/admin/campaigns",
            data={"name": "Should Not Be Created", "is_80g": "on"},
            follow_redirects=True,
        )
        assert b"requires an administrator" in resp.data

    def test_admin_can_create_campaign(self, app, client):
        login(client, username="testadmin")
        client.post(
            "/admin/campaigns",
            data={"name": "New Test Campaign", "is_80g": "on"},
            follow_redirects=True,
        )
        assert Campaign.query.filter_by(name="New Test Campaign").first() is not None
