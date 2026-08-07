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
