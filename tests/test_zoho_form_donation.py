"""Tests for the Zoho Forms webhook (POST /internal/zoho-form-donation).

This route is deliberately small now, and it used to be several hundred
lines plus a database table. What it no longer tries to do is the point.

Zoho fires it once, at submission time -- before the donor pays -- so most
calls carry the form and no transaction ID. The old design stored those
and later tried to work out which Razorpay payment belonged to which
stored submission, from phone, amount and a time window. That guessing had
a case with no correct answer: one donor, two submissions for the same
amount, two payments is indistinguishable from one submission paid for
twice, and those need opposite handling.

None of it is needed. Razorpay knows which payments were captured and have
no donation behind them, and this app's own record of Zoho's calls supplies
the donor's name (see test_zoho_submissions). So a call without a
transaction ID is now simply recorded and acknowledged -- there is a
complete, unambiguous path that picks that donation up regardless.

What remains is the one case this route can settle by itself: when Zoho
*does* send the transaction ID, the receipt is issued immediately rather
than at the next sweep. That happened for real throughout September, for
EBG_Registration and Bhakti_Vriksha.
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

TOKEN = "test-zoho-token"
URL = "/internal/zoho-form-donation"


def _razorpay(status="captured"):
    client = MagicMock()
    client.payment.fetch.side_effect = lambda pid: {"id": pid, "status": status}
    return patch("razorpay.Client", return_value=client)


def _post(client, app, campaign="BACE Contribution", token=TOKEN, **overrides):
    app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
    app.config["RAZORPAY_ENABLED"] = True
    payload = {
        "full_name": "Zoho Donor",
        "phone": "9811100011",
        "amount": "100",
        "payment_status": "Completed",
        "payment_transaction_id": "pay_ZohoTest001",
    }
    payload.update(overrides)
    headers = {"X-Zoho-Webhook-Token": token} if token else {}
    return client.post(f"{URL}?campaign={campaign}", json=payload, headers=headers)


class TestAuth:
    def test_a_wrong_token_is_rejected(self, client, app):
        assert _post(client, app, token="nope").status_code == 401

    def test_a_missing_token_is_rejected(self, client, app):
        assert _post(client, app, token=None).status_code == 401

    def test_it_refuses_to_run_when_no_token_is_configured(self, client, app):
        """Never accept an unauthenticated call just because the server
        forgot its own secret."""
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = ""
        resp = client.post(f"{URL}?campaign=BACE Contribution", json={},
                           headers={"X-Zoho-Webhook-Token": "anything"})
        assert resp.status_code == 503


class TestCampaign:
    def test_a_missing_campaign_parameter_is_rejected(self, client, app):
        app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
        resp = client.post(URL, json={}, headers={"X-Zoho-Webhook-Token": TOKEN})
        assert resp.status_code == 400

    def test_an_unknown_campaign_is_rejected_not_guessed(self, client, app):
        """Filing a donation under the wrong campaign quietly corrupts
        every figure this app reports on."""
        from models import Donation
        resp = _post(client, app, campaign="No Such Campaign")
        assert resp.status_code == 400
        assert Donation.query.count() == 0

    def test_campaign_matching_ignores_case(self, client, app):
        from models import Donation
        with _razorpay():
            resp = _post(client, app, campaign="bace contribution")
        assert resp.status_code == 200
        assert Donation.query.count() == 1


class TestTheCallWithoutATransactionId:
    """Zoho's ordinary pre-payment call: the whole form, no payment yet.
    Recorded rather than discarded -- that record is what lets
    reconciliation name the donor once Razorpay reports the payment."""

    def test_it_is_acknowledged_and_recorded_but_creates_no_donation(self, client, app):
        """No longer a dead end: the call is kept as this app's own copy
        of Zoho's record, which is what lets reconciliation name the donor
        once Razorpay reports the payment. See test_zoho_submissions."""
        from models import Donation, ZohoSubmission
        resp = _post(client, app, payment_transaction_id="", payment_status="processing")
        assert resp.status_code == 200
        assert "submission recorded" in resp.get_json()["acknowledged"]
        assert Donation.query.count() == 0
        assert ZohoSubmission.query.count() == 1

    def test_a_completed_label_with_no_id_is_also_just_acknowledged(self, client, app):
        """Zoho's own status is not consulted at all any more -- it has
        been wrong in both directions, and the presence of a transaction ID
        is the only part of the call worth reading."""
        from models import Donation
        resp = _post(client, app, payment_transaction_id="", payment_status="Completed")
        assert resp.status_code == 200
        assert Donation.query.count() == 0


class TestTheCallWithATransactionId:
    def test_a_confirmed_payment_becomes_a_donation_with_a_receipt(self, client, app):
        from models import Donation

        with _razorpay():
            resp = _post(client, app)

        assert resp.status_code == 200
        donation = Donation.query.one()
        assert donation.status == "success"
        assert donation.razorpay_payment_id == "pay_ZohoTest001"
        assert donation.receipt_number
        assert float(donation.amount) == 100.0

    def test_the_id_is_found_inside_zohos_combined_string(self, client, app):
        """Production renders it as "Txn ID : pay_X Order ID : order_Y" on
        some forms and a bare id on others."""
        from models import Donation

        with _razorpay():
            _post(client, app, payment_transaction_id=(
                "Txn ID : pay_TY1ckLpy6lUDMr Order ID : order_TY1bS1x9eSe5tb"
            ))

        donation = Donation.query.one()
        assert donation.razorpay_payment_id == "pay_TY1ckLpy6lUDMr"
        assert donation.razorpay_order_id == "order_TY1bS1x9eSe5tb"

    def test_the_id_is_found_even_in_a_differently_named_field(self, client, app):
        """Scanned across the payload rather than one named field, so a
        form that labels it differently can't silently import nothing."""
        from models import Donation

        with _razorpay():
            _post(client, app, payment_transaction_id="",
                  some_other_label="ref pay_Relabelled7")

        assert Donation.query.one().razorpay_payment_id == "pay_Relabelled7"

    def test_razorpay_decides_not_zoho(self, client, app):
        """Zoho says Completed; Razorpay says otherwise. Razorpay wins."""
        from models import Donation

        with _razorpay(status="authorized"):
            resp = _post(client, app)

        assert resp.status_code == 200
        assert resp.get_json()["skipped"] == "payment not captured"
        assert Donation.query.count() == 0

    def test_an_unreachable_razorpay_returns_502_so_zoho_can_re_push(self, client, app):
        """A non-2xx lands the call in that form's Failed Entries, where it
        can be re-pushed by hand. Reconciliation would catch it within the
        hour regardless, but a silent 200 wastes that signal."""
        from models import Donation

        client_mock = MagicMock()
        client_mock.payment.fetch.side_effect = RuntimeError("connection reset")
        with patch("razorpay.Client", return_value=client_mock), \
             patch("utils.time.sleep", lambda s: None):
            resp = _post(client, app)

        assert resp.status_code == 502
        assert Donation.query.count() == 0


class TestIdempotency:
    def test_a_redelivered_call_does_not_create_a_second_donation(self, client, app):
        from models import Donation

        with _razorpay():
            _post(client, app)
            resp = _post(client, app)

        assert resp.get_json()["skipped"] == "already recorded"
        assert Donation.query.count() == 1

    def test_a_payment_backfilled_by_hand_is_recognised(self, client, app):
        """September's 21 backfills carry their reference in
        bank_transaction_id. Matching only razorpay_payment_id would
        re-create every one of them."""
        from extensions import db
        from models import Campaign, Donation, Donor

        donor = Donor(full_name="Already There", phone="9811100011")
        db.session.add(donor)
        db.session.flush()
        db.session.add(Donation(
            donor_id=donor.id, campaign_id=Campaign.query.first().id, amount=100,
            status="success", payment_mode="online",
            bank_transaction_id="pay_ZohoTest001",
        ))
        db.session.commit()

        with _razorpay():
            resp = _post(client, app)

        assert resp.get_json()["skipped"] == "already recorded"
        assert Donation.query.count() == 1


class TestValidation:
    def test_an_80g_campaign_without_a_pan_is_refused(self, client, app):
        """Enforced by the shared _create_zoho_donation, so this path
        cannot drift from the reconciler or the report importer."""
        from models import Donation

        with _razorpay():
            resp = _post(client, app, campaign="Annadan")  # Annadan is 80G

        assert resp.status_code == 400
        assert b"PAN" in resp.data
        assert Donation.query.count() == 0

    def test_an_invalid_amount_is_refused(self, client, app):
        from models import Donation
        with _razorpay():
            resp = _post(client, app, amount="not a number")
        assert resp.status_code == 400
        assert Donation.query.count() == 0

    def test_the_activity_log_records_what_arrived(self, client, app):
        from models import AdminActivityLog

        with _razorpay():
            _post(client, app)

        entry = AdminActivityLog.query.filter_by(action="zoho_form_donation_received").one()
        assert "pay_ZohoTest001" in entry.details
        assert "BACE Contribution" in entry.details
