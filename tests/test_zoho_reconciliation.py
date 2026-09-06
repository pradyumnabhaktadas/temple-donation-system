"""Tests for the Zoho Forms payment reconciliation
(public.reconcile_zoho_submissions + PendingZohoSubmission).

WHY THIS EXISTS -- the production failure, in full:

Zoho Forms fires its webhook once, at form-submission time, *before* the
donor has paid. That call carries the entire form (name, phone, amount)
but no transaction ID, because no payment exists yet. Zoho's documentation
says a second call follows with the final payment result once the gateway
responds. On this live account it repeatedly did not:

  - "Me, You & The Ego" seminar, 03-Sep-2026 18:14, Rs. 100, Nandani
    Kumari. Zoho's own record: Payment Status "Completed", Payment
    Transaction ID "pay_TXZJ0OU6EtNogX". This app's record of the only
    call it ever received: {"skipped":"no payment transaction id yet"}.
  - "Essence of Bhagavad Gita", 04-Sep-2026 21:55, Rs. 100, shivam raj.
    Zoho: Completed, "pay_TY1ckLpy6lUDMr". Us: the same skip.

In both cases the donor paid, Razorpay captured the money, and this app
never heard another word about it -- no donor record, no donation, no
receipt, and nothing anywhere in the app to indicate anything was missing.
They were found only because someone noticed a stale "Webhook Status" cell
in Zoho's Reports grid days later.

Earlier fixes to the webhook route itself could not have caught these:
they made the route smarter about calls that arrive *carrying* a
transaction ID, and here no such call ever arrives. So the fix under test
is a different shape -- stop depending on Zoho's second call at all. The
first call's donor details are kept (PendingZohoSubmission), and a
scheduled job matches them against the payments Razorpay itself confirms
it captured, then issues the receipt.

TestTheProductionFailures below replays both real cases end to end.
"""
import datetime
import os
import sys
import time
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

TOKEN = "test-zoho-token"
URL = "/internal/zoho-form-donation"


def _razorpay(payments, fetch_status="captured", all_side_effect=None):
    """Stands in for the Razorpay SDK for both calls reconciliation makes:
    payment.all() (the scan for captured payments) and payment.fetch()
    (the per-match re-confirmation before a receipt is written).

    `payments` is a list of dicts in Razorpay's own shape -- amount in
    paise, created_at as a unix timestamp -- so tests exercise the same
    parsing production does rather than a pre-digested version of it."""
    client = MagicMock()
    if all_side_effect is not None:
        client.payment.all.side_effect = all_side_effect
    else:
        client.payment.all.return_value = {"items": payments, "count": len(payments)}
    client.payment.fetch.side_effect = lambda pid: {"id": pid, "status": fetch_status}
    return patch("razorpay.Client", return_value=client)


def _payment(payment_id, amount_rupees, contact, minutes_after_submission=3,
             submitted_at=None, status="captured", order_id=None):
    """One captured payment as Razorpay reports it."""
    submitted_at = submitted_at or datetime.datetime.utcnow()
    created = submitted_at + datetime.timedelta(minutes=minutes_after_submission)
    return {
        "id": payment_id,
        "order_id": order_id or payment_id.replace("pay_", "order_"),
        "amount": int(round(amount_rupees * 100)),
        "status": status,
        "contact": contact,
        "email": "donor@example.com",
        "created_at": int(created.replace(tzinfo=datetime.timezone.utc).timestamp()),
        "notes": {},
    }


def _submit_form(client, app, campaign="BACE Contribution", **overrides):
    """The call Zoho actually sends: full form, no transaction ID.

    This is the *only* call that arrived for either production failure, so
    every test here starts from it rather than from a hand-built
    PendingZohoSubmission row -- the record under test has to be the one
    the real webhook actually writes.

    Defaults to a Non-80G campaign because that's what these forms
    actually are: both real failures came through event-registration forms
    ("EBG_Registration (Non-80G)", the "Me, You & The Ego" seminar), which
    collect a fee and no PAN. See TestItRefusesToGuess's 80G test for the
    other case."""
    app.config["ZOHO_FORMS_WEBHOOK_TOKEN"] = TOKEN
    app.config["RAZORPAY_ENABLED"] = True
    payload = {
        "full_name": "Zoho Donor",
        "phone": "9811100011",
        "amount": "100",
        "payment_status": "processing",
        "payment_transaction_id": "",
    }
    payload.update(overrides)
    return client.post(
        f"{URL}?campaign={campaign}", json=payload,
        headers={"X-Zoho-Webhook-Token": TOKEN},
    )


def _reconcile(app, payments, **kwargs):
    from public import reconcile_zoho_submissions
    with _razorpay(payments, fetch_status=kwargs.pop("fetch_status", "captured")):
        return reconcile_zoho_submissions(app.config, min_age_minutes=0, **kwargs)


def _age_submissions(minutes):
    """Backdates every open pending submission, so a test can exercise the
    "old enough to act on" / "old enough to give up on" branches without
    sleeping."""
    from extensions import db
    from models import PendingZohoSubmission
    for s in PendingZohoSubmission.query.all():
        s.received_at = s.received_at - datetime.timedelta(minutes=minutes)
    db.session.commit()


class TestTheEarlyCallIsKeptNotDiscarded:
    """The first half of the fix: Zoho's pre-payment call must stop being
    a dead end. Before this, it returned {"skipped": ...} and the donor's
    details went nowhere."""

    def test_a_call_with_no_transaction_id_is_recorded(self, client, app):
        from models import PendingZohoSubmission

        resp = _submit_form(client, app, full_name="Nandani Kumari", phone="9625901202")

        assert resp.status_code == 200
        assert resp.get_json()["pending"] == "awaiting payment confirmation"

        pending = PendingZohoSubmission.query.one()
        assert pending.full_name == "Nandani Kumari"
        assert pending.phone_normalized == "9625901202"
        assert pending.amount == 100
        assert pending.resolved_at is None

    def test_the_full_payload_is_kept_verbatim_for_replay(self, client, app):
        """Reconciliation creates the donation by replaying this payload
        through the same code the webhook uses, so anything the donor told
        us has to survive the round trip -- not just the columns matching
        happens to need."""
        import json
        from models import PendingZohoSubmission

        _submit_form(
            client, app, full_name="Nandani Kumari", phone="9625901202",
            email="nandani@example.com", address="42 Dwarka", city="New Delhi",
        )

        payload = json.loads(PendingZohoSubmission.query.one().payload_json)
        assert payload["email"] == "nandani@example.com"
        assert payload["address"] == "42 Dwarka"
        assert payload["city"] == "New Delhi"

    def test_a_repeated_early_call_does_not_create_a_second_record(self, client, app):
        """Zoho retries its own calls, and a donor can retry a failed
        payment on the same form. Two pending rows for one real submission
        would later look like two people owed a receipt -- which
        reconciliation would then refuse to resolve as ambiguous, turning
        a recoverable case into a manual one for no reason."""
        from models import PendingZohoSubmission

        _submit_form(client, app, full_name="Nandani Kumari", phone="9625901202")
        _submit_form(client, app, full_name="Nandani Kumari", phone="9625901202")

        assert PendingZohoSubmission.query.count() == 1

    def test_a_completed_label_with_no_id_is_still_an_error_not_a_pending_row(self, client, app):
        """Unchanged behaviour, kept deliberately: "Completed" with no
        transaction ID at all is anomalous rather than the ordinary
        pre-payment call, and should stay loud."""
        from models import PendingZohoSubmission

        resp = _submit_form(client, app, payment_status="Completed")

        assert resp.status_code == 400
        assert PendingZohoSubmission.query.count() == 0


class TestTheProductionFailures:
    """Both real incidents, replayed end to end: the single early call
    Zoho sent, no follow-up call ever, and a captured payment sitting in
    Razorpay that nothing in this app knew about."""

    def test_nandani_kumari_gets_her_receipt(self, client, app):
        """03-Sep-2026 18:14, Rs. 100, "Me, You & The Ego" seminar.
        Zoho: Completed / pay_TXZJ0OU6EtNogX. This app, before the fix:
        nothing at all."""
        from models import Donation, PendingZohoSubmission

        _submit_form(client, app, full_name="Nandani Kumari", phone="9625901202", amount="100")
        submitted_at = PendingZohoSubmission.query.one().received_at

        # Zoho never calls again. The money is in Razorpay regardless.
        summary = _reconcile(app, [
            _payment("pay_TXZJ0OU6EtNogX", 100, "+919625901202", submitted_at=submitted_at),
        ])

        assert len(summary["created"]) == 1
        donation = Donation.query.one()
        assert donation.status == "success"
        assert donation.amount == 100
        assert donation.razorpay_payment_id == "pay_TXZJ0OU6EtNogX"
        assert donation.receipt_number, "a real receipt number must be issued"
        assert donation.donor.full_name == "Nandani Kumari"

        pending = PendingZohoSubmission.query.one()
        assert pending.resolution == "reconciled"
        assert pending.donation_id == donation.id

    def test_shivam_raj_gets_his_receipt(self, client, app):
        """04-Sep-2026 21:55, Rs. 100, Essence of Bhagavad Gita.
        Zoho: Completed / pay_TY1ckLpy6lUDMr. Same silent loss."""
        from models import Donation, PendingZohoSubmission

        _submit_form(client, app, full_name="shivam raj", phone="+919319880507", amount="100")
        submitted_at = PendingZohoSubmission.query.one().received_at

        summary = _reconcile(app, [
            _payment("pay_TY1ckLpy6lUDMr", 100, "9319880507", submitted_at=submitted_at),
        ])

        assert len(summary["created"]) == 1
        donation = Donation.query.one()
        assert donation.razorpay_payment_id == "pay_TY1ckLpy6lUDMr"
        assert donation.receipt_number
        assert PendingZohoSubmission.query.one().resolution == "reconciled"

    def test_the_phone_format_mismatch_that_would_have_broken_matching(self, client, app):
        """Zoho sends "+919625901202"; Razorpay returns "9625901202" for
        the same donor. Matching on the raw strings would have missed
        every single one of these."""
        from models import Donation, PendingZohoSubmission

        _submit_form(client, app, full_name="Nandani Kumari", phone="+919625901202")
        submitted_at = PendingZohoSubmission.query.one().received_at

        _reconcile(app, [_payment("pay_Fmt1", 100, "9625901202", submitted_at=submitted_at)])

        assert Donation.query.count() == 1

    def test_it_is_idempotent(self, client, app):
        """The job runs hourly. A second run over the same payment must
        not issue a second receipt -- these are tax documents."""
        from models import Donation, PendingZohoSubmission

        _submit_form(client, app, phone="9625901202")
        submitted_at = PendingZohoSubmission.query.one().received_at
        payment = _payment("pay_Idem1", 100, "9625901202", submitted_at=submitted_at)

        _reconcile(app, [payment])
        _reconcile(app, [payment])

        assert Donation.query.count() == 1

    def test_a_receipt_is_never_issued_twice_for_one_payment(self, client, app):
        """Belt and braces on the above: even if two open submissions
        somehow both looked plausible, one payment can only ever produce
        one donation, because the payment is already attached to one."""
        from models import Donation, PendingZohoSubmission

        _submit_form(client, app, phone="9625901202")
        submitted_at = PendingZohoSubmission.query.one().received_at
        _reconcile(app, [_payment("pay_Once1", 100, "9625901202", submitted_at=submitted_at)])

        # A fresh submission from the same donor for the same amount, and
        # the same already-used payment still in the scan window.
        _submit_form(client, app, phone="9625901202")
        _reconcile(app, [_payment("pay_Once1", 100, "9625901202", submitted_at=submitted_at)])

        assert Donation.query.filter_by(razorpay_payment_id="pay_Once1").count() == 1


class TestItRefusesToGuess:
    """These produce 80G tax receipts. A wrong match is worse than no
    match, so everything below must decline rather than pick."""

    def test_two_matching_payments_are_left_for_a_human(self, client, app):
        from models import Donation, PendingZohoSubmission

        _submit_form(client, app, phone="9625901202")
        submitted_at = PendingZohoSubmission.query.one().received_at

        summary = _reconcile(app, [
            _payment("pay_Amb1", 100, "9625901202", submitted_at=submitted_at),
            _payment("pay_Amb2", 100, "9625901202", submitted_at=submitted_at, minutes_after_submission=5),
        ])

        assert Donation.query.count() == 0
        assert len(summary["ambiguous"]) == 1
        pending = PendingZohoSubmission.query.one()
        assert pending.resolution == "ambiguous"
        assert "pay_Amb1" in pending.note and "pay_Amb2" in pending.note

    def test_two_submissions_fitting_one_payment_are_left_for_a_human(self, client, app):
        """Two donors, same amount, same phone on file, one payment. There
        is no honest way to pick, so neither gets a receipt."""
        from models import Donation, PendingZohoSubmission

        from extensions import db
        from models import Campaign
        db.session.add(Campaign(name="Seminar Fees", is_80g=False))
        db.session.commit()

        _submit_form(client, app, full_name="Donor One", phone="9625901202")
        _submit_form(client, app, full_name="Donor Two", phone="9625901202", campaign="Seminar Fees")
        submitted_at = min(s.received_at for s in PendingZohoSubmission.query.all())

        summary = _reconcile(app, [_payment("pay_Rival1", 100, "9625901202", submitted_at=submitted_at)])

        assert Donation.query.count() == 0
        assert len(summary["ambiguous"]) == 2

    def test_a_different_amount_is_not_a_match(self, client, app):
        from models import Donation

        _submit_form(client, app, phone="9625901202", amount="100")
        summary = _reconcile(app, [_payment("pay_Diff1", 500, "9625901202")])

        assert Donation.query.count() == 0
        assert summary["still_waiting"] == 1

    def test_a_different_phone_is_not_a_match(self, client, app):
        from models import Donation

        _submit_form(client, app, phone="9625901202")
        summary = _reconcile(app, [_payment("pay_Diff2", 100, "9000000000")])

        assert Donation.query.count() == 0
        assert summary["still_waiting"] == 1

    def test_a_payment_made_before_the_form_was_submitted_is_not_a_match(self, client, app):
        """Someone else's earlier payment for the same amount must not be
        harvested by a form submitted afterwards."""
        from models import Donation, PendingZohoSubmission

        _submit_form(client, app, phone="9625901202")
        submitted_at = PendingZohoSubmission.query.one().received_at

        summary = _reconcile(app, [
            _payment("pay_Early1", 100, "9625901202", submitted_at=submitted_at,
                     minutes_after_submission=-30),
        ])

        assert Donation.query.count() == 0
        assert summary["still_waiting"] == 1

    def test_an_uncaptured_payment_is_not_a_match(self, client, app):
        """Authorized-but-not-captured is not money received."""
        from models import Donation, PendingZohoSubmission

        _submit_form(client, app, phone="9625901202")
        summary = _reconcile(app, [_payment("pay_Auth1", 100, "9625901202", status="authorized")])

        assert Donation.query.count() == 0
        assert summary["still_waiting"] == 1
        assert summary["orphan_payments"] == [], \
            "an uncaptured payment is not money received, so it isn't an unexplained receipt either"
        assert PendingZohoSubmission.query.one().resolved_at is None, \
            "the submission stays open -- the donor may yet complete the payment"

    def test_an_80g_campaign_with_no_pan_is_flagged_not_receipted(self, client, app):
        """Reconciliation runs every rule the webhook runs, because it
        creates donations through the same function. An 80G campaign with
        no PAN on the submission can't produce a valid tax receipt, so it
        goes to a human -- it must never quietly issue one anyway, and
        must never silently drop the payment either."""
        from models import Donation, PendingZohoSubmission

        _submit_form(client, app, campaign="Annadan", phone="9625901202")  # Annadan is 80G
        submitted_at = PendingZohoSubmission.query.one().received_at

        summary = _reconcile(app, [_payment("pay_NoPan1", 100, "9625901202", submitted_at=submitted_at)])

        assert Donation.query.count() == 0
        assert len(summary["ambiguous"]) == 1
        assert "PAN" in PendingZohoSubmission.query.one().note

    def test_a_payment_that_stops_confirming_on_recheck_is_not_written(self, client, app):
        """The scan says captured, the per-payment confirmation disagrees.
        Trust the confirmation and write nothing."""
        from models import Donation

        _submit_form(client, app, phone="9625901202")
        summary = _reconcile(app, [_payment("pay_Flip1", 100, "9625901202")], fetch_status="authorized")

        assert Donation.query.count() == 0
        assert summary["still_waiting"] == 1

    def test_an_unreachable_razorpay_resolves_nothing(self, client, app):
        """The failure mode that must never happen: treating "couldn't
        check" as "nothing to do" would let the job report a clean run
        while payments sit unreceipted."""
        from models import Donation, PendingZohoSubmission
        from public import reconcile_zoho_submissions

        _submit_form(client, app, phone="9625901202")

        with _razorpay([], all_side_effect=RuntimeError("connection reset")):
            summary = reconcile_zoho_submissions(app.config, min_age_minutes=0)

        assert summary["error"]
        assert Donation.query.count() == 0
        assert PendingZohoSubmission.query.one().resolved_at is None, \
            "an unreachable Razorpay must not close anything out"


class TestWhenZohoSendsNoAmount:
    """Zoho's pre-payment call is not guaranteed to carry an amount -- it
    fires before the gateway is involved, and which field a form maps to
    the `amount` payload parameter is per-form configuration. If that
    means no match can ever be made, the fix silently does nothing for
    those forms, which is the original failure one level further in."""

    def test_a_submission_with_no_amount_still_matches_on_phone(self, client, app):
        from models import Donation, PendingZohoSubmission

        _submit_form(client, app, full_name="No Amount", phone="9625901202", amount="")
        _age_submissions(minutes=60)
        pending = PendingZohoSubmission.query.one()
        assert pending.amount is None, "precondition: nothing to match on but phone and time"

        _reconcile(app, [_payment("pay_NoAmt1", 250, "9625901202", submitted_at=pending.received_at)])

        donation = Donation.query.one()
        assert donation.razorpay_payment_id == "pay_NoAmt1"
        assert donation.receipt_number

    def test_the_receipt_uses_the_amount_razorpay_actually_received(self, client, app):
        """The figure on a receipt has to be the money that actually
        arrived, not a form field that may be blank or stale."""
        from models import Donation, PendingZohoSubmission

        _submit_form(client, app, phone="9625901202", amount="")
        _age_submissions(minutes=60)
        pending = PendingZohoSubmission.query.one()

        _reconcile(app, [_payment("pay_NoAmt2", 250, "9625901202", submitted_at=pending.received_at)])

        assert float(Donation.query.one().amount) == 250.0

    def test_it_still_refuses_to_guess_between_two_payments(self, client, app):
        """Matching on less information must not mean matching more
        loosely -- two payments from the same donor in the window is still
        a human's call."""
        from models import Donation, PendingZohoSubmission

        _submit_form(client, app, phone="9625901202", amount="")
        _age_submissions(minutes=60)
        pending = PendingZohoSubmission.query.one()

        summary = _reconcile(app, [
            _payment("pay_NoAmtA", 250, "9625901202", submitted_at=pending.received_at),
            _payment("pay_NoAmtB", 900, "9625901202", submitted_at=pending.received_at,
                     minutes_after_submission=6),
        ])

        assert Donation.query.count() == 0
        assert len(summary["ambiguous"]) == 1

    def test_a_submission_with_no_phone_is_never_matched(self, client, app):
        """Phone is the one field matching genuinely cannot do without.
        Without it, anything is a "match" -- so match nothing, and let it
        surface as an orphan payment for a person instead."""
        from models import Donation, PendingZohoSubmission

        _submit_form(client, app, phone="", amount="100")
        _age_submissions(minutes=60)

        summary = _reconcile(app, [_payment("pay_NoPhone1", 100, "9625901202")])

        assert Donation.query.count() == 0
        assert [p["payment_id"] for p in summary["orphan_payments"]] == ["pay_NoPhone1"]


class TestItDoesNotHoardDonorData:
    """This table newly persists whole donor payloads. That has to obey
    the same rules the rest of the app does, and not become a quiet
    permanent copy of the donor list."""

    def test_a_spurious_pan_is_not_stored(self, client, app):
        """REG-001: a PAN on a Non-80G donation below the high-value
        threshold isn't required, so it must not be persisted.
        _create_zoho_donation already strips it before it can reach a
        donor profile -- without the same rule here, this table is a way
        around that, and these Zoho forms are Non-80G registrations, so
        every PAN typed into one is spurious by definition."""
        import json
        from models import PendingZohoSubmission

        _submit_form(client, app, phone="9625901202", amount="100", pan="ABCDE1234F")

        stored = json.loads(PendingZohoSubmission.query.one().payload_json)
        assert stored.get("pan") == "", "a PAN with no legal basis must not be kept"

    def test_a_pan_that_is_needed_is_kept(self, client, app):
        """The 80G case: the PAN is required to issue the receipt, so
        stripping it would break the donation this feature exists to
        rescue."""
        import json
        from models import PendingZohoSubmission

        _submit_form(client, app, campaign="Annadan", phone="9625901202",
                     amount="100", pan="ABCDE1234F")  # Annadan is 80G

        stored = json.loads(PendingZohoSubmission.query.one().payload_json)
        assert stored.get("pan") == "ABCDE1234F"

    def test_a_pan_is_kept_when_the_amount_is_unknown(self, client, app):
        """Zoho's pre-payment call doesn't always carry an amount, and a
        high-value donation can't be ruled out without one. Dropping the
        PAN there would fail the high-value check at creation and turn a
        recoverable donation into manual work -- so it's kept unless we
        can positively establish it isn't needed."""
        import json
        from models import PendingZohoSubmission

        _submit_form(client, app, phone="9625901202", amount="", pan="ABCDE1234F")

        stored = json.loads(PendingZohoSubmission.query.one().payload_json)
        assert stored.get("pan") == "ABCDE1234F"

    def test_a_high_value_pan_is_kept(self, client, app):
        import json
        from models import PendingZohoSubmission
        from utils import HIGH_VALUE_PAN_THRESHOLD

        _submit_form(client, app, phone="9625901202",
                     amount=str(HIGH_VALUE_PAN_THRESHOLD + 1000), pan="ABCDE1234F")

        stored = json.loads(PendingZohoSubmission.query.one().payload_json)
        assert stored.get("pan") == "ABCDE1234F"

    def test_resolved_rows_are_eventually_deleted(self, client, app):
        """Once resolved, this row's job is done -- the donation record
        (or the deliberate absence of one) is what's worth keeping.
        Holding a name, phone and address indefinitely because someone
        once opened a registration form isn't something to do quietly,
        and nothing else here would ever have cleaned them up."""
        from extensions import db
        from models import PendingZohoSubmission

        _submit_form(client, app, phone="9625901202")
        # 3 days: past max_age_hours (48h) so it closes as unpaid, but
        # still inside the scan horizon (lookback_days + 1 = 4 days), so
        # it's evaluated rather than expired.
        _age_submissions(minutes=60 * 24 * 3)

        _reconcile(app, [], max_age_hours=48, retain_resolved_days=90)
        db.session.expire_all()
        row = PendingZohoSubmission.query.one()
        assert row.resolution == "unpaid"

        # Backdate the resolution itself past the retention window.
        row.resolved_at = datetime.datetime.utcnow() - datetime.timedelta(days=120)
        db.session.commit()

        summary = _reconcile(app, [], retain_resolved_days=90)

        assert summary["pruned"] == 1
        assert PendingZohoSubmission.query.count() == 0

    def test_a_row_too_old_to_ever_match_is_closed_off_not_left_dangling(self, client, app):
        """Past the scan horizon nothing can match it any more. Left
        unresolved it would sit there forever -- never retried, never
        reported, and never pruned either, since pruning only touches
        resolved rows. That's donor data accumulating with no path out.

        Rare in normal running; it means the job was down for days, which
        is exactly what has happened to this project's cron jobs before."""
        from extensions import db
        from models import PendingZohoSubmission

        _submit_form(client, app, phone="9625901202")
        _age_submissions(minutes=60 * 24 * 30)   # far beyond lookback + 1

        summary = _reconcile(app, [], retain_resolved_days=90)

        assert summary["expired"] == 1
        row = PendingZohoSubmission.query.one()
        assert row.resolution == "expired"
        assert row.resolved_at is not None, "must be closed off so retention can reach it"

        # And having been closed off, it is now prunable like anything else.
        row.resolved_at = datetime.datetime.utcnow() - datetime.timedelta(days=120)
        db.session.commit()
        assert _reconcile(app, [], retain_resolved_days=90)["pruned"] == 1
        assert PendingZohoSubmission.query.count() == 0

    def test_a_row_still_within_the_window_is_left_open(self, client, app):
        """Unfinished business, not history -- a recent unresolved row
        still represents a donor who may be owed a receipt."""
        from models import PendingZohoSubmission

        _submit_form(client, app, phone="9625901202")
        _age_submissions(minutes=60)

        summary = _reconcile(app, [], retain_resolved_days=1)

        assert summary["pruned"] == 0 and summary["expired"] == 0
        assert PendingZohoSubmission.query.one().resolved_at is None

    def test_a_recently_resolved_row_is_kept_for_auditing(self, client, app):
        from extensions import db
        from models import Donation, PendingZohoSubmission

        _submit_form(client, app, phone="9625901202")
        _age_submissions(minutes=60)
        submitted_at = PendingZohoSubmission.query.one().received_at

        _reconcile(app, [_payment("pay_Keep1", 100, "9625901202", submitted_at=submitted_at)],
                   retain_resolved_days=90)

        assert Donation.query.count() == 1
        assert PendingZohoSubmission.query.count() == 1, \
            "a just-resolved row stays available for auditing the receipt it produced"


class TestScanWindow:
    """The Razorpay scan window itself -- bugs here are invisible (the job
    reports a clean run) but mean payments are never looked at."""

    def test_the_lookback_window_is_a_real_utc_window(self, client, app):
        """Regression: this was computed with now_ist(), which returns a
        *naive* datetime holding IST wall-clock. .timestamp() on a naive
        datetime reads it as the host's local zone, so on Render (UTC) the
        window silently started 5h30m late -- a "3 day" scan that actually
        covered 2 days 18.5 hours. Payments in that gap were never
        examined, and nothing anywhere would have said so.

        Pinned to TZ=UTC for the duration, deliberately: on a machine
        already set to IST the two errors cancel out and this passes with
        the bug still present. That is exactly how the bug survived its
        first test -- the sandbox this was written in runs
        TZ=Asia/Calcutta, while production runs UTC. Asserting under the
        timezone production actually uses is the only version of this test
        that means anything."""
        from public import unreconciled_razorpay_payments

        app.config["RAZORPAY_ENABLED"] = True
        captured_args = {}

        client_mock = MagicMock()

        def _record(params):
            captured_args.update(params)
            return {"items": [], "count": 0}

        client_mock.payment.all.side_effect = _record

        original_tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()
        try:
            with patch("razorpay.Client", return_value=client_mock):
                unreconciled_razorpay_payments(app.config, lookback_days=3)

            expected = (datetime.datetime.utcnow() - datetime.timedelta(days=3)) \
                .replace(tzinfo=datetime.timezone.utc).timestamp()
            # Within a minute of a true 3-days-ago UTC instant. The old bug
            # was off by 19,800 seconds, so this catches it with room to spare.
            assert abs(captured_args["from"] - expected) < 60
        finally:
            if original_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original_tz
            time.tzset()

    def test_it_pages_through_more_than_one_hundred_payments(self, client, app):
        """Razorpay caps a page at 100. A busy festival day can exceed
        that, and stopping at the first page would silently ignore
        everything past it."""
        from public import unreconciled_razorpay_payments

        app.config["RAZORPAY_ENABLED"] = True
        page1 = [_payment(f"pay_p{i}", 10, "9000000000") for i in range(100)]
        page2 = [_payment("pay_last", 10, "9000000000")]

        client_mock = MagicMock()
        client_mock.payment.all.side_effect = [
            {"items": page1, "count": 100},
            {"items": page2, "count": 1},
        ]

        with patch("razorpay.Client", return_value=client_mock):
            payments, error = unreconciled_razorpay_payments(app.config)

        assert error is None
        assert len(payments) == 101
        assert "pay_last" in [p["payment_id"] for p in payments]

    def test_uncaptured_payments_are_excluded_from_the_scan(self, client, app):
        from public import unreconciled_razorpay_payments

        app.config["RAZORPAY_ENABLED"] = True
        with _razorpay([
            _payment("pay_ok", 10, "9000000000"),
            _payment("pay_failed", 10, "9000000000", status="failed"),
            _payment("pay_auth", 10, "9000000000", status="authorized"),
        ]):
            payments, _ = unreconciled_razorpay_payments(app.config)

        assert [p["payment_id"] for p in payments] == ["pay_ok"]

    def test_no_razorpay_configured_is_not_an_error(self, client, app):
        """Demo/local deployments have no Razorpay at all. Nothing to
        reconcile against isn't a failure to report."""
        from public import unreconciled_razorpay_payments

        app.config["RAZORPAY_ENABLED"] = False
        payments, error = unreconciled_razorpay_payments(app.config)

        assert payments == []
        assert error is None


class TestLifecycle:
    def test_a_very_recent_submission_is_left_alone(self, client, app):
        """The donor may still be mid-checkout, and Zoho's own follow-up
        call deserves the first chance to handle it."""
        from models import Donation
        from public import reconcile_zoho_submissions

        _submit_form(client, app, phone="9625901202")

        with _razorpay([_payment("pay_Fresh1", 100, "9625901202")]):
            summary = reconcile_zoho_submissions(app.config, min_age_minutes=15)

        assert Donation.query.count() == 0
        assert summary["still_waiting"] == 1

    def test_an_old_submission_with_no_payment_is_closed_as_unpaid(self, client, app):
        """Someone opened the form and never paid. Ordinary, not an error
        -- but it must stop being rechecked forever."""
        from models import PendingZohoSubmission

        _submit_form(client, app, phone="9625901202")
        _age_submissions(minutes=60 * 72)

        summary = _reconcile(app, [], max_age_hours=48)

        assert summary["unpaid"] == 1
        assert PendingZohoSubmission.query.one().resolution == "unpaid"

    def test_zohos_own_follow_up_call_closes_the_pending_record(self, client, app):
        """When Zoho does behave, the webhook creates the donation itself.
        The pending row from the earlier call has to be closed out, or
        reconciliation would keep hunting for a payment that has already
        become a receipt."""
        from models import Donation, PendingZohoSubmission

        _submit_form(client, app, phone="9811100011", amount="2100")
        assert PendingZohoSubmission.query.one().resolved_at is None

        # The follow-up call Zoho is supposed to send, arriving this time.
        with _razorpay([], fetch_status="captured"):
            resp = client.post(
                f"{URL}?campaign=BACE Contribution",
                json={
                    "full_name": "Zoho Donor", "phone": "9811100011",
                    "amount": "2100", "payment_status": "Completed",
                    "payment_transaction_id": "pay_FollowUp1",
                },
                headers={"X-Zoho-Webhook-Token": TOKEN},
            )

        assert resp.status_code == 200
        donation = Donation.query.one()
        pending = PendingZohoSubmission.query.one()
        assert pending.resolution == "webhook"
        assert pending.donation_id == donation.id

    def test_an_ambiguous_payment_is_not_also_reported_as_unexplained(self, client, app):
        """A payment named in an ambiguous decision is accounted for -- by
        the line in the same report saying a submission matches it and
        someone needs to pick. Listing it again under "nothing in this
        system matches this" contradicted the line directly above it in
        the daily report email."""
        from models import PendingZohoSubmission

        _submit_form(client, app, phone="9625901202")
        _age_submissions(minutes=60)
        submitted_at = PendingZohoSubmission.query.one().received_at

        summary = _reconcile(app, [
            _payment("pay_AmbX", 100, "9625901202", submitted_at=submitted_at),
            _payment("pay_AmbY", 100, "9625901202", submitted_at=submitted_at, minutes_after_submission=6),
            _payment("pay_TrulyOrphan", 999, "9000009999", submitted_at=submitted_at),
        ])

        assert len(summary["ambiguous"]) == 1
        reported = [p["payment_id"] for p in summary["orphan_payments"]]
        assert reported == ["pay_TrulyOrphan"], \
            "only payments nothing accounts for at all belong in the orphan list"

    def test_a_captured_payment_nothing_can_explain_is_reported(self, client, app):
        """Money Razorpay received that no donation in this app accounts
        for -- from any source, not just Zoho. Never guessed at, always
        surfaced."""
        app.config["RAZORPAY_ENABLED"] = True
        summary = _reconcile(app, [_payment("pay_Orphan1", 750, "9000000001")])

        assert [p["payment_id"] for p in summary["orphan_payments"]] == ["pay_Orphan1"]

    def test_a_hand_entered_backfill_stops_being_flagged(self, client, app):
        """The workflow this feature actually creates: the report flags a
        payment, staff enter it through Offline Donation -> Single Entry,
        and it must then go quiet.

        That form stores the payment reference in bank_transaction_id, not
        razorpay_payment_id, so matching only the latter meant the very
        payments staff had just finished fixing kept reappearing as
        unexplained, every day, forever. A safety net that keeps flagging
        resolved items is one people stop reading."""
        from extensions import db
        from models import Campaign, Donation, Donor

        donor = Donor(full_name="Backfilled By Hand", phone="9319880507")
        db.session.add(donor)
        db.session.flush()
        db.session.add(Donation(
            donor_id=donor.id, campaign_id=Campaign.query.first().id, amount=100,
            status="success", payment_mode="online",
            bank_transaction_id="pay_TY1ckLpy6lUDMr",   # a real one from production
        ))
        db.session.commit()

        app.config["RAZORPAY_ENABLED"] = True
        summary = _reconcile(app, [_payment("pay_TY1ckLpy6lUDMr", 100, "9319880507")])

        assert summary["orphan_payments"] == [], \
            "a payment already entered by hand is accounted for, not unexplained"

    def test_a_hand_entered_backfill_is_never_receipted_twice(self, client, app):
        """The other half: a pending submission must not produce a second
        receipt for a payment staff already entered manually."""
        from extensions import db
        from models import Campaign, Donation, Donor, PendingZohoSubmission

        _submit_form(client, app, phone="9319880507", amount="100")
        _age_submissions(minutes=60)
        submitted_at = PendingZohoSubmission.query.one().received_at

        donor = Donor(full_name="Backfilled By Hand", phone="9319880507")
        db.session.add(donor)
        db.session.flush()
        db.session.add(Donation(
            donor_id=donor.id, campaign_id=Campaign.query.first().id, amount=100,
            status="success", payment_mode="online",
            bank_transaction_id="pay_Manual1",
        ))
        db.session.commit()

        _reconcile(app, [_payment("pay_Manual1", 100, "9319880507", submitted_at=submitted_at)])

        assert Donation.query.count() == 1

    def test_donations_this_app_already_knows_about_are_not_orphans(self, client, app):
        """A payment already attached to a donation (this site's own
        checkout, or an earlier reconciliation) must not be re-flagged."""
        from extensions import db
        from models import Campaign, Donation, Donor

        donor = Donor(full_name="Existing", phone="9000000002")
        db.session.add(donor)
        db.session.flush()
        db.session.add(Donation(
            donor_id=donor.id, campaign_id=Campaign.query.first().id, amount=750,
            status="success", payment_mode="online", razorpay_payment_id="pay_Known1",
        ))
        db.session.commit()

        summary = _reconcile(app, [_payment("pay_Known1", 750, "9000000002")])

        assert summary["orphan_payments"] == []


class TestResilience:
    """One bad row must not cost every other donor their receipt, and the
    job must stay safe when two copies of it overlap."""

    def test_one_failing_submission_does_not_strand_the_others(self, client, app):
        from extensions import db
        from models import Campaign, Donation, PendingZohoSubmission

        db.session.add(Campaign(name="Seminar Fees", is_80g=False))
        db.session.commit()

        _submit_form(client, app, full_name="Breaks", phone="9000000001")
        _submit_form(client, app, full_name="Fine", phone="9000000002", campaign="Seminar Fees")
        _age_submissions(minutes=60)
        submitted_at = min(s.received_at for s in PendingZohoSubmission.query.all())

        payments = [
            _payment("pay_Bad1", 100, "9000000001", submitted_at=submitted_at),
            _payment("pay_Good1", 100, "9000000002", submitted_at=submitted_at),
        ]

        import public
        real = public._create_zoho_donation

        def _boom(payload, campaign, transaction_id, order_id):
            if transaction_id == "pay_Bad1":
                raise RuntimeError("something unexpected in the stored payload")
            return real(payload, campaign, transaction_id, order_id)

        with patch.object(public, "_create_zoho_donation", side_effect=_boom):
            summary = _reconcile(app, payments)

        assert len(summary["created"]) == 1, "the healthy submission must still be receipted"
        assert Donation.query.one().razorpay_payment_id == "pay_Good1"
        assert len(summary["failed"]) == 1

        # The failed one stays open so the next hourly run retries it,
        # rather than being closed out and forgotten.
        failed = PendingZohoSubmission.query.filter_by(full_name="Breaks").one()
        assert failed.resolved_at is None

    def test_decisions_already_taken_survive_a_later_failure(self, client, app):
        """A creation failure rolls the session back. Decisions made
        earlier in the same run must already be committed, or the run
        forgets what it had ruled on."""
        from extensions import db
        from models import Campaign, PendingZohoSubmission

        db.session.add(Campaign(name="Seminar Fees", is_80g=False))
        db.session.commit()

        # One that will be closed as unpaid, one that will blow up.
        _submit_form(client, app, full_name="Abandoned", phone="9000000003")
        _submit_form(client, app, full_name="Breaks", phone="9000000001", campaign="Seminar Fees")
        _age_submissions(minutes=60 * 72)
        submitted_at = min(s.received_at for s in PendingZohoSubmission.query.all())

        import public
        with patch.object(public, "_create_zoho_donation", side_effect=RuntimeError("boom")):
            _reconcile(app, [_payment("pay_Bad2", 100, "9000000001", submitted_at=submitted_at)],
                       max_age_hours=48)

        db.session.expire_all()
        assert PendingZohoSubmission.query.filter_by(full_name="Abandoned").one().resolution == "unpaid"

    def test_a_payment_receipted_mid_run_is_not_receipted_again(self, client, app):
        """The real overlap case between the hourly cron and the daily
        report's own run.

        The scan already excludes payments attached to a donation, so a
        donation that exists *before* the run is filtered out and never
        reaches the guard -- which is why an earlier version of this test
        passed with the guard deleted and proved nothing. The case that
        matters is a donation appearing in the window *between* the scan
        and the write, which is what a concurrent run does. Simulated here
        by creating it during the per-payment confirmation call, the last
        step before the write."""
        from extensions import db
        from models import Campaign, Donation, Donor, PendingZohoSubmission
        import public

        _submit_form(client, app, phone="9000000004")
        _age_submissions(minutes=60)
        submitted_at = PendingZohoSubmission.query.one().received_at

        def _concurrent_run_wins(payment_id):
            """Stands in for another worker finishing this same payment
            first, after our snapshot was taken."""
            if not Donation.query.filter_by(razorpay_payment_id=payment_id).first():
                donor = Donor(full_name="Already There", phone="9000000004")
                db.session.add(donor)
                db.session.flush()
                db.session.add(Donation(
                    donor_id=donor.id, campaign_id=Campaign.query.first().id, amount=100,
                    status="success", payment_mode="online", razorpay_payment_id=payment_id,
                ))
                db.session.commit()
            return True, None

        with patch.object(public, "_zoho_payment_is_captured", side_effect=_concurrent_run_wins):
            _reconcile(app, [_payment("pay_Dup1", 100, "9000000004", submitted_at=submitted_at)])

        assert Donation.query.filter_by(razorpay_payment_id="pay_Dup1").count() == 1, \
            "a concurrent run's donation must not be duplicated"

    def test_work_per_run_is_bounded(self, client, app):
        """Each match costs a Razorpay confirmation call inside an HTTP
        request. Unbounded, a large backlog would run past gunicorn's
        worker timeout and be killed mid-run -- the failure mode this
        codebase has already hit twice. The remainder rolls to the next
        hourly run."""
        from extensions import db
        from models import Campaign, Donation

        for i in range(6):
            db.session.add(Campaign(name=f"Camp {i}", is_80g=False))
        db.session.commit()

        payments = []
        for i in range(6):
            _submit_form(client, app, phone=f"90000100{i:02d}", campaign=f"Camp {i}")
        _age_submissions(minutes=60)

        from models import PendingZohoSubmission
        submitted_at = min(s.received_at for s in PendingZohoSubmission.query.all())
        for i in range(6):
            payments.append(_payment(f"pay_Cap{i}", 100, f"90000100{i:02d}", submitted_at=submitted_at))

        summary = _reconcile(app, payments, max_per_run=2)

        assert len(summary["created"]) == 2
        assert Donation.query.count() == 2

    def test_the_cap_also_bounds_payments_that_fail_to_confirm(self, client, app):
        """The pathological case, and the one a creation-based cap missed
        entirely: every confirmation fails. A failing confirmation is the
        most expensive call in the loop -- three attempts, two seconds
        apart, via retry() -- so capping on donations *created* left this
        completely unbounded. Twenty-five of these would spend ~150
        seconds inside a request that gunicorn kills at 30.

        The cap counts confirmation calls attempted, so it engages here
        even though nothing is ever created."""
        from extensions import db
        from models import Campaign, Donation, PendingZohoSubmission
        import public

        for i in range(6):
            db.session.add(Campaign(name=f"Camp {i}", is_80g=False))
        db.session.commit()
        for i in range(6):
            _submit_form(client, app, phone=f"90000200{i:02d}", campaign=f"Camp {i}")
        _age_submissions(minutes=60)

        submitted_at = min(s.received_at for s in PendingZohoSubmission.query.all())
        payments = [
            _payment(f"pay_Fail{i}", 100, f"90000200{i:02d}", submitted_at=submitted_at)
            for i in range(6)
        ]

        calls = []

        def _never_confirms(payment_id):
            calls.append(payment_id)
            return False, "razorpay timed out"

        with patch.object(public, "_zoho_payment_is_captured", side_effect=_never_confirms):
            summary = _reconcile(app, payments, max_per_run=2)

        assert Donation.query.count() == 0
        assert len(calls) == 2, f"cap must bound confirmation calls, made {len(calls)}"
        assert summary["created"] == []


class TestDailyReportSafetyNet:
    """Reconciliation also runs from the daily report, because this
    project's cron jobs have silently failed for days at a time before."""

    def test_the_daily_report_runs_reconciliation(self, client, app):
        from daily_report_utils import run_reconciliation_safely
        from models import Donation, PendingZohoSubmission

        _submit_form(client, app, phone="9625901202")
        _age_submissions(minutes=60)
        submitted_at = PendingZohoSubmission.query.one().received_at

        with _razorpay([_payment("pay_Daily1", 100, "9625901202", submitted_at=submitted_at)]):
            summary = run_reconciliation_safely(app)

        assert len(summary["created"]) == 1
        assert Donation.query.one().receipt_number

    def test_a_reconciliation_failure_never_stops_the_daily_report(self, app):
        """The report going out matters more than the sweep succeeding."""
        from daily_report_utils import run_reconciliation_safely

        with patch("public.reconcile_zoho_submissions", side_effect=RuntimeError("boom")):
            summary = run_reconciliation_safely(app)

        assert summary["error"]

    def test_stranded_payments_appear_in_the_report_email(self, app):
        """What makes this visible to a human. Before, the only signal was
        a stale cell in Zoho's own Reports grid."""
        from daily_report_utils import _render_email_html

        html = _render_email_html(
            {
                "report_date": datetime.date(2026, 9, 6),
                "week_start": datetime.date(2026, 8, 31),
                "month_start": datetime.date(2026, 9, 1),
                "today": {"amount": 0, "count": 0, "campaigns": []},
                "week": {"amount": 0, "count": 0, "campaigns": []},
                "month": {"amount": 0, "count": 0, "campaigns": []},
                "reconciliation": {
                    "created": [], "ambiguous": [], "unpaid": 0, "still_waiting": 0,
                    "error": None,
                    "orphan_payments": [
                        {"payment_id": "pay_Orphan9", "amount": 100.0, "contact": "9625901202"},
                    ],
                },
            },
            "Test Temple",
        )

        assert "pay_Orphan9" in html
        assert "Payment Reconciliation" in html

    def test_a_clean_run_adds_nothing_to_the_email(self, app):
        """No box that always says zero -- people stop reading those."""
        from daily_report_utils import _render_email_html

        html = _render_email_html(
            {
                "report_date": datetime.date(2026, 9, 6),
                "week_start": datetime.date(2026, 8, 31),
                "month_start": datetime.date(2026, 9, 1),
                "today": {"amount": 0, "count": 0, "campaigns": []},
                "week": {"amount": 0, "count": 0, "campaigns": []},
                "month": {"amount": 0, "count": 0, "campaigns": []},
                "reconciliation": {
                    "created": [], "ambiguous": [], "unpaid": 3, "still_waiting": 2,
                    "orphan_payments": [], "error": None,
                },
            },
            "Test Temple",
        )

        assert "Payment Reconciliation" not in html


class TestInternalRoute:
    """POST /internal/zoho-reconcile -- what the hourly cron calls."""

    def test_it_requires_the_internal_token(self, client, app):
        app.config["INTERNAL_TASK_TOKEN"] = "secret-token"
        resp = client.post("/internal/zoho-reconcile", json={}, headers={"X-Internal-Token": "wrong"})
        assert resp.status_code == 401

    def test_it_refuses_to_run_unauthenticated_when_no_token_is_set(self, client, app):
        resp = client.post("/internal/zoho-reconcile", json={})
        assert resp.status_code == 503

    def test_it_reports_what_it_issued(self, client, app):
        from models import PendingZohoSubmission

        app.config["INTERNAL_TASK_TOKEN"] = "secret-token"
        _submit_form(client, app, phone="9625901202")
        _age_submissions(minutes=60)
        submitted_at = PendingZohoSubmission.query.one().received_at

        with _razorpay([_payment("pay_Route1", 100, "9625901202", submitted_at=submitted_at)]):
            resp = client.post(
                "/internal/zoho-reconcile", json={},
                headers={"X-Internal-Token": "secret-token"},
            )

        assert resp.status_code == 200
        created = resp.get_json()["created"]
        assert len(created) == 1
        assert created[0]["receipt_number"]

    def test_an_unreachable_razorpay_is_a_non_2xx_not_a_quiet_success(self, client, app):
        """A scheduled job reporting success on a scan it never performed
        is precisely the silent failure this feature exists to end."""
        app.config["INTERNAL_TASK_TOKEN"] = "secret-token"
        app.config["RAZORPAY_ENABLED"] = True

        with _razorpay([], all_side_effect=RuntimeError("razorpay down")):
            resp = client.post(
                "/internal/zoho-reconcile", json={},
                headers={"X-Internal-Token": "secret-token"},
            )

        assert resp.status_code == 502
