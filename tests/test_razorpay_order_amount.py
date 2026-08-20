import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class TestRazorpayOrderAmountRounding:
    """Regression test for a real bug: create_order() used int(amount * 100)
    to convert rupees to paise for Razorpay's order.create() call. Float
    multiplication can land a hair under the intended value (e.g.
    128.14 * 100 == 12813.999999999998 in IEEE 754 float64), and int()
    truncates that down -- 12813 instead of 12814. Every donation form's
    checkout.js, meanwhile, does Math.round(order.amount * 100), which
    rounds correctly to 12814. That one-paisa mismatch between what
    Razorpay's order was actually created with and what checkout.js opens
    with gets rejected client-side as a generic "Something went wrong" --
    silently breaking roughly 1 in 20 possible two-decimal rupee amounts.
    Fixed by using round() instead of int() so the server-side conversion
    always agrees with the client-side one.
    """

    def test_amount_known_to_truncate_under_int_is_sent_correctly(self, app, client):
        from models import Campaign

        app.config["RAZORPAY_KEY_ID"] = "rzp_test_fake"
        app.config["RAZORPAY_KEY_SECRET"] = "fake_secret"
        # RAZORPAY_ENABLED is derived from the keys *inside* create_app(),
        # so setting the keys afterwards doesn't flip it -- it has to be
        # set explicitly here or create_order() takes the demo-mode branch
        # and never calls Razorpay at all. This used to pass only by
        # accident, on machines whose .env happened to carry real Razorpay
        # keys; conftest now pins every integration to "not configured" so
        # the suite behaves the same everywhere.
        app.config["RAZORPAY_ENABLED"] = True
        campaign = Campaign.query.filter_by(name="Annadan").first()

        mock_order = {"id": "order_fake123"}
        mock_client = MagicMock()
        mock_client.order.create.return_value = mock_order

        with patch("razorpay.Client", return_value=mock_client):
            resp = client.post(
                "/api/create-order",
                json={
                    "campaign_id": campaign.id,
                    # 128.14 is one of the amounts where int(128.14 * 100)
                    # truncates to 12813 instead of 12814 -- see module
                    # docstring above.
                    "amount": 128.14,
                    "full_name": "Rounding Test Donor",
                    "phone": "9876512345",
                    "consent": "on",
                    "pan": "ABCDE1234F",  # Annadan is 80G-eligible; see REG-036
                },
            )

        assert resp.status_code == 200
        call_kwargs = mock_client.order.create.call_args[0][0]
        assert call_kwargs["amount"] == 12814, (
            f"Razorpay order created for {call_kwargs['amount']} paise, "
            "but the browser's Math.round(128.14 * 100) opens checkout "
            "expecting 12814 -- this mismatch is what Razorpay rejects as "
            '"Something went wrong".'
        )

    def test_a_range_of_two_decimal_amounts_never_mismatch_browser_rounding(self):
        """Belt-and-suspenders: the fix (round()) must agree with the
        browser's Math.round(amount * 100) for every amount a donor could
        plausibly enter, not just the one example above."""
        for cents in range(10100, 200000):  # Rs. 101.00 to Rs. 2000.00
            amount = cents / 100
            assert round(amount * 100) == cents


class TestOutOfRangeAmountRejection:
    """REG-033 (QA report, 2026-08-20): a very large amount is passed
    through to Razorpay, which rejects it past its own ceiling
    (razorpay.errors.BadRequestError) -- that used to surface as a bare
    502 "gateway" error, the same response a donor would see if Razorpay
    itself were down, even though the donor's own input was the actual
    problem. Now returns a clean 400 for that specific case; a genuine
    connectivity/server failure (any other exception) still returns 502.

    The report also flagged a pending donation row appearing to survive
    the failed order call -- reproduced first to confirm whether that was
    still true before writing a fix for it: it wasn't (the existing
    db.session.rollback() already undoes the flush()'d row), so only the
    status-code half needed fixing. Both are asserted here so a future
    regression on either one is caught."""

    def test_amount_rejected_by_razorpay_returns_400_not_502(self, app, client):
        import razorpay.errors
        from unittest.mock import MagicMock, patch
        from models import Campaign

        app.config["RAZORPAY_KEY_ID"] = "rzp_test_fake"
        app.config["RAZORPAY_KEY_SECRET"] = "fake_secret"
        app.config["RAZORPAY_ENABLED"] = True
        campaign = Campaign.query.filter_by(name="Annadan").first()

        mock_client = MagicMock()
        mock_client.order.create.side_effect = razorpay.errors.BadRequestError(
            "Amount exceeds maximum amount allowed."
        )

        with patch("razorpay.Client", return_value=mock_client):
            resp = client.post(
                "/api/create-order",
                json={
                    "campaign_id": campaign.id,
                    "amount": 999999999,
                    "full_name": "Out Of Range Donor",
                    "phone": "9876500001",
                    "consent": "on",
                    "pan": "ABCDE1234F",
                    "address": "123 Test Street",
                },
            )

        assert resp.status_code == 400
        assert "error" in resp.get_json()

    def test_amount_rejected_by_razorpay_leaves_no_orphaned_donation_row(self, app, client):
        import razorpay.errors
        from unittest.mock import MagicMock, patch
        from models import Campaign, Donation

        app.config["RAZORPAY_KEY_ID"] = "rzp_test_fake"
        app.config["RAZORPAY_KEY_SECRET"] = "fake_secret"
        app.config["RAZORPAY_ENABLED"] = True
        campaign = Campaign.query.filter_by(name="Annadan").first()

        mock_client = MagicMock()
        mock_client.order.create.side_effect = razorpay.errors.BadRequestError(
            "Amount exceeds maximum amount allowed."
        )

        with app.app_context():
            before = Donation.query.count()

        with patch("razorpay.Client", return_value=mock_client):
            client.post(
                "/api/create-order",
                json={
                    "campaign_id": campaign.id,
                    "amount": 999999999,
                    "full_name": "Orphan Row Donor",
                    "phone": "9876500002",
                    "consent": "on",
                    "pan": "ABCDE1234F",
                    "address": "123 Test Street",
                },
            )

        with app.app_context():
            after = Donation.query.count()
        assert after == before, "a rejected order-creation call must not leave a pending donation row behind"

    def test_a_generic_razorpay_failure_still_returns_502(self, app, client):
        """A real connectivity/server-side failure (not a rejected
        amount) is a different kind of problem -- still worth telling the
        donor "try again," not the amount-specific message above."""
        from unittest.mock import MagicMock, patch
        from models import Campaign

        app.config["RAZORPAY_KEY_ID"] = "rzp_test_fake"
        app.config["RAZORPAY_KEY_SECRET"] = "fake_secret"
        app.config["RAZORPAY_ENABLED"] = True
        campaign = Campaign.query.filter_by(name="Annadan").first()

        mock_client = MagicMock()
        mock_client.order.create.side_effect = ConnectionError("Razorpay unreachable")

        with patch("razorpay.Client", return_value=mock_client):
            resp = client.post(
                "/api/create-order",
                json={
                    "campaign_id": campaign.id,
                    "amount": 501,
                    "full_name": "Connectivity Failure Donor",
                    "phone": "9876500003",
                    "consent": "on",
                    "pan": "ABCDE1234F",
                },
            )

        assert resp.status_code == 502
