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
from unittest.mock import MagicMock, patch

import pytest

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
        from models import Donation

        _submit_form(client, app, phone="9625901202")
        summary = _reconcile(app, [_payment("pay_Auth1", 100, "9625901202", status="authorized")])

        assert Donation.query.count() == 0

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

    def test_a_captured_payment_nothing_can_explain_is_reported(self, client, app):
        """Money Razorpay received that no donation in this app accounts
        for -- from any source, not just Zoho. Never guessed at, always
        surfaced."""
        app.config["RAZORPAY_ENABLED"] = True
        summary = _reconcile(app, [_payment("pay_Orphan1", 750, "9000000001")])

        assert [p["payment_id"] for p in summary["orphan_payments"]] == ["pay_Orphan1"]

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
