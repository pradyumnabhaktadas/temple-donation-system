import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class TestGracefulErrors:
    """Malformed API requests used to raise an unhandled ValueError straight
    out of int()/float() and surface as a generic 500 error page. These
    should all come back as clean 400s with a JSON error body instead."""

    def test_create_order_rejects_non_numeric_campaign_id(self, client):
        resp = client.post(
            "/api/create-order",
            json={"campaign_id": "not-a-number", "amount": 100, "full_name": "X", "phone": "9000000000", "consent": "on"},
        )
        assert resp.status_code == 400
        assert "error" in resp.get_json()

    def test_create_order_rejects_missing_body(self, client):
        resp = client.post("/api/create-order", content_type="application/json", data="not json")
        assert resp.status_code == 400

    def test_create_order_rejects_non_numeric_amount(self, app, client):
        from models import Campaign
        campaign = Campaign.query.filter_by(name="Annadan").first()
        resp = client.post(
            "/api/create-order",
            json={"campaign_id": campaign.id, "amount": "abc", "full_name": "X", "phone": "9000000000", "consent": "on"},
        )
        assert resp.status_code == 400

    def test_verify_payment_rejects_non_numeric_donation_id(self, client):
        resp = client.post(
            "/api/verify-payment",
            json={"donation_id": "nope", "razorpay_order_id": "x", "razorpay_payment_id": "y", "razorpay_signature": "z"},
        )
        assert resp.status_code == 400

    def test_verify_payment_rejects_missing_signature_fields(self, app, client):
        from models import Campaign
        from public import find_or_create_donor
        from extensions import db
        from models import Donation

        campaign = Campaign.query.filter_by(name="Annadan").first()
        donor = find_or_create_donor({"full_name": "Err Test", "phone": "9012345678"})
        donation = Donation(donor_id=donor.id, campaign_id=campaign.id, amount=100, payment_mode="online", status="pending", recorded_by="online")
        db.session.add(donation)
        db.session.commit()

        resp = client.post("/api/verify-payment", json={"donation_id": donation.id})
        assert resp.status_code == 400

    def test_simulate_payment_rejects_non_numeric_donation_id(self, client):
        resp = client.post("/api/simulate-payment", json={"donation_id": "nope"})
        assert resp.status_code == 400
