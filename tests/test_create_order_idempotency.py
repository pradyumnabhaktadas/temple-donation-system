"""REG-032 (QA report, 2026-08-20): two identical /api/create-order
requests fired back-to-back -- a donor double-clicking, or retrying after
the pay button re-enables on modal.ondismiss/payment.failed -- used to
create two separate donations and two separate Razorpay orders for what
was really one donation intent. The report's own PAY-12 case showed the
only guard was the client's payBtn.disabled flag, which the code itself
re-enables on failure paths and is absent entirely for a non-browser
client (or two genuinely concurrent requests via Promise.all).

create_order() now reuses an existing pending donation for the same
(donor, campaign, amount, purpose/property/festival/seva) if one was
created in the last few minutes and -- for a live Razorpay payment --
already has an order attached, instead of minting a second one.
"""
from unittest.mock import MagicMock, patch


def _order(client, **overrides):
    data = {
        "amount": 501, "full_name": "Dedup Test Donor", "phone": "9876500099", "consent": "on",
        "pan": "ABCDE1234F",
    }
    data.update(overrides)
    return client.post("/api/create-order", json=data)


class TestLiveRazorpayDeduplication:
    def _enable_razorpay(self, app):
        app.config["RAZORPAY_KEY_ID"] = "rzp_test_fake"
        app.config["RAZORPAY_KEY_SECRET"] = "fake_secret"
        app.config["RAZORPAY_ENABLED"] = True

    def test_two_identical_requests_reuse_the_same_donation_and_order(self, app, client):
        from models import Campaign
        self._enable_razorpay(app)
        campaign = Campaign.query.filter_by(name="Annadan").first()

        mock_client = MagicMock()
        mock_client.order.create.return_value = {"id": "order_dedup_test"}

        with patch("razorpay.Client", return_value=mock_client):
            r1 = _order(client, campaign_id=campaign.id)
            r2 = _order(client, campaign_id=campaign.id)

        assert r1.status_code == 200 and r2.status_code == 200
        assert r1.get_json()["donation_id"] == r2.get_json()["donation_id"]
        assert r1.get_json()["order_id"] == r2.get_json()["order_id"]
        assert mock_client.order.create.call_count == 1, "Razorpay should only be called once for the deduped pair"

    def test_a_different_amount_is_not_deduplicated(self, app, client):
        """The dedup key includes amount -- a donor who changes their mind
        about the amount and resubmits must get a fresh donation, not be
        silently stuck with the first one."""
        from models import Campaign
        self._enable_razorpay(app)
        campaign = Campaign.query.filter_by(name="Annadan").first()

        mock_client = MagicMock()
        mock_client.order.create.side_effect = [{"id": "order_a"}, {"id": "order_b"}]

        with patch("razorpay.Client", return_value=mock_client):
            r1 = _order(client, campaign_id=campaign.id, amount=501)
            r2 = _order(client, campaign_id=campaign.id, amount=1001)

        assert r1.get_json()["donation_id"] != r2.get_json()["donation_id"]
        assert mock_client.order.create.call_count == 2

    def test_a_different_donor_is_not_deduplicated(self, app, client):
        """Two different people donating the same amount to the same
        campaign at the same time must not collide with each other."""
        from models import Campaign
        self._enable_razorpay(app)
        campaign = Campaign.query.filter_by(name="Annadan").first()

        mock_client = MagicMock()
        mock_client.order.create.side_effect = [{"id": "order_a"}, {"id": "order_b"}]

        with patch("razorpay.Client", return_value=mock_client):
            # Different PANs, not just different names/phones -- PAN is
            # find_or_create_donor()'s strongest identity signal, so two
            # requests sharing one PAN are correctly treated as the same
            # donor regardless of name/phone (see its own docstring).
            r1 = _order(client, campaign_id=campaign.id, phone="9876500001", full_name="Donor One", pan="ABCDE1234F")
            r2 = _order(client, campaign_id=campaign.id, phone="9876500002", full_name="Donor Two", pan="PQRSX5678K")

        assert r1.get_json()["donation_id"] != r2.get_json()["donation_id"]
        assert mock_client.order.create.call_count == 2

    def test_a_half_created_row_with_no_order_yet_is_not_reused(self, app, client):
        """If a prior request created the pending donation row but never
        got as far as attaching a Razorpay order (e.g. it crashed in
        between), that row must not be handed back as if it were a
        complete, checkout-ready order -- a fresh attempt should try
        order creation again rather than reuse a broken half-state."""
        from extensions import db
        from models import Campaign, Donor, Donation
        self._enable_razorpay(app)
        campaign = Campaign.query.filter_by(name="Annadan").first()

        with app.app_context():
            donor = Donor(full_name="Dedup Test Donor", phone="9876500099", pan="ABCDE1234F")
            db.session.add(donor)
            db.session.commit()
            stuck = Donation(
                donor_id=donor.id, campaign_id=campaign.id, amount=501, payment_mode="online",
                status="pending", recorded_by="online", razorpay_order_id=None,
            )
            db.session.add(stuck)
            db.session.commit()
            stuck_id = stuck.id

        mock_client = MagicMock()
        mock_client.order.create.return_value = {"id": "order_fresh"}

        with patch("razorpay.Client", return_value=mock_client):
            resp = _order(client, campaign_id=campaign.id)

        assert resp.status_code == 200
        assert resp.get_json()["donation_id"] != stuck_id
        assert mock_client.order.create.call_count == 1


class TestDemoModeDeduplication:
    """Razorpay disabled (demo/simulate mode) has no order to attach, so
    the dedup check there is just "a matching pending donation exists,"
    with no razorpay_order_id requirement."""

    def test_two_identical_requests_reuse_the_same_donation(self, app, client):
        from models import Campaign
        campaign = Campaign.query.filter_by(name="Annadan").first()

        r1 = _order(client, campaign_id=campaign.id)
        r2 = _order(client, campaign_id=campaign.id)

        assert r1.status_code == 200 and r2.status_code == 200
        assert r1.get_json()["donation_id"] == r2.get_json()["donation_id"]
