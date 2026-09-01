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
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

TOKEN = "test-zoho-token"
URL = "/internal/zoho-form-donation"


def _post(client, campaign="Annadan", headers=True, **overrides):
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
    return client.post(f"{URL}?campaign={campaign}", json=payload, headers=hdrs)


class TestAuth:
    def test_returns_503_when_not_configured(self, client, app):
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = ""
        resp = _post(client)
        assert resp.status_code == 503

    def test_rejects_missing_token(self, client, app):
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = _post(client, headers=False)
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
        resp = _post(client, campaign="Does Not Exist")
        assert resp.status_code == 400
        assert Donation.query.count() == 0

    def test_campaign_match_is_case_insensitive(self, client, app):
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = _post(client, campaign="annadan")
        assert resp.status_code == 200
        assert Donation.query.count() == 1


class TestPaymentStatusGate:
    def test_pending_status_is_acknowledged_but_creates_nothing(self, client, app):
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = _post(client, payment_status="pending")
        assert resp.status_code == 200
        assert resp.get_json()["skipped"] == "payment not completed"
        assert Donation.query.count() == 0

    def test_failed_status_creates_nothing(self, client, app):
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = _post(client, payment_status="failed")
        assert resp.status_code == 200
        assert Donation.query.count() == 0

    def test_real_zoho_processing_status_creates_nothing(self, client, app):
        """Exact values confirmed from a live Zoho Forms account's Reports
        grid: "Processing" is the early async call (see the route's
        docstring) before the gateway responds -- must never create a
        donation on its own."""
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = _post(client, payment_status="Processing")
        assert resp.status_code == 200
        assert Donation.query.count() == 0

    def test_real_zoho_processing_not_needed_status_creates_nothing(self, client, app):
        """"Processing not needed" is Zoho's value for a submission that
        never actually triggered the payment field at all -- also not a
        donation."""
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = _post(client, payment_status="Processing not needed")
        assert resp.status_code == 200
        assert Donation.query.count() == 0

    def test_real_zoho_completed_status_is_accepted(self, client, app):
        """The one confirmed real success value -- exact casing as shown
        on a live account (this route lowercases before comparing)."""
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = _post(client, payment_status="Completed")
        assert resp.status_code == 200
        assert Donation.query.count() == 1

    def test_missing_transaction_id_on_a_success_is_rejected(self, client, app):
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = _post(client, payment_transaction_id="")
        assert resp.status_code == 400
        assert Donation.query.count() == 0


class TestDonationCreation:
    def test_successful_payment_creates_a_donation_with_a_real_receipt(self, client, app):
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = _post(client)
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
        _post(client, full_name="Radhika Devi", phone="9822233344", email="radhika@example.com")
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
            client,
            payment_transaction_id="Txn ID : pay_TWnsKWUifmlYnc Order ID : order_TWns42m6OljsvJ",
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["transaction_id"] == "pay_TWnsKWUifmlYnc"
        assert body["order_id"] == "order_TWns42m6OljsvJ"

        d = Donation.query.one()
        assert d.razorpay_payment_id == "pay_TWnsKWUifmlYnc"
        assert d.razorpay_order_id == "order_TWns42m6OljsvJ"

    def test_redelivery_of_the_combined_string_is_still_deduped(self, client, app):
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        first = _post(
            client, payment_transaction_id="Txn ID : pay_Redeliver1 Order ID : order_Redeliver1",
        )
        assert first.status_code == 200
        second = _post(
            client, payment_transaction_id="Txn ID : pay_Redeliver1 Order ID : order_Redeliver1",
            full_name="Someone Else",
        )
        assert second.get_json().get("skipped") == "already processed"
        assert Donation.query.count() == 1

    def test_duplicate_transaction_id_is_a_no_op(self, client, app):
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        first = _post(client, payment_transaction_id="pay_Dup1")
        assert first.status_code == 200
        second = _post(client, payment_transaction_id="pay_Dup1", full_name="Someone Else")
        assert second.status_code == 200
        assert second.get_json().get("skipped") == "already processed"
        assert Donation.query.count() == 1

    def test_invalid_amount_is_rejected(self, client, app):
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = _post(client, amount="not-a-number")
        assert resp.status_code == 400
        assert Donation.query.count() == 0

    def test_invalid_phone_is_rejected(self, client, app):
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = _post(client, phone="123")
        assert resp.status_code == 400
        assert Donation.query.count() == 0

    def test_invalid_pan_is_rejected(self, client, app):
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = _post(client, pan="NOTAPAN")
        assert resp.status_code == 400
        assert Donation.query.count() == 0

    def test_receipt_type_non80g_is_honoured(self, client, app):
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        _post(client, receipt_type="non80g", payment_transaction_id="pay_Non80g")
        assert Donation.query.one().effective_is_80g is False

    def test_receipt_type_80g_requires_pan(self, client, app):
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = _post(client, receipt_type="80g", pan="", payment_transaction_id="pay_80gNoPan")
        assert resp.status_code == 400
        assert Donation.query.count() == 0

    def test_receipt_type_80g_with_pan_succeeds(self, client, app):
        from models import Donation
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = _post(
            client, receipt_type="80g", pan="ABCDE1234F",
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
            client, amount="500", pan="ABCDE1234F", receipt_type="non80g",
            payment_transaction_id="pay_SpuriousPan",
        )
        d = Donation.query.one()
        assert not d.donor.pan

    def test_activity_log_entry_is_written(self, client, app):
        from models import AdminActivityLog
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        _post(client, payment_transaction_id="pay_ActivityLog1")
        log = AdminActivityLog.query.filter_by(action="zoho_form_donation_received").first()
        assert log is not None
        assert log.admin_username == "system"
        assert "pay_ActivityLog1" in log.details
