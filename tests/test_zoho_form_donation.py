"""Tests for the Zoho Forms donation webhook (public.zoho_form_donation_webhook,
POST /internal/zoho-form-donation).

Context: some collection happens through separate Zoho Forms (each with
its own Razorpay-backed payment field configured inside Zoho) rather than
this site's own donate.html. This route turns a payment-confirmed Zoho
submission into a real donation here -- real receipt number, real PDF,
same email/WhatsApp receipt as a donation made directly on this site --
via the same _finalize_success() every other confirmation path uses. See
README's "Zoho Forms" section and config.py's ZOHO_FORMS_WEBHOOK_TOKEN
docstring for the full design.

Authentication mirrors /internal/daily-report/send (test_daily_report_relay.py)
almost exactly -- same shared-secret-header pattern, different token.

Regression context for the Razorpay-verification design (TestRazorpayVerification
below): a live production submission's own Zoho record went on to show
Payment Status "Completed", but the *only* webhook call this route ever
received for it carried status "processing", and no follow-up call ever
arrived -- Zoho counts a 200 response as a successfully delivered webhook
regardless of what this route did with it, so nothing on Zoho's side ever
retried, and the donation was silently lost. The fix: this route no longer
trusts Zoho's self-reported payment_status as the pass/fail gate at all --
it asks Razorpay directly whether the extracted payment ID is captured.
"""
import os
import re
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

TOKEN = "test-zoho-token"
URL = "/internal/zoho-form-donation"


def _extract_payment_id(raw):
    m = re.search(r"pay_\w+", raw or "")
    return m.group() if m else None


def _razorpay_patch(payment_id, status="captured", side_effect=None):
    """Mirrors test_payment_flow.py's _mock_razorpay -- stands in for the
    razorpay SDK so _zoho_payment_is_captured's client.payment.fetch call
    never touches the network. status="captured" is the default success
    case; pass a different status (or side_effect) to exercise the
    "not yet captured" / "couldn't verify" branches."""
    client = MagicMock()
    if side_effect is not None:
        client.payment.fetch.side_effect = side_effect
    else:
        client.payment.fetch.return_value = {"id": payment_id, "status": status}
    return patch("razorpay.Client", return_value=client)


def _post(client, app, campaign="Annadan", headers=True, razorpay_status="captured",
          razorpay_side_effect=None, **overrides):
    """POSTs to the Zoho webhook, with RAZORPAY_ENABLED on and the
    Razorpay client mocked so _zoho_payment_is_captured (the route's
    actual pass/fail gate) sees whatever this call asks for, without any
    real network access. razorpay_status="captured" by default so most
    tests exercise the happy path without extra boilerplate; pass a
    different status (e.g. "authorized") or razorpay_side_effect (an
    exception, to simulate an unreachable Razorpay) for the other
    branches."""
    app.config["RAZORPAY_ENABLED"] = True
    payload = {
        "full_name": "Zoho Donor",
        "phone": "9811100011",
        "pan": "ABCDE1234F",  # Annadan (the default campaign here) is fixed 80G -- needs a PAN on file
        "amount": "2100",
        "payment_status": "success",
        "payment_transaction_id": "pay_ZohoTest001",
    }
    payload.update(overrides)
    hdrs = {"X-Zoho-Webhook-Token": TOKEN} if headers else {}
    payment_id = _extract_payment_id(payload.get("payment_transaction_id"))
    with _razorpay_patch(payment_id, status=razorpay_status, side_effect=razorpay_side_effect):
        return client.post(f"{URL}?campaign={campaign}", json=payload, headers=hdrs)


class TestAuth:
    def test_returns_503_when_not_configured(self, client, app):
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = ""
        resp = _post(client, app)
        assert resp.status_code == 503

    def test_rejects_missing_token(self, client, app):
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = _post(client, app, headers=False)
        assert resp.status_code == 401

    def test_rejects_wrong_token(self, client, app):
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = client.post(
            f"{URL}?campaign=Annadan",
            json={"amount": "100", "payment_status": "success", "payment_transaction_id": "x"},
            headers={"X-Zoho-Webhook-Token": "wrong"},
        )
        assert resp.status_code == 401


class TestCampaignResolution:
    def test_missing_campaign_param_is_rejected(self, client, app):
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = client.post(
            URL, json={"amount": "100", "payment_status": "success", "payment_transaction_id": "x"},
            headers={"X-Zoho-Webhook-Token": TOKEN},
        )
        assert resp.status_code == 400

    def test_unknown_campaign_is_rejected(self, client, app):
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = _post(client, app, campaign="Does Not Exist")
        assert resp.status_code == 400
        assert Donation.query.count() == 0

    def test_campaign_match_is_case_insensitive(self, client, app):
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = _post(client, app, campaign="annadan")
        assert resp.status_code == 200
        assert Donation.query.count() == 1


class TestPaymentStatusGate:
    """Zoho's own payment_status is advisory only now (see the route's
    docstring and the module docstring above) -- these tests cover the
    one thing it still gates: whether a transaction id is expected yet at
    all. Whether a donation actually gets created is covered by
    TestRazorpayVerification below."""

    def test_no_transaction_id_and_a_non_success_status_is_skipped(self, client, app):
        """The normal shape of Zoho's early async call: no payment id has
        arrived yet, nothing to verify against Razorpay, no donation."""
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = _post(client, app, payment_status="pending", payment_transaction_id="")
        assert resp.status_code == 200
        assert resp.get_json()["skipped"] == "no payment transaction id yet"
        assert Donation.query.count() == 0

    def test_real_zoho_processing_not_needed_with_no_id_is_skipped(self, client, app):
        """"Processing not needed" (confirmed from a live account) is
        Zoho's value for a submission that never triggered the payment
        field at all -- no id, no donation."""
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = _post(client, app, payment_status="Processing not needed", payment_transaction_id="")
        assert resp.status_code == 200
        assert Donation.query.count() == 0

    def test_missing_transaction_id_on_a_completed_label_is_rejected(self, client, app):
        """A "Completed" label with no transaction id at all is genuinely
        anomalous -- flagged as an error (400) rather than silently
        skipped like a normal early/no-payment call."""
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = _post(client, app, payment_transaction_id="")
        assert resp.status_code == 400
        assert Donation.query.count() == 0

    def test_unrecognisable_transaction_id_on_a_completed_label_is_rejected(self, client, app):
        """A "Completed" status with a transaction_id that doesn't contain
        a genuine-looking Razorpay payment ID (no "pay_..." substring) must
        be rejected outright -- a receipt must only ever be issued against
        something that actually looks like a real payment."""
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = _post(client, app, payment_transaction_id="not-a-real-transaction-id")
        assert resp.status_code == 400
        assert Donation.query.count() == 0


class TestRazorpayVerification:
    """The actual pass/fail gate: whatever Zoho's own payment_status says,
    a donation is only ever created once Razorpay itself confirms the
    extracted payment ID is captured."""

    def test_regression_stale_processing_label_with_a_captured_payment_creates_a_donation(self, client, app):
        """Reproduces the exact production incident: Zoho's own status
        label still reads "processing" (the one and only call this route
        ever receives for it), but Razorpay itself already shows the
        payment as captured. The donation must be created off this single
        call -- no second webhook call is required, and none is assumed."""
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = _post(
            client, app, payment_status="processing", payment_transaction_id="pay_StaleLabelTest1",
            razorpay_status="captured",
        )
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True
        d = Donation.query.one()
        assert d.status == "success"
        assert d.razorpay_payment_id == "pay_StaleLabelTest1"
        assert d.receipt_number

    def test_completed_label_but_razorpay_says_not_yet_captured_is_skipped(self, client, app):
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = _post(
            client, app, payment_transaction_id="pay_NotCapturedYet",
            razorpay_status="authorized",
        )
        assert resp.status_code == 200
        assert resp.get_json()["skipped"] == "payment not yet captured"
        assert Donation.query.count() == 0

    def test_unreachable_razorpay_returns_502_and_creates_nothing(self, client, app):
        """Deliberately fails closed (unlike _payment_is_captured's
        fail-open, used by the site's own already-signature-verified
        flow) -- this webhook has no other proof the payment is genuine,
        so an inability to check must surface as a failure, not a silent
        skip. A non-2xx here is what makes Zoho log the call under that
        form's "Webhooks - Failed Entries", re-pushable once Razorpay is
        reachable again."""
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = _post(
            client, app, payment_transaction_id="pay_Unreachable1",
            razorpay_side_effect=RuntimeError("connection reset"),
        )
        assert resp.status_code == 502
        assert Donation.query.count() == 0

    def test_razorpay_disabled_returns_502_and_creates_nothing(self, client, app):
        """_post's helper turns RAZORPAY_ENABLED on for convenience -- this
        test posts directly instead, with it deliberately left off, to
        cover _zoho_payment_is_captured's "Razorpay isn't configured on
        this deployment" branch."""
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        app.config["RAZORPAY_ENABLED"] = False
        resp = client.post(
            f"{URL}?campaign=Annadan",
            json={
                "full_name": "No Razorpay", "phone": "9811100011", "pan": "ABCDE1234F",
                "amount": "2100", "payment_status": "Completed",
                "payment_transaction_id": "pay_NoRazorpayDisabled",
            },
            headers={"X-Zoho-Webhook-Token": TOKEN},
        )
        assert resp.status_code == 502
        assert Donation.query.filter_by(razorpay_payment_id="pay_NoRazorpayDisabled").count() == 0

    def test_retries_a_transient_razorpay_failure_then_succeeds(self, client, app, monkeypatch):
        """_zoho_payment_is_captured retries (utils.retry, same helper the
        daily report's email/WhatsApp sends use) -- a single transient
        network hiccup talking to Razorpay shouldn't be the difference
        between a real donation getting its receipt or not."""
        from models import Donation
        monkeypatch.setattr("utils.time.sleep", lambda s: None)
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        app.config["RAZORPAY_ENABLED"] = True

        mock_client = MagicMock()
        mock_client.payment.fetch.side_effect = [
            RuntimeError("connection reset"), RuntimeError("connection reset"),
            {"id": "pay_RetryTest1", "status": "captured"},
        ]
        with patch("razorpay.Client", return_value=mock_client):
            resp = client.post(
                f"{URL}?campaign=Annadan",
                json={
                    "full_name": "Retry Donor", "phone": "9811100011", "pan": "ABCDE1234F",
                    "amount": "2100", "payment_status": "processing",
                    "payment_transaction_id": "pay_RetryTest1",
                },
                headers={"X-Zoho-Webhook-Token": TOKEN},
            )
        assert resp.status_code == 200
        assert mock_client.payment.fetch.call_count == 3
        assert Donation.query.count() == 1


class TestDonationCreation:
    def test_successful_payment_creates_a_donation_with_a_real_receipt(self, client, app):
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = _post(client, app)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["receipt_number"]

        d = Donation.query.one()
        assert d.status == "success"
        assert d.amount == 2100.0
        assert d.payment_mode == "online"
        assert d.razorpay_payment_id == "pay_ZohoTest001"
        assert d.receipt_number == body["receipt_number"]
        assert d.receipt_pdf

    def test_donor_is_created_from_submitted_fields(self, client, app):
        from models import Donor
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        _post(client, app, full_name="Radhika Devi", phone="9822233344", email="radhika@example.com")
        donor = Donor.query.filter_by(phone="9822233344").one()
        assert donor.full_name == "Radhika Devi"
        assert donor.email == "radhika@example.com"

    def test_zoho_combined_txn_and_order_id_string_is_parsed(self, client, app):
        """Zoho's own "Payment Transaction ID" field (confirmed from a live
        account's Reports grid) isn't a bare Razorpay payment ID -- it's a
        combined string like "Txn ID : pay_xxx Order ID : order_xxx". Both
        pieces must be pulled out correctly rather than the whole string
        landing in razorpay_payment_id (which would break idempotency
        matching against the site's own Razorpay flow's payment IDs, and
        against a redelivered Zoho webhook for the same payment)."""
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = _post(
            client, app,
            payment_transaction_id="Txn ID : pay_TWnsKWUifmlYnc Order ID : order_TWns42m6OljsvJ",
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["transaction_id"] == "pay_TWnsKWUifmlYnc"
        assert body["order_id"] == "order_TWns42m6OljsvJ"

        d = Donation.query.one()
        assert d.razorpay_payment_id == "pay_TWnsKWUifmlYnc"
        assert d.razorpay_order_id == "order_TWns42m6OljsvJ"

    def test_real_zoho_webhook_transaction_id_has_no_order_id_embedded(self, client, app):
        """Confirmed from a live production call: unlike the Reports grid
        (which shows "Txn ID : ... Order ID : ..."), the actual webhook
        payload for payment_transaction_id arrived as just the bare
        "pay_..." id. The donation must still be created correctly with
        no order ID rather than failing or mis-parsing."""
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = _post(client, app, payment_transaction_id="pay_TX10nlyMiM6Lga")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["transaction_id"] == "pay_TX10nlyMiM6Lga"
        assert body["order_id"] is None

        d = Donation.query.one()
        assert d.razorpay_payment_id == "pay_TX10nlyMiM6Lga"
        assert d.razorpay_order_id is None

    def test_separate_payment_order_id_parameter_is_picked_up(self, client, app):
        """If a form's Payload Parameters do map a separate Order ID field
        (payment_order_id), it's used since payment_transaction_id alone
        doesn't carry one in practice (see the test above)."""
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = _post(
            client, app, payment_transaction_id="pay_SeparateOrderTest",
            payment_order_id="order_SeparateOrderTest",
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["order_id"] == "order_SeparateOrderTest"
        assert Donation.query.one().razorpay_order_id == "order_SeparateOrderTest"

    def test_redelivery_of_the_combined_string_is_still_deduped(self, client, app):
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        first = _post(
            client, app, payment_transaction_id="Txn ID : pay_Redeliver1 Order ID : order_Redeliver1",
        )
        assert first.status_code == 200
        second = _post(
            client, app, payment_transaction_id="Txn ID : pay_Redeliver1 Order ID : order_Redeliver1",
            full_name="Someone Else",
        )
        assert second.get_json().get("skipped") == "already processed"
        assert Donation.query.count() == 1

    def test_duplicate_transaction_id_is_a_no_op(self, client, app):
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        first = _post(client, app, payment_transaction_id="pay_Dup1")
        assert first.status_code == 200
        second = _post(client, app, payment_transaction_id="pay_Dup1", full_name="Someone Else")
        assert second.status_code == 200
        assert second.get_json().get("skipped") == "already processed"
        assert Donation.query.count() == 1

    def test_invalid_amount_is_rejected(self, client, app):
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = _post(client, app, amount="not-a-number")
        assert resp.status_code == 400
        assert Donation.query.count() == 0

    def test_invalid_phone_is_rejected(self, client, app):
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = _post(client, app, phone="123")
        assert resp.status_code == 400
        assert Donation.query.count() == 0

    def test_invalid_pan_is_rejected(self, client, app):
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = _post(client, app, pan="NOTAPAN")
        assert resp.status_code == 400
        assert Donation.query.count() == 0

    def test_receipt_type_non80g_is_honoured(self, client, app):
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        _post(client, app, receipt_type="non80g", payment_transaction_id="pay_Non80g")
        assert Donation.query.one().effective_is_80g is False

    def test_receipt_type_80g_requires_pan(self, client, app):
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = _post(client, app, receipt_type="80g", pan="", payment_transaction_id="pay_80gNoPan")
        assert resp.status_code == 400
        assert Donation.query.count() == 0

    def test_receipt_type_80g_with_pan_succeeds(self, client, app):
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = _post(
            client, app, receipt_type="80g", pan="ABCDE1234F",
            payment_transaction_id="pay_80gWithPan",
        )
        assert resp.status_code == 200
        assert Donation.query.one().effective_is_80g is True

    def test_low_value_non_80g_donation_never_stores_a_spurious_pan(self, client, app):
        """Same REG-001 backstop as create_order() -- a PAN that arrived
        despite not being legally required (not 80G, not above the
        high-value threshold) must not be written to the donor's profile."""
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        _post(
            client, app, amount="500", pan="ABCDE1234F", receipt_type="non80g",
            payment_transaction_id="pay_SpuriousPan",
        )
        d = Donation.query.one()
        assert not d.donor.pan

    def test_activity_log_entry_is_written(self, client, app):
        from models import AdminActivityLog
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        _post(client, app, payment_transaction_id="pay_ActivityLog1")
        log = AdminActivityLog.query.filter_by(action="zoho_form_donation_received").first()
        assert log is not None
        assert log.admin_username == "system"
        assert "pay_ActivityLog1" in log.details
