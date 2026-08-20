"""The online payment flow, end to end, including the donor seeing a receipt.

The webhook has had tests for a while. The rest of the flow -- the browser
confirming a payment, the capture check, asking Razorpay directly when
confirmation is late, the redirect flow for in-app browsers, and the
success page itself -- had none, despite being where every donor-facing
problem this project has had actually lived.

The journey these follow is the one that matters: money leaves the donor,
a receipt number is issued exactly once, and the donor lands on a page
showing it with a working download.
"""
import hashlib
import hmac
import io
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

KEY_SECRET = "test_key_secret"


def _enable_razorpay(app):
    app.config["RAZORPAY_KEY_ID"] = "rzp_test_fake"
    app.config["RAZORPAY_KEY_SECRET"] = KEY_SECRET
    app.config["RAZORPAY_ENABLED"] = True


def _signature(order_id, payment_id, secret=KEY_SECRET):
    return hmac.new(secret.encode(), f"{order_id}|{payment_id}".encode(),
                    hashlib.sha256).hexdigest()


def _captured_payment(payment_id="pay_TEST1", order_id="order_TEST1", status="captured"):
    return {"id": payment_id, "order_id": order_id, "status": status,
            "method": "upi", "currency": "INR"}


def _mock_razorpay(payment=None, order_payments=None):
    """Stand in for the razorpay SDK. `payment` answers payment.fetch (the
    capture check); `order_payments` answers order.payments (reconcile)."""
    client = MagicMock()
    client.payment.fetch.return_value = payment or _captured_payment()
    client.order.payments.return_value = {"items": order_payments or []}
    client.order.create.return_value = {"id": "order_TEST1"}
    return patch("razorpay.Client", return_value=client), client


def _start_donation(app, client, amount=501, order_id="order_TEST1"):
    """Create a pending donation the way the public form does."""
    from models import Campaign
    campaign = Campaign.query.filter_by(name="Annadan").first()
    ctx, rz = _mock_razorpay()
    rz.order.create.return_value = {"id": order_id}
    with ctx:
        resp = client.post("/api/create-order", json={
            "campaign_id": campaign.id, "amount": amount,
            "full_name": "Ravi Sharma", "phone": "9876543210",
            "email": "ravi@example.com", "consent": "on",
            # Annadan is 80G-eligible, and create_order() now refuses an
            # 80G donation with no PAN (REG-036) -- this helper is used by
            # tests that care about the payment flow, not 80G/PAN
            # specifically, so it always supplies one.
            "pan": "ABCDE1234F",
        })
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()["donation_id"]


class TestDonorSeesTheirReceipt:
    """The journey the donor actually experiences."""

    def test_pay_then_land_on_a_receipt(self, app, client):
        from models import Donation
        _enable_razorpay(app)
        donation_id = _start_donation(app, client)

        ctx, _ = _mock_razorpay(payment=_captured_payment())
        with ctx:
            resp = client.post("/api/verify-payment", json={
                "donation_id": donation_id,
                "razorpay_order_id": "order_TEST1",
                "razorpay_payment_id": "pay_TEST1",
                "razorpay_signature": _signature("order_TEST1", "pay_TEST1"),
            })
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True and body["receipt_number"]

        # verify-payment just proved (via the Razorpay signature) that this
        # caller made this exact payment, so its response carries the same
        # token /receipt/<id> and /donate/success/<id> require -- see
        # public.py's donate_success()/REG-056 comment. The real JS appends
        # this to the redirect it makes (donation-payment.js's goToReceipt).
        assert body["token"], "verify-payment must hand back a receipt token"

        # Without that token, the success page must not show donor-specific
        # detail to just anyone who knows the id.
        bare = client.get(f"/donate/success/{donation_id}")
        assert bare.status_code == 200
        assert body["receipt_number"].encode() not in bare.data, \
            "success page showed the receipt number with no proof of ownership"

        # The page the browser is actually sent to (with the token) must
        # show the receipt.
        page = client.get(f"/donate/success/{donation_id}?t={body['token']}")
        assert page.status_code == 200
        assert b"Thank you" in page.data
        assert body["receipt_number"].encode() in page.data, \
            "success page didn't show the receipt number"

        # And the download link on it must work.
        from utils import receipt_access_token
        token = receipt_access_token(donation_id, app.config["SECRET_KEY"])
        pdf = client.get(f"/receipt/{donation_id}?t={token}")
        assert pdf.status_code == 200
        assert pdf.data.startswith(b"%PDF")

        d = Donation.query.get(donation_id)
        assert d.status == "success"
        assert d.receipt_pdf, "receipt PDF wasn't stored"

    def test_receipt_number_issued_only_once(self, app, client):
        """A retried confirmation must not burn a second number."""
        from models import Donation
        _enable_razorpay(app)
        donation_id = _start_donation(app, client)
        payload = {
            "donation_id": donation_id, "razorpay_order_id": "order_TEST1",
            "razorpay_payment_id": "pay_TEST1",
            "razorpay_signature": _signature("order_TEST1", "pay_TEST1"),
        }
        ctx, _ = _mock_razorpay(payment=_captured_payment())
        with ctx:
            first = client.post("/api/verify-payment", json=payload).get_json()
            second = client.post("/api/verify-payment", json=payload).get_json()
        assert first["receipt_number"] == second["receipt_number"]
        assert Donation.query.get(donation_id).receipt_number == first["receipt_number"]

    def test_status_endpoint_reports_success_for_polling(self, app, client):
        """The browser polls this to know when to redirect."""
        _enable_razorpay(app)
        donation_id = _start_donation(app, client)
        assert client.get(f"/api/donation-status/{donation_id}").get_json()["status"] == "pending"

        ctx, _ = _mock_razorpay(payment=_captured_payment())
        with ctx:
            client.post("/api/verify-payment", json={
                "donation_id": donation_id, "razorpay_order_id": "order_TEST1",
                "razorpay_payment_id": "pay_TEST1",
                "razorpay_signature": _signature("order_TEST1", "pay_TEST1")})
        body = client.get(f"/api/donation-status/{donation_id}").get_json()
        assert body["status"] == "success" and body["receipt_number"]


class TestSuccessPageStates:
    def test_pending_donation_does_not_claim_a_receipt(self, app, client):
        _enable_razorpay(app)
        donation_id = _start_donation(app, client)
        page = client.get(f"/donate/success/{donation_id}")
        assert b"Payment Pending" in page.data
        assert b"Download receipt" not in page.data

    def test_cancelled_donation_says_so(self, app, client):
        from extensions import db
        from models import Donation
        _enable_razorpay(app)
        donation_id = _start_donation(app, client)
        d = Donation.query.get(donation_id)
        d.status = "cancelled"
        db.session.commit()
        page = client.get(f"/donate/success/{donation_id}")
        assert b"Cancelled" in page.data
        assert b"Download receipt" not in page.data

    def test_receipt_refused_for_a_pending_donation(self, app, client):
        from utils import receipt_access_token
        _enable_razorpay(app)
        donation_id = _start_donation(app, client)
        token = receipt_access_token(donation_id, app.config["SECRET_KEY"])
        resp = client.get(f"/receipt/{donation_id}?t={token}", follow_redirects=True)
        assert not resp.data.startswith(b"%PDF")


class TestSignatureBinding:
    """A valid signature proves a payment exists, not whose it is."""

    def test_another_donations_payment_cannot_be_claimed(self, app, client):
        """Pay Rs 1 on your own order, then try to use that signature to
        finalize someone else's larger donation.

        Kept under Rs 49,000: above that the form requires PAN and address
        (Income Tax rules), which is a different rejection and would mask
        the one being tested."""
        from models import Donation
        _enable_razorpay(app)
        victim = _start_donation(app, client, amount=25000, order_id="order_VICTIM")
        attacker_sig = _signature("order_ATTACKER", "pay_ATTACKER")

        ctx, _ = _mock_razorpay(payment=_captured_payment())
        with ctx:
            resp = client.post("/api/verify-payment", json={
                "donation_id": victim,
                "razorpay_order_id": "order_ATTACKER",
                "razorpay_payment_id": "pay_ATTACKER",
                "razorpay_signature": attacker_sig,
            })
        assert resp.status_code == 400
        d = Donation.query.get(victim)
        assert d.status == "pending" and not d.receipt_number

    def test_bad_signature_cannot_downgrade_a_successful_donation(self, app, client):
        from models import Donation
        _enable_razorpay(app)
        donation_id = _start_donation(app, client)
        ctx, _ = _mock_razorpay(payment=_captured_payment())
        with ctx:
            client.post("/api/verify-payment", json={
                "donation_id": donation_id, "razorpay_order_id": "order_TEST1",
                "razorpay_payment_id": "pay_TEST1",
                "razorpay_signature": _signature("order_TEST1", "pay_TEST1")})
        receipt = Donation.query.get(donation_id).receipt_number

        client.post("/api/verify-payment", json={
            "donation_id": donation_id, "razorpay_order_id": "order_TEST1",
            "razorpay_payment_id": "pay_TEST1", "razorpay_signature": "rubbish"})
        d = Donation.query.get(donation_id)
        assert d.status == "success" and d.receipt_number == receipt

    def test_bad_signature_marks_a_pending_donation_failed(self, app, client):
        from models import Donation
        _enable_razorpay(app)
        donation_id = _start_donation(app, client)
        client.post("/api/verify-payment", json={
            "donation_id": donation_id, "razorpay_order_id": "order_TEST1",
            "razorpay_payment_id": "pay_TEST1", "razorpay_signature": "rubbish"})
        assert Donation.query.get(donation_id).status == "failed"


class TestCaptureGate:
    """An authorized-but-uncaptured payment is auto-refunded by Razorpay, so
    issuing an 80G receipt for one would certify a donation that reverses."""

    def test_uncaptured_payment_gets_no_receipt_yet(self, app, client):
        from models import Donation
        _enable_razorpay(app)
        donation_id = _start_donation(app, client)
        ctx, _ = _mock_razorpay(payment=_captured_payment(status="authorized"))
        with ctx:
            resp = client.post("/api/verify-payment", json={
                "donation_id": donation_id, "razorpay_order_id": "order_TEST1",
                "razorpay_payment_id": "pay_TEST1",
                "razorpay_signature": _signature("order_TEST1", "pay_TEST1")})
        assert resp.status_code == 202
        assert resp.get_json()["ok"] is False, "client would redirect to an empty receipt"
        d = Donation.query.get(donation_id)
        assert d.status == "pending" and not d.receipt_number
        assert d.razorpay_payment_id == "pay_TEST1", "payment id should still be recorded"

    def test_capture_confirmed_later_by_webhook(self, app, client):
        """The receipt isn't lost -- it arrives when capture does."""
        from models import Donation
        _enable_razorpay(app)
        app.config["RAZORPAY_WEBHOOK_SECRET"] = "whsec"
        donation_id = _start_donation(app, client)

        ctx, _ = _mock_razorpay(payment=_captured_payment(status="authorized"))
        with ctx:
            client.post("/api/verify-payment", json={
                "donation_id": donation_id, "razorpay_order_id": "order_TEST1",
                "razorpay_payment_id": "pay_TEST1",
                "razorpay_signature": _signature("order_TEST1", "pay_TEST1")})

        body = ('{"event":"payment.captured","payload":{"payment":{"entity":'
                '{"id":"pay_TEST1","order_id":"order_TEST1","status":"captured"}}}}')
        sig = hmac.new(b"whsec", body.encode(), hashlib.sha256).hexdigest()
        resp = client.post("/webhooks/razorpay", data=body,
                           headers={"X-Razorpay-Signature": sig,
                                    "Content-Type": "application/json"})
        assert resp.status_code == 200
        assert Donation.query.get(donation_id).receipt_number

    def test_razorpay_unreachable_falls_back_to_the_signature(self, app, client):
        """Blocking every receipt during a Razorpay outage would be worse
        than briefly trusting a signature that's already been verified."""
        from models import Donation
        _enable_razorpay(app)
        donation_id = _start_donation(app, client)
        with patch("razorpay.Client", side_effect=RuntimeError("network down")):
            resp = client.post("/api/verify-payment", json={
                "donation_id": donation_id, "razorpay_order_id": "order_TEST1",
                "razorpay_payment_id": "pay_TEST1",
                "razorpay_signature": _signature("order_TEST1", "pay_TEST1")})
        assert resp.status_code == 200
        assert Donation.query.get(donation_id).receipt_number


class TestReconcileWithRazorpay:
    """?verify=1 -- ask Razorpay directly instead of waiting to be told."""

    def test_finalizes_a_donation_razorpay_says_is_paid(self, app, client):
        from models import Donation
        _enable_razorpay(app)
        donation_id = _start_donation(app, client)

        ctx, _ = _mock_razorpay(order_payments=[_captured_payment()])
        with ctx:
            body = client.get(f"/api/donation-status/{donation_id}?verify=1").get_json()
        assert body["status"] == "success"
        assert Donation.query.get(donation_id).receipt_number

    def test_plain_poll_does_not_call_razorpay(self, app, client):
        """Polling every 3 seconds must not hammer their API."""
        _enable_razorpay(app)
        donation_id = _start_donation(app, client)
        ctx, rz = _mock_razorpay(order_payments=[_captured_payment()])
        with ctx:
            client.get(f"/api/donation-status/{donation_id}")
        rz.order.payments.assert_not_called()

    def test_authorized_only_is_not_treated_as_paid(self, app, client):
        from models import Donation
        _enable_razorpay(app)
        donation_id = _start_donation(app, client)
        ctx, _ = _mock_razorpay(order_payments=[_captured_payment(status="authorized")])
        with ctx:
            body = client.get(f"/api/donation-status/{donation_id}?verify=1").get_json()
        assert body["status"] == "pending"
        assert not Donation.query.get(donation_id).receipt_number

    def test_no_payment_found_leaves_it_pending(self, app, client):
        _enable_razorpay(app)
        donation_id = _start_donation(app, client)
        ctx, _ = _mock_razorpay(order_payments=[])
        with ctx:
            body = client.get(f"/api/donation-status/{donation_id}?verify=1").get_json()
        assert body["status"] == "pending"

    def test_razorpay_error_does_not_500_the_poll(self, app, client):
        _enable_razorpay(app)
        donation_id = _start_donation(app, client)
        with patch("razorpay.Client", side_effect=RuntimeError("boom")):
            resp = client.get(f"/api/donation-status/{donation_id}?verify=1")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "pending"

    def test_already_successful_donation_is_not_rechecked(self, app, client):
        _enable_razorpay(app)
        donation_id = _start_donation(app, client)
        ctx, _ = _mock_razorpay(payment=_captured_payment())
        with ctx:
            client.post("/api/verify-payment", json={
                "donation_id": donation_id, "razorpay_order_id": "order_TEST1",
                "razorpay_payment_id": "pay_TEST1",
                "razorpay_signature": _signature("order_TEST1", "pay_TEST1")})
        ctx2, rz2 = _mock_razorpay(order_payments=[_captured_payment()])
        with ctx2:
            client.get(f"/api/donation-status/{donation_id}?verify=1")
        rz2.order.payments.assert_not_called()


class TestRedirectFlowCallback:
    """/api/payment-callback -- Instagram, Messenger, Opera, UC Browser."""

    def _post_callback(self, client, order_id="order_TEST1", payment_id="pay_TEST1",
                       signature=None):
        return client.post("/api/payment-callback", data={
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature if signature is not None
                                  else _signature(order_id, payment_id),
        }, follow_redirects=False)

    def test_valid_callback_issues_a_receipt_and_redirects(self, app, client):
        from models import Donation
        _enable_razorpay(app)
        donation_id = _start_donation(app, client)
        ctx, _ = _mock_razorpay(payment=_captured_payment())
        with ctx:
            resp = self._post_callback(client)
        assert resp.status_code == 302
        assert f"/donate/success/{donation_id}" in resp.headers["Location"]
        assert Donation.query.get(donation_id).receipt_number

        # This flow (Instagram/Messenger/Opera/UC Browser) has no session or
        # cookie of its own -- see payment_callback()'s docstring -- so the
        # only way the donor's own browser can see its own receipt on the
        # page it's redirected to is a token in that redirect's URL itself.
        # Without one, REG-056 is back: the success page would either show
        # nothing donor-specific (safe but broken for this flow) or leak it
        # to anyone (unsafe). Confirm the redirect actually carries a token
        # that works.
        assert "t=" in resp.headers["Location"], \
            "redirect-flow callback must carry a receipt token; donate_success now requires one"
        page = client.get(resp.headers["Location"])
        assert page.status_code == 200
        assert Donation.query.get(donation_id).receipt_number.encode() in page.data

    def test_bad_signature_issues_no_receipt(self, app, client):
        from models import Donation
        _enable_razorpay(app)
        donation_id = _start_donation(app, client)
        resp = self._post_callback(client, signature="rubbish")
        assert resp.status_code == 302
        d = Donation.query.get(donation_id)
        assert not d.receipt_number and d.status == "failed"

    def test_unknown_order_is_handled_not_crashed(self, app, client):
        _enable_razorpay(app)
        _start_donation(app, client)
        resp = self._post_callback(client, order_id="order_NOPE")
        assert resp.status_code == 302        # a page, never a 500

    def test_uncaptured_payment_goes_to_the_status_page(self, app, client):
        from models import Donation
        _enable_razorpay(app)
        donation_id = _start_donation(app, client)
        ctx, _ = _mock_razorpay(payment=_captured_payment(status="authorized"))
        with ctx:
            resp = self._post_callback(client)
        assert f"/donate/success/{donation_id}" in resp.headers["Location"]
        assert not Donation.query.get(donation_id).receipt_number

    def test_callback_and_webhook_are_csrf_exempt(self, app):
        """Razorpay posts cross-origin with no token of ours; the signature
        is the authentication. Both routes carry @csrf.exempt, and neither
        had ever been exercised with CSRF actually on -- the rest of the
        suite runs with it disabled, so a broken exemption would have gone
        unnoticed until production.

        Builds its own app with CSRF enabled from the start, the way
        production boots. Toggling the flag on an already-created app is
        not the same thing and doesn't prove anything about it."""
        import hashlib as _h, hmac as _hm
        from app import create_app
        from extensions import db
        from models import Campaign

        csrf_app = create_app(test_config={
            "TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "WTF_CSRF_ENABLED": True, "SECRET_KEY": "t",
            "RAZORPAY_WEBHOOK_SECRET": "whsec",
        })
        with csrf_app.app_context():
            db.drop_all()
            db.create_all()
            db.session.add(Campaign(name="Annadan", is_80g=True))
            db.session.commit()
            c = csrf_app.test_client()

            resp = c.post("/api/payment-callback", data={
                "razorpay_order_id": "o", "razorpay_payment_id": "p",
                "razorpay_signature": "x"})
            assert resp.status_code != 400, "CSRF blocked Razorpay's callback"

            body = ('{"event":"payment.captured","payload":{"payment":{"entity":'
                    '{"id":"p","order_id":"o","status":"captured"}}}}')
            sig = _hm.new(b"whsec", body.encode(), _h.sha256).hexdigest()
            resp = c.post("/webhooks/razorpay", data=body,
                          headers={"X-Razorpay-Signature": sig,
                                   "Content-Type": "application/json"})
            assert resp.status_code == 200, "CSRF blocked Razorpay's webhook"
