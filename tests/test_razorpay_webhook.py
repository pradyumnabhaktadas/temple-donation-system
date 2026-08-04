import hashlib
import hmac
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from extensions import db
from models import Campaign, Donation


def _sign(body_bytes, secret):
    return hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()


def _post_webhook(client, app, body, secret=None):
    body_bytes = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if secret is not None:
        headers["X-Razorpay-Signature"] = _sign(body_bytes, secret)
    return client.post("/webhooks/razorpay", data=body_bytes, headers=headers)


def _make_pending_donation(app, order_id="order_test123"):
    campaign = Campaign.query.filter_by(name="Annadan").first()
    from public import find_or_create_donor

    donor = find_or_create_donor({"full_name": "Webhook Donor", "phone": "9111122223"})
    donation = Donation(
        donor_id=donor.id,
        campaign_id=campaign.id,
        amount=501,
        payment_mode="online",
        status="pending",
        recorded_by="online",
        razorpay_order_id=order_id,
        consent_given=True,
    )
    db.session.add(donation)
    db.session.commit()
    return donation


def _captured_event(order_id, payment_id="pay_test456", **entity_extra):
    entity = {
        "id": payment_id,
        "order_id": order_id,
        "status": "captured",
    }
    entity.update(entity_extra)
    return {
        "event": "payment.captured",
        "payload": {"payment": {"entity": entity}},
    }


class TestRazorpayWebhook:
    """Webhook is a server-to-server backstop for /api/verify-payment,
    verified with a separate RAZORPAY_WEBHOOK_SECRET rather than trusting
    the browser's callback alone."""

    def test_rejects_when_secret_not_configured(self, app, client):
        donation = _make_pending_donation(app)
        resp = _post_webhook(client, app, _captured_event(donation.razorpay_order_id), secret=None)

        assert resp.status_code == 400
        db.session.refresh(donation)
        assert donation.status == "pending"

    def test_rejects_invalid_signature(self, app, client):
        app.config["RAZORPAY_WEBHOOK_SECRET"] = "whsec_correct"
        donation = _make_pending_donation(app)

        body_bytes = json.dumps(_captured_event(donation.razorpay_order_id)).encode()
        resp = client.post(
            "/webhooks/razorpay",
            data=body_bytes,
            headers={"Content-Type": "application/json", "X-Razorpay-Signature": "not-the-right-signature"},
        )

        assert resp.status_code == 400
        db.session.refresh(donation)
        assert donation.status == "pending"

    def test_finalizes_donation_on_valid_payment_captured_event(self, app, client):
        app.config["RAZORPAY_WEBHOOK_SECRET"] = "whsec_correct"
        donation = _make_pending_donation(app)

        resp = _post_webhook(
            client, app, _captured_event(donation.razorpay_order_id, payment_id="pay_abc999"),
            secret="whsec_correct",
        )

        assert resp.status_code == 200
        db.session.refresh(donation)
        assert donation.status == "success"
        assert donation.receipt_number is not None
        assert donation.razorpay_payment_id == "pay_abc999"

    def test_captures_full_payment_details_from_upi_payload(self, app, client):
        app.config["RAZORPAY_WEBHOOK_SECRET"] = "whsec_correct"
        donation = _make_pending_donation(app)

        event = _captured_event(
            donation.razorpay_order_id,
            payment_id="pay_upi001",
            method="upi",
            vpa="donor@okhdfcbank",
            fee=1200,  # paise -> Rs. 12.00
            email="donor@example.com",
            contact="+919111122223",
        )
        resp = _post_webhook(client, app, event, secret="whsec_correct")

        assert resp.status_code == 200
        db.session.refresh(donation)
        assert donation.razorpay_method == "upi"
        assert donation.razorpay_reference == "donor@okhdfcbank"
        assert float(donation.razorpay_fee) == 12.0
        assert donation.razorpay_email == "donor@example.com"
        assert donation.razorpay_contact == "+919111122223"
        assert donation.razorpay_raw_payload is not None
        assert "donor@okhdfcbank" in donation.razorpay_raw_payload

    def test_captures_masked_card_reference(self, app, client):
        app.config["RAZORPAY_WEBHOOK_SECRET"] = "whsec_correct"
        donation = _make_pending_donation(app)

        event = _captured_event(
            donation.razorpay_order_id,
            payment_id="pay_card001",
            method="card",
            card={"network": "Visa", "last4": "1111"},
        )
        _post_webhook(client, app, event, secret="whsec_correct")

        db.session.refresh(donation)
        assert donation.razorpay_method == "card"
        assert donation.razorpay_reference == "Visa ****1111"

    def test_duplicate_event_is_idempotent(self, app, client):
        app.config["RAZORPAY_WEBHOOK_SECRET"] = "whsec_correct"
        donation = _make_pending_donation(app)

        _post_webhook(client, app, _captured_event(donation.razorpay_order_id), secret="whsec_correct")
        db.session.refresh(donation)
        first_receipt = donation.receipt_number

        # Razorpay retries webhooks that don't 2xx quickly, and may also
        # send both payment.captured and order.paid for the same payment --
        # a second delivery must not burn a second receipt number.
        resp2 = _post_webhook(client, app, _captured_event(donation.razorpay_order_id), secret="whsec_correct")
        db.session.refresh(donation)

        assert resp2.status_code == 200
        assert donation.receipt_number == first_receipt

    def test_ignores_unrelated_event_types(self, app, client):
        app.config["RAZORPAY_WEBHOOK_SECRET"] = "whsec_correct"
        donation = _make_pending_donation(app)
        event = {"event": "refund.created", "payload": {}}

        resp = _post_webhook(client, app, event, secret="whsec_correct")

        assert resp.status_code == 200
        db.session.refresh(donation)
        assert donation.status == "pending"

    def test_unknown_order_id_is_acknowledged_not_errored(self, app, client):
        app.config["RAZORPAY_WEBHOOK_SECRET"] = "whsec_correct"
        resp = _post_webhook(client, app, _captured_event("order_does_not_exist"), secret="whsec_correct")

        assert resp.status_code == 200
        assert resp.get_json()["matched"] is False
