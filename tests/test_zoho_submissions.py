"""Tests for the local mirror of Zoho's calls (ZohoSubmission).

Razorpay knows a payment was captured but records a phone number, not a
name. Zoho knows the name. This table is the app's own copy of Zoho's
side, kept the way the Google Sheet keeps it -- one row per call, nothing
thrown away -- so a receipt can be issued without publishing donor names
and phone numbers to a public link, configuring a spreadsheet per form, or
depending on Google being reachable when a receipt is due.

The distinction that matters, and the reason an earlier table was deleted
rather than reused: these rows never decide *whether* a receipt is owed.
Razorpay decides that. They only supply the name. Deciding entitlement
from a stored submission is what produced a case with no correct answer --
one donor with two submissions for the same amount is indistinguishable
from one submission paid for twice, and those need opposite handling.
Asking only for a name has no such problem, because both rows agree.
"""
import datetime
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

TOKEN = "test-zoho-token"
URL = "/internal/zoho-form-donation"


def _razorpay(payments=(), fetch_status="captured"):
    client = MagicMock()
    client.payment.all.return_value = {"items": list(payments), "count": len(payments)}
    client.payment.fetch.side_effect = lambda pid: {"id": pid, "status": fetch_status}
    return patch("razorpay.Client", return_value=client)


def _payment(payment_id, amount, contact, form="EBG", minutes_after=3):
    created = datetime.datetime.utcnow() + datetime.timedelta(minutes=minutes_after)
    return {
        "id": payment_id,
        "order_id": payment_id.replace("pay_", "order_"),
        "amount": int(round(amount * 100)),
        "status": "captured",
        "contact": contact,
        "email": None,
        "created_at": int(created.replace(tzinfo=datetime.timezone.utc).timestamp()),
        "notes": {"zform_custom": f"iskcondwarka,{form},tok"},
    }


def _call(client, app, campaign="BACE Contribution", **overrides):
    """One Zoho webhook call, as Zoho actually sends them."""
    app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
    app.config["RAZORPAY_ENABLED"] = True
    payload = {
        "full_name": "shivam raj",
        "phone": "+919319880507",
        "amount": "100",
        "payment_status": "Processing",
        "payment_transaction_id": "",
    }
    payload.update(overrides)
    return client.post(f"{URL}?campaign={campaign}", json=payload,
                       headers={"X-Zoho-Webhook-Token": TOKEN})


def _form(app, form_key="EBG", campaign_name="BACE Contribution", is_test=False):
    from extensions import db
    from models import Campaign, ZohoForm
    campaign = Campaign.query.filter_by(name=campaign_name).first()
    entry = ZohoForm(form_key=form_key, campaign_id=campaign.id if campaign else None,
                     is_test=is_test)
    db.session.add(entry)
    db.session.commit()
    app.config["RAZORPAY_ENABLED"] = True
    return entry


class TestEveryCallIsRecorded:
    def test_the_pre_payment_call_is_kept(self, client, app):
        """The call Zoho reliably sends, and the one it used to be
        pointless to receive."""
        from models import ZohoSubmission

        resp = _call(client, app)

        assert resp.status_code == 200
        assert "submission recorded" in resp.get_json()["acknowledged"]
        row = ZohoSubmission.query.one()
        assert row.full_name == "shivam raj"
        assert row.phone_normalized == "9319880507"
        assert row.amount == 100
        assert row.transaction_id is None
        assert row.payment_status == "Processing"

    def test_a_call_carrying_a_transaction_id_is_kept_too(self, client, app):
        from models import ZohoSubmission

        with _razorpay():
            _call(client, app, payment_transaction_id="pay_WithId1",
                  payment_status="Completed")

        assert ZohoSubmission.query.one().transaction_id == "pay_WithId1"

    def test_a_failed_payment_is_still_recorded(self, client, app):
        """Knowing a payment failed means never chasing it."""
        from models import Donation, ZohoSubmission

        with _razorpay(fetch_status="failed"):
            _call(client, app, payment_transaction_id="pay_Failed1",
                  payment_status="Failed")

        assert Donation.query.count() == 0, "a failed payment gets no receipt"
        row = ZohoSubmission.query.one()
        assert row.transaction_id == "pay_Failed1"
        assert row.payment_status == "Failed"

    def test_repeat_calls_are_all_kept_not_deduplicated(self, client, app):
        """This is a log, not a work queue. Zoho retries, and donors
        submit twice -- both are recorded as they arrive."""
        from models import ZohoSubmission

        _call(client, app)
        _call(client, app)
        _call(client, app)

        assert ZohoSubmission.query.count() == 3

    def test_the_whole_payload_is_kept_for_later(self, client, app):
        import json
        from models import ZohoSubmission

        _call(client, app, email="shivam@example.com", age="27",
              some_form_specific_field="whatever")

        stored = json.loads(ZohoSubmission.query.one().payload_json)
        assert stored["email"] == "shivam@example.com"
        assert stored["some_form_specific_field"] == "whatever"

    def test_a_spurious_pan_is_not_stored(self, client, app):
        """REG-001: a PAN on a Non-80G donation below the high-value
        threshold isn't required, so it must not be persisted. Without
        this rule here, the table would be a way around it -- and these
        registration forms are exactly the case it exists for."""
        import json
        from models import ZohoSubmission

        _call(client, app, pan="ABCDE1234F")

        assert json.loads(ZohoSubmission.query.one().payload_json)["pan"] == ""

    def test_a_pan_that_is_needed_is_kept(self, client, app):
        import json
        from models import ZohoSubmission

        _call(client, app, campaign="Annadan", pan="ABCDE1234F")  # Annadan is 80G

        assert json.loads(ZohoSubmission.query.one().payload_json)["pan"] == "ABCDE1234F"

    def test_a_recording_failure_never_costs_the_receipt(self, client, app):
        """A missing log row is worth less than a missing receipt."""
        import public
        from models import Donation

        with _razorpay(), patch.object(
            public, "_record_zoho_submission", side_effect=RuntimeError("db hiccup")
        ):
            resp = _call(client, app, payment_transaction_id="pay_StillWorks1",
                         payment_status="Completed")

        assert resp.status_code == 200
        assert Donation.query.one().razorpay_payment_id == "pay_StillWorks1"


class TestNamingAPaymentFromTheRecords:
    """Matched on the transaction ID and nothing else.

    Earlier versions fell back to phone and amount when Zoho hadn't sent
    an ID, and that is where every hard case came from. A guess that looks
    like a match is worse than no match: it produces a tax receipt in the
    wrong name and nobody notices."""

    def _reconcile(self, app, payments):
        from public import reconcile_zoho_submissions
        with _razorpay(payments):
            return reconcile_zoho_submissions(app.config)

    def _recorded_with_id(self, client, app, txn, **overrides):
        """A Zoho call carrying a transaction ID, recorded but not
        receipted -- Razorpay wasn't confirming it at the time. The
        realistic way a payment reaches reconciliation with a record
        already waiting for it."""
        with _razorpay(fetch_status="authorized"):
            _call(client, app, payment_transaction_id=txn,
                  payment_status="Completed", **overrides)

    def test_a_recorded_transaction_id_becomes_a_receipt(self, client, app):
        from models import Donation

        _form(app)
        self._recorded_with_id(client, app, "pay_Rec1", full_name="shivam raj")

        summary = self._reconcile(app, [_payment("pay_Rec1", 100, "+919319880507")])

        assert len(summary["created"]) == 1
        donation = Donation.query.one()
        assert donation.donor.full_name == "shivam raj"
        assert donation.razorpay_payment_id == "pay_Rec1"
        assert donation.receipt_number

    def test_the_donors_own_phone_is_used_not_razorpays_contact(self, client, app):
        """Production has seen the two differ -- Mohit Rewal paid from a
        different number than his form carried. The form's number is what
        the donor gave the temple, and what Zoho validated."""
        from models import Donation

        _form(app)
        self._recorded_with_id(client, app, "pay_Phone1", phone="+919319880507")

        self._reconcile(app, [_payment("pay_Phone1", 100, "+918888888888")])

        assert Donation.query.one().donor.phone == "9319880507"

    def test_nothing_is_matched_without_a_transaction_id(self, client, app):
        """The pre-payment call is recorded, but it names nobody -- there
        is no id to match it on, and phone and amount are not consulted."""
        from models import Donation

        _form(app)
        _call(client, app)   # no transaction id

        summary = self._reconcile(app, [_payment("pay_NoId1", 100, "+919319880507")])

        assert Donation.query.count() == 0
        reason = summary["orphan_payments"][0]["why_not_receipted"]
        assert "no recorded Zoho submission carries this transaction id" in reason
        assert "webhook is configured" in reason

    def test_two_donations_from_one_donor_each_get_their_own_receipt(self, client, app):
        """The case that had no answer under phone matching: same donor,
        same amount, twice. Two transaction ids, so two exact matches and
        nothing to decide."""
        from models import Donation

        _form(app)
        self._recorded_with_id(client, app, "pay_Jatin1", full_name="Jatin Saini",
                               phone="+919650150283")
        self._recorded_with_id(client, app, "pay_Jatin2", full_name="Jatin Saini",
                               phone="+919650150283")

        summary = self._reconcile(app, [
            _payment("pay_Jatin1", 100, "+919650150283"),
            _payment("pay_Jatin2", 100, "+919650150283"),
        ])

        assert len(summary["created"]) == 2
        assert Donation.query.count() == 2
        assert {d.razorpay_payment_id for d in Donation.query.all()} == {"pay_Jatin1", "pay_Jatin2"}

    def test_a_shared_phone_is_no_longer_a_problem(self, client, app):
        """Two people on one family phone. Under phone matching this was
        unresolvable; each transaction id names its own donor."""
        from models import Donation

        _form(app)
        self._recorded_with_id(client, app, "pay_Fam1", full_name="First Person",
                               phone="+919319880507")
        self._recorded_with_id(client, app, "pay_Fam2", full_name="Second Person",
                               phone="+919319880507")

        self._reconcile(app, [
            _payment("pay_Fam1", 100, "+919319880507"),
            _payment("pay_Fam2", 100, "+919319880507"),
        ])

        names = {d.donor.full_name for d in Donation.query.all()}
        assert names == {"First Person", "Second Person"}

    def test_one_transaction_id_under_two_names_is_refused(self, client, app):
        """Should be impossible. If it happens something is wrong upstream
        and a person should look, rather than one name being picked."""
        from models import Donation

        _form(app)
        self._recorded_with_id(client, app, "pay_Conflict1", full_name="One Name")
        self._recorded_with_id(client, app, "pay_Conflict1", full_name="Another Name")

        summary = self._reconcile(app, [_payment("pay_Conflict1", 100, "+919319880507")])

        assert Donation.query.count() == 0
        assert "different names" in summary["orphan_payments"][0]["why_not_receipted"]

    def test_a_payment_with_no_record_at_all_is_reported(self, client, app):
        from models import Donation

        _form(app)
        summary = self._reconcile(app, [_payment("pay_Unknown1", 100, "+919999999999")])

        assert Donation.query.count() == 0
        assert "no recorded Zoho submission" in summary["orphan_payments"][0]["why_not_receipted"]

    def test_a_form_with_no_campaign_is_reported_not_guessed(self, client, app):
        from models import Donation

        _form(app, campaign_name="__none__")   # resolves to campaign_id None
        self._recorded_with_id(client, app, "pay_NoCamp1")

        summary = self._reconcile(app, [_payment("pay_NoCamp1", 100, "+919319880507")])

        assert Donation.query.count() == 0
        assert "no campaign set" in summary["orphan_payments"][0]["why_not_receipted"]

    def test_a_test_form_is_never_receipted(self, client, app):
        from models import Donation

        _form(app, is_test=True)
        self._recorded_with_id(client, app, "pay_TestForm4")

        self._reconcile(app, [_payment("pay_TestForm4", 100, "+919319880507")])

        assert Donation.query.count() == 0

    def test_an_already_receipted_payment_is_not_receipted_again(self, client, app):
        from models import Donation

        _form(app)
        self._recorded_with_id(client, app, "pay_Once9")
        payment = _payment("pay_Once9", 100, "+919319880507")

        self._reconcile(app, [payment])
        self._reconcile(app, [payment])

        assert Donation.query.count() == 1

    def test_an_uncaptured_payment_is_never_receipted(self, client, app):
        """Razorpay decides. A recorded submission does not make a payment
        real."""
        from models import Donation
        from public import reconcile_zoho_submissions

        _form(app)
        self._recorded_with_id(client, app, "pay_NotCaptured1")

        with _razorpay([_payment("pay_NotCaptured1", 100, "+919319880507")],
                       fetch_status="authorized"):
            reconcile_zoho_submissions(app.config)

        assert Donation.query.count() == 0


class TestRetention:
    def test_records_older_than_the_window_are_deleted(self, client, app):
        """These hold names and phone numbers, including for people who
        never paid. A month is far longer than any reconciliation looks
        back, so nothing useful is lost."""
        from extensions import db
        from models import ZohoSubmission
        from public import prune_zoho_submissions

        _call(client, app)
        row = ZohoSubmission.query.one()
        row.received_at = datetime.datetime.utcnow() - datetime.timedelta(days=40)
        db.session.commit()

        assert prune_zoho_submissions(retain_days=30) == 1
        assert ZohoSubmission.query.count() == 0

    def test_recent_records_are_kept(self, client, app):
        from models import ZohoSubmission
        from public import prune_zoho_submissions

        _call(client, app)
        assert prune_zoho_submissions(retain_days=30) == 0
        assert ZohoSubmission.query.count() == 1

    def test_reconciliation_prunes_as_it_goes(self, client, app):
        """So nobody has to remember to run it."""
        from extensions import db
        from models import ZohoSubmission
        from public import reconcile_zoho_submissions

        _call(client, app)
        row = ZohoSubmission.query.one()
        row.received_at = datetime.datetime.utcnow() - datetime.timedelta(days=90)
        db.session.commit()

        app.config["RAZORPAY_ENABLED"] = True
        with _razorpay([]):
            summary = reconcile_zoho_submissions(app.config)

        assert summary["pruned"] == 1
        assert ZohoSubmission.query.count() == 0


class TestTheWholeJourneyAsProductionRunsIt:
    """Every test above seeds the record through the webhook or directly,
    then matches on a bare "pay_..." id. Production doesn't always send a
    bare id: on several of these forms the Payment Transaction ID field
    renders as "Txn ID : pay_X Order ID : order_Y". If the webhook stored
    that whole string, every other test here would still pass and
    reconciliation would match nothing at all -- the exact shape of
    failure this feature exists to end.

    So: post what Zoho posts, then reconcile what Razorpay reports, and
    check a receipt comes out."""

    def test_a_raw_zoho_transaction_string_still_matches_its_payment(self, client, app):
        from models import Donation, ZohoSubmission
        from public import reconcile_zoho_submissions

        _form(app)
        # Zoho's call arrives while the payment is still authorized, so
        # the webhook records it and issues nothing.
        with _razorpay(fetch_status="authorized"):
            resp = _call(
                client, app, full_name="Jatin Saini", phone="9650150283",
                payment_status="Completed",
                payment_transaction_id=(
                    "Txn ID : pay_TY1ckLpy6lUDMr Order ID : order_TY1bS1x9eSe5tb"
                ),
            )
        assert resp.status_code == 200
        assert Donation.query.count() == 0

        # Stored as the bare id -- what Razorpay will report.
        assert ZohoSubmission.query.one().transaction_id == "pay_TY1ckLpy6lUDMr"

        # Razorpay captures it later; reconciliation issues the receipt.
        with _razorpay([_payment("pay_TY1ckLpy6lUDMr", 100, "9650150283")]):
            summary = reconcile_zoho_submissions(app.config)

        assert len(summary["created"]) == 1
        donation = Donation.query.one()
        assert donation.donor.full_name == "Jatin Saini"
        assert donation.razorpay_payment_id == "pay_TY1ckLpy6lUDMr"
        assert donation.receipt_number

    def test_the_pre_payment_call_alone_never_produces_a_receipt(self, client, app):
        """The call Zoho reliably makes carries no transaction ID. It is
        recorded and nothing more -- no receipt is owed until Razorpay
        says money arrived."""
        from models import Donation, ZohoSubmission
        from public import reconcile_zoho_submissions

        _form(app)
        _call(client, app, full_name="Nandani Kumari", phone="9319880507")

        assert ZohoSubmission.query.one().transaction_id is None

        # A captured payment exists, but nothing ties it to that record.
        with _razorpay([_payment("pay_NoLink1", 100, "9319880507")]):
            summary = reconcile_zoho_submissions(app.config)

        assert Donation.query.count() == 0
        assert summary["created"] == []
        left = summary["orphan_payments"]
        assert len(left) == 1
        assert "webhook is configured" in left[0]["why_not_receipted"]
