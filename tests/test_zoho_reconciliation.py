"""Tests for reconciliation: turning Razorpay's record of captured
payments into donations and receipts.

Zoho's webhook fires once, at submission time, before the donor pays, so
it usually carries no transaction ID -- and on this account the follow-up
call it promises frequently never arrives. It also has to be configured
per form, and four of the six forms taking money in September 2026 never
were, so those donations were invisible here entirely.

Reconciliation doesn't depend on any of that. Razorpay knows which
payments were captured and have no donation behind them; this app's own
record of Zoho's calls supplies the donor's name, matched on the
transaction ID and nothing else.

Two real losses drove this, both replayed in the tests below:

  - "Me, You & The Ego", 03-Sep-2026, Rs. 100, Nandani Kumari. Zoho's own
    record showed Payment Status "Completed" and a real transaction ID.
    This app never heard about it.
  - "Essence of Bhagavad Gita", 04-Sep-2026, Rs. 100, shivam raj. Same.

Both were found only because someone noticed a stale cell in Zoho's
Reports grid days later.
"""
import datetime
import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))



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


def _setup_form(app, form_key="EBG", campaign_name="BACE Contribution", is_test=False):
    """A configured Zoho form, as Admin -> Zoho Forms would create it."""
    from extensions import db
    from models import Campaign, ZohoForm
    campaign = Campaign.query.filter_by(name=campaign_name).first()
    entry = ZohoForm(
        form_key=form_key, campaign_id=campaign.id if campaign else None, is_test=is_test,
    )
    db.session.add(entry)
    db.session.commit()
    app.config["RAZORPAY_ENABLED"] = True
    return entry


def _reconcile(app, payments, **kwargs):
    from public import reconcile_zoho_submissions
    with _razorpay(payments, fetch_status=kwargs.pop("fetch_status", "captured")):
        return reconcile_zoho_submissions(app.config, **kwargs)




_ZOHO_ENTRIES = {}


@pytest.fixture(autouse=True)
def _zoho_api(app):
    """Serves whatever the test registered with _record(), in the shape
    Zoho's own API returns. Autouse so no test can accidentally reach the
    real API, and cleared per test so entries never leak between them."""
    _ZOHO_ENTRIES.clear()
    app.config.update(
        ZOHO_CLIENT_ID="cid", ZOHO_CLIENT_SECRET="csec", ZOHO_REFRESH_TOKEN="rtok",
    )

    def _entries(config, link_name, start_index=1, limit=200):
        return (list(_ZOHO_ENTRIES.values()) if start_index == 1 else []), "records"

    with patch("zoho_api.entries", side_effect=_entries):
        yield


def _record(form, transaction_id, full_name, phone, amount, payment_status="Completed"):
    """One entry as Zoho's API returns it for this account: the composite
    Name control, and the transaction as the combined
    "Txn ID : pay_X Order ID : order_Y" string production actually sends."""
    _ZOHO_ENTRIES[transaction_id] = {
        "Name": full_name,
        "Phone": phone,
        "Payment Amount": str(amount),
        "Payment Status": payment_status,
        "Payment Transaction ID": (
            f"Txn ID : {transaction_id} "
            f"Order ID : {transaction_id.replace('pay_', 'order_')}"
        ),
        "Added Time": "04-Sep-2026 21:55:22",
    }
    return _ZOHO_ENTRIES[transaction_id]


def _post_captured(client, app, payment_id, amount, contact, notes=None):
    """A signed payment.captured event, as Razorpay sends it."""
    import hashlib
    import hmac as hmac_mod
    import json as json_mod

    app.config["RAZORPAY_WEBHOOK_SECRET"] = "wh-secret"
    body = json_mod.dumps({
        "event": "payment.captured",
        "payload": {"payment": {"entity": {
            "id": payment_id,
            "order_id": payment_id.replace("pay_", "order_"),
            "amount": int(round(amount * 100)),
            "status": "captured",
            "contact": contact,
            "created_at": int(datetime.datetime.utcnow()
                              .replace(tzinfo=datetime.timezone.utc).timestamp()),
            "notes": notes if notes is not None else
                     {"zform_custom": "iskcondwarka,EBG,tok"},
        }}},
    })
    sig = hmac_mod.new(b"wh-secret", body.encode(), hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/razorpay", data=body,
        headers={"Content-Type": "application/json", "X-Razorpay-Signature": sig},
    )


def _render_daily_email(summary):
    """Renders the daily report email around this reconciliation summary.

    The email is the only consumer of that dict, and it reads keys off it
    positionally rather than defensively -- so a summary whose shape has
    drifted throws here, in the report builder, and takes down an email
    that has nothing to do with Zoho."""
    from daily_report_utils import _render_email_html, compute_report
    data = compute_report()
    data["reconciliation"] = summary
    return _render_email_html(data, "ISKCON Dwarka")


class TestPaymentOrigin:
    """Razorpay's notes say where a payment came from, and the two sources
    need opposite handling. Without this every unexplained payment looked
    alike, and a 141-row list mixing website checkouts, Zoho forms and
    test traffic is one somebody backfills wholesale into duplicates."""

    def test_a_zoho_payment_is_identified_by_its_form(self, client, app):
        """Real note shape from production:
        {"zform_custom": "iskcondwarka,BACERENT,<token>"}. The middle field
        names the form -- the one fact that says which Zoho report to open."""
        from public import unreconciled_razorpay_payments

        app.config["RAZORPAY_ENABLED"] = True
        pay = _payment("pay_Zf1", 8000, "8861957012")
        pay["notes"] = {"zform_custom": "iskcondwarka,BACERENT,svofYsw8Cp2mh91a1nny7cIeUHj9myrg"}

        with _razorpay([pay]):
            payments, _ = unreconciled_razorpay_payments(app.config)

        assert payments[0]["source"] == "zoho"
        assert payments[0]["source_ref"] == "BACERENT"

    def test_a_website_payment_is_identified_by_its_donation(self, client, app):
        from public import unreconciled_razorpay_payments

        app.config["RAZORPAY_ENABLED"] = True
        pay = _payment("pay_Web1", 500, "9000000001")
        pay["notes"] = {"donation_id": "4321", "campaign": "Annadan"}

        with _razorpay([pay]):
            payments, _ = unreconciled_razorpay_payments(app.config)

        assert payments[0]["source"] == "website"
        assert payments[0]["source_ref"] == "4321"

    def test_a_payment_with_no_notes_is_unknown_not_guessed(self, client, app):
        from public import unreconciled_razorpay_payments

        app.config["RAZORPAY_ENABLED"] = True
        with _razorpay([_payment("pay_Bare1", 100, "9000000002")]):
            payments, _ = unreconciled_razorpay_payments(app.config)

        assert payments[0]["source"] == "unknown"
        assert payments[0]["source_ref"] is None

    def test_a_settled_website_donation_is_not_an_orphan(self, client, app):
        """The big false-positive class in a historical scan. A donor pays,
        closes the tab before the browser confirms, and the payment id
        never lands on the donation -- but a webhook or a later retry
        finished it, so the receipt exists. Matching on notes.donation_id
        recognises that; matching on the id columns alone flags it as
        missing money forever."""
        from extensions import db
        from models import Campaign, Donation, Donor
        from public import unreconciled_razorpay_payments

        donor = Donor(full_name="Web Donor", phone="9000000003")
        db.session.add(donor)
        db.session.flush()
        d = Donation(
            donor_id=donor.id, campaign_id=Campaign.query.first().id, amount=500,
            status="success", payment_mode="online", receipt_number="032511/ISK500999",
        )
        db.session.add(d)
        db.session.commit()

        app.config["RAZORPAY_ENABLED"] = True
        pay = _payment("pay_Web2", 500, "9000000003")
        pay["notes"] = {"donation_id": str(d.id), "campaign": "Annadan"}

        with _razorpay([pay]):
            payments, _ = unreconciled_razorpay_payments(app.config)

        assert payments == [], "a successful donation accounts for its payment, id column or not"

    def test_an_unfinished_website_donation_is_still_reported(self, client, app):
        """The opposite case, and it must not be swept up by the above: a
        captured payment against a donation still sitting pending is real
        money with no receipt -- the same loss as the Zoho gap, arriving by
        a different route."""
        from extensions import db
        from models import Campaign, Donation, Donor
        from public import unreconciled_razorpay_payments

        donor = Donor(full_name="Stuck Donor", phone="9000000004")
        db.session.add(donor)
        db.session.flush()
        d = Donation(
            donor_id=donor.id, campaign_id=Campaign.query.first().id, amount=500,
            status="pending", payment_mode="online",
        )
        db.session.add(d)
        db.session.commit()

        app.config["RAZORPAY_ENABLED"] = True
        pay = _payment("pay_Web3", 500, "9000000004")
        pay["notes"] = {"donation_id": str(d.id), "campaign": "Annadan"}

        with _razorpay([pay]):
            payments, _ = unreconciled_razorpay_payments(app.config)

        assert [p["payment_id"] for p in payments] == ["pay_Web3"]
        assert payments[0]["source"] == "website"


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

    def test_a_truncated_scan_is_an_error_not_a_short_list(self, client, app):
        """The old cap simply stopped at 1,000 payments, which made a
        truncated scan indistinguishable from a complete one. Everything
        downstream reads "not in this list" as "no such payment", so
        unseen payments would look receipted and unseen submissions would
        be closed as unpaid. Reporting a partial scan as complete is the
        exact failure this feature exists to eliminate."""
        from public import unreconciled_razorpay_payments

        app.config["RAZORPAY_ENABLED"] = True
        full_page = [_payment(f"pay_bulk{i}", 10, "9000000000") for i in range(100)]

        client_mock = MagicMock()
        client_mock.payment.all.return_value = {"items": full_page, "count": 100}

        with patch("razorpay.Client", return_value=client_mock):
            payments, error = unreconciled_razorpay_payments(app.config)

        assert payments == []
        assert error and "cut short" in error

    def test_a_fixed_date_window_is_passed_to_razorpay(self, client, app):
        from public import unreconciled_razorpay_payments

        app.config["RAZORPAY_ENABLED"] = True
        seen = {}
        client_mock = MagicMock()

        def _record(params):
            seen.update(params)
            return {"items": [], "count": 0}

        client_mock.payment.all.side_effect = _record

        original_tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()
        try:
            with patch("razorpay.Client", return_value=client_mock):
                unreconciled_razorpay_payments(
                    app.config,
                    from_date=datetime.date(2026, 8, 1),
                    to_date=datetime.date(2026, 8, 31),
                )
            assert seen["from"] == datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc).timestamp()
            # `to` covers the whole of the final day, not midnight at its start.
            assert seen["to"] == datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc).timestamp()
        finally:
            if original_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original_tz
            time.tzset()

    def test_no_razorpay_configured_is_not_an_error(self, client, app):
        """Demo/local deployments have no Razorpay at all. Nothing to
        reconcile against isn't a failure to report."""
        from public import unreconciled_razorpay_payments

        app.config["RAZORPAY_ENABLED"] = False
        payments, error = unreconciled_razorpay_payments(app.config)

        assert payments == []
        assert error is None



class TestIgnoredTestForms:
    """A test form charges real money through the live gateway, so its
    payments look exactly like donations and would sit in the report
    forever. A report carrying permanent known-noise is one people stop
    reading -- which is how the original failures went unnoticed."""

    def test_a_test_forms_payments_are_kept_out_of_the_actionable_list(self, client, app):
        from public import reconcile_zoho_submissions

        _setup_form(app, form_key="TestingWebsitewithFormsintergration", is_test=True)
        _setup_form(app, form_key="EBG")

        test_pay = _payment("pay_TestOne", 10, "9650150283")
        test_pay["notes"] = {"zform_custom": "iskcondwarka,TestingWebsitewithFormsintergration,tok"}
        real_pay = _payment("pay_RealOne", 100, "9319880507")
        real_pay["notes"] = {"zform_custom": "iskcondwarka,EBG,tok"}

        with _razorpay([test_pay, real_pay]):
            summary = reconcile_zoho_submissions(app.config)

        assert [p["payment_id"] for p in summary["orphan_payments"]] == ["pay_RealOne"]

    def test_they_are_reported_separately_rather_than_disappearing(self, client, app):
        """A form ticked as a test by mistake must show up as a
        suspiciously busy line, not vanish."""
        from public import reconcile_zoho_submissions

        _setup_form(app, form_key="TestingWebsitewithFormsintergration", is_test=True)
        pay = _payment("pay_TestTwo", 10, "9650150283")
        pay["notes"] = {"zform_custom": "iskcondwarka,TestingWebsitewithFormsintergration,tok"}

        with _razorpay([pay]):
            summary = reconcile_zoho_submissions(app.config)

        assert [p["payment_id"] for p in summary["ignored_payments"]] == ["pay_TestTwo"]


class TestHistoricalScan:
    """A fixed date range is for auditing an old period. It must report
    and change nothing."""

    def test_a_dated_scan_changes_nothing(self, client, app):
        """Set up so that it *would* issue a receipt if report-only were
        ignored: configured form, and a recorded Zoho call carrying this
        exact transaction ID. Without that this test passes whether or
        not report-only works, because an unmatched payment is never
        receipted anyway."""
        from models import Donation
        from public import reconcile_zoho_submissions

        form = _setup_form(app, form_key="EBG")
        _record(form, "pay_Hist1", "Nandani Kumari", "9000000001", 100)
        pay = _payment("pay_Hist1", 100, "9000000001")
        pay["notes"] = {"zform_custom": "iskcondwarka,EBG,tok"}

        # Proof the setup is live: the same payment, scanned without a
        # date range, is receipted.
        with _razorpay([pay]):
            live = reconcile_zoho_submissions(app.config)
        assert len(live["created"]) == 1
        Donation.query.delete()
        from extensions import db as _db
        _db.session.commit()

        with _razorpay([pay]):
            summary = reconcile_zoho_submissions(
                app.config,
                from_date=datetime.date(2026, 8, 1), to_date=datetime.date(2026, 8, 31),
            )

        assert summary["report_only"] is True
        assert summary["created"] == []
        assert Donation.query.count() == 0

    def test_it_still_reports_what_it_found(self, client, app):
        from public import reconcile_zoho_submissions

        app.config["RAZORPAY_ENABLED"] = True
        with _razorpay([_payment("pay_Hist2", 501, "9758517155")]):
            summary = reconcile_zoho_submissions(app.config, from_date=datetime.date(2026, 8, 1))

        assert [p["payment_id"] for p in summary["orphan_payments"]] == ["pay_Hist2"]


class TestDailyReportSafetyNet:
    """Reconciliation also runs from the daily report, because this
    project's cron jobs have silently failed for days at a time before."""

    def test_the_daily_report_actually_issues_receipts(self, client, app):
        """Not "it returned without an error" -- that passes if the call
        is a stub. A receipt has to come out the far end."""
        from models import Donation
        from daily_report_utils import run_reconciliation_safely

        form = _setup_form(app, form_key="EBG")
        _record(form, "pay_Daily1", "Shivam Raj", "9319880507", 100)
        pay = _payment("pay_Daily1", 100, "9319880507")
        pay["notes"] = {"zform_custom": "iskcondwarka,EBG,tok"}

        with _razorpay([pay]):
            summary = run_reconciliation_safely(app)

        assert summary["error"] is None
        assert len(summary["created"]) == 1
        assert Donation.query.one().receipt_number

    def test_a_reconciliation_failure_never_stops_the_daily_report(self, app):
        """The report going out matters more than the sweep succeeding."""
        from daily_report_utils import run_reconciliation_safely

        with patch("public.reconcile_zoho_submissions", side_effect=RuntimeError("boom")):
            summary = run_reconciliation_safely(app)

        assert summary["error"]
        # And the email must be able to render that summary. This dict
        # drifted out of shape once, still carrying keys from a queue
        # that had been deleted, which would have thrown inside the
        # report builder -- taking down the email as well as the sweep.
        assert set(summary) == {
            "created", "orphan_payments", "ignored_payments",
            "report_only", "error",
        }
        _render_daily_email(summary)


class TestInternalRoute:
    """POST /internal/zoho-reconcile -- what the scheduled job calls."""

    def test_it_requires_the_internal_token(self, client, app):
        app.config["INTERNAL_TASK_TOKEN"] = "secret-token"
        resp = client.post("/internal/zoho-reconcile", json={},
                           headers={"X-Internal-Token": "wrong"})
        assert resp.status_code == 401

    def test_it_refuses_to_run_unauthenticated_when_no_token_is_set(self, client, app):
        assert client.post("/internal/zoho-reconcile", json={}).status_code == 503

    def test_an_unreachable_razorpay_is_a_non_2xx_not_a_quiet_success(self, client, app):
        """A scheduled job reporting success on a scan it never performed
        is precisely the silent failure this feature exists to end."""
        app.config["INTERNAL_TASK_TOKEN"] = "secret-token"
        app.config["RAZORPAY_ENABLED"] = True

        with _razorpay([], all_side_effect=RuntimeError("razorpay down")):
            resp = client.post("/internal/zoho-reconcile", json={},
                               headers={"X-Internal-Token": "secret-token"})

        assert resp.status_code == 502

    def test_it_reports_what_it_issued(self, client, app):
        app.config["INTERNAL_TASK_TOKEN"] = "secret-token"
        form = _setup_form(app, form_key="EBG")
        _record(form, "pay_Route9", "Shivam Raj", "9319880507", 100)

        pay = _payment("pay_Route9", 100, "9319880507")
        pay["notes"] = {"zform_custom": "iskcondwarka,EBG,tok"}
        with _razorpay([pay]):
            resp = client.post("/internal/zoho-reconcile", json={},
                               headers={"X-Internal-Token": "secret-token"})

        assert resp.status_code == 200
        created = resp.get_json()["created"]
        assert len(created) == 1 and created[0]["receipt_number"]


class TestPerRunBudget:
    """max_per_run exists because this runs synchronously inside a request
    under gunicorn's 30-second worker timeout, and _zoho_payment_is_captured
    retries three times with a 2-second delay -- so a confirmation that
    fails costs ~6 seconds, more than one that succeeds. Counting the
    budget in receipts created (which this did, twice) leaves the case
    where every confirmation fails completely unbounded."""

    def _client(self, payments, fetch_status="failed"):
        client = MagicMock()
        client.payment.all.return_value = {"items": payments, "count": len(payments)}
        client.payment.fetch.side_effect = lambda pid: {"id": pid, "status": fetch_status}
        return client

    def _six_matched_payments(self, app):
        form = _setup_form(app, form_key="EBG")
        payments = []
        for n in range(6):
            pid = f"pay_Budget{n}"
            _record(form, pid, f"Donor {n}", f"90000000{n:02d}", 100)
            pay = _payment(pid, 100, f"90000000{n:02d}")
            pay["notes"] = {"zform_custom": "iskcondwarka,EBG,tok"}
            payments.append(pay)
        return payments

    def test_the_budget_bounds_confirmations_not_receipts(self, client, app):
        """Every confirmation fails, so nothing is created. The old check
        (len(created) >= max_per_run) never fires in this case and calls
        Razorpay for all six."""
        from public import reconcile_zoho_submissions

        payments = self._six_matched_payments(app)
        rz = self._client(payments, fetch_status="failed")

        with patch("razorpay.Client", return_value=rz):
            summary = reconcile_zoho_submissions(app.config, max_per_run=2)

        assert summary["created"] == []
        assert rz.payment.fetch.call_count == 2

    def test_it_still_bounds_a_run_where_everything_succeeds(self, client, app):
        from public import reconcile_zoho_submissions

        payments = self._six_matched_payments(app)
        rz = self._client(payments, fetch_status="captured")

        with patch("razorpay.Client", return_value=rz):
            summary = reconcile_zoho_submissions(app.config, max_per_run=2)

        assert len(summary["created"]) == 2
        assert rz.payment.fetch.call_count == 2

    def test_what_it_did_not_reach_says_so_rather_than_looking_unexplained(self, client, app):
        """Otherwise the daily email reports four payments with nothing to
        match them to, when in fact they were simply next in the queue."""
        from public import reconcile_zoho_submissions

        payments = self._six_matched_payments(app)
        rz = self._client(payments, fetch_status="captured")

        with patch("razorpay.Client", return_value=rz):
            summary = reconcile_zoho_submissions(app.config, max_per_run=2)

        left = summary["orphan_payments"]
        assert len(left) == 4
        assert all("next run" in p["why_not_receipted"] for p in left)

    def test_the_remainder_is_picked_up_by_the_following_run(self, client, app):
        from models import Donation
        from public import reconcile_zoho_submissions

        payments = self._six_matched_payments(app)

        for _ in range(3):
            fresh = [dict(p) for p in payments]
            rz = self._client(fresh, fetch_status="captured")
            with patch("razorpay.Client", return_value=rz):
                reconcile_zoho_submissions(app.config, max_per_run=2)

        # Six payments, two per run, three runs -- and no duplicates, since
        # each run's scan excludes payments already carrying a donation.
        assert Donation.query.count() == 6
        assert len({d.razorpay_payment_id for d in Donation.query.all()}) == 6


class TestTheDailyEmailCanRenderWhatReconciliationReturns:
    """The email reads this dict's keys directly. Every time the summary
    shape changed, this renderer was left behind -- at one point rendering
    `s.full_name` over payment dicts and `f["pending_id"]` for a table
    that had been dropped."""

    def test_a_clean_run_says_nothing_at_all(self, client, app):
        """A box that always appears, always saying zero, is a box people
        stop reading."""
        from public import reconcile_zoho_submissions

        with _razorpay([]):
            summary = reconcile_zoho_submissions(app.config)

        assert "Payment Reconciliation" not in _render_daily_email(summary)

    def test_an_unreceipted_payment_is_reported_with_its_reason(self, client, app):
        """The reason is the actionable part: "MYTE has no campaign set"
        tells the reader exactly what to do; a bare payment id does not.

        Note what it does *not* say any more -- "add it under Admin > Zoho
        Forms". The form adds itself now; the only thing left for a person
        is the campaign."""
        from public import reconcile_zoho_submissions

        app.config["RAZORPAY_ENABLED"] = True
        pay = _payment("pay_Unset1", 100, "9811100011")
        pay["notes"] = {"zform_custom": "iskcondwarka,MYTE,tok"}

        with _razorpay([pay]):
            summary = reconcile_zoho_submissions(app.config)

        html = _render_daily_email(summary)
        assert "pay_Unset1" in html
        assert "MYTE" in html and "no campaign set" in html

    def test_an_issued_receipt_is_reported(self, client, app):
        from public import reconcile_zoho_submissions

        form = _setup_form(app, form_key="EBG")
        _record(form, "pay_Mail1", "Shivam Raj", "9319880507", 100)
        pay = _payment("pay_Mail1", 100, "9319880507")
        pay["notes"] = {"zform_custom": "iskcondwarka,EBG,tok"}

        with _razorpay([pay]):
            summary = reconcile_zoho_submissions(app.config)

        html = _render_daily_email(summary)
        assert "1 receipt(s) issued" in html
        assert summary["created"][0].receipt_number in html


class TestTheInternalRouteAcceptsADateRange:
    def test_the_route_accepts_a_date_range(self, client, app):
        app.config["INTERNAL_TASK_TOKEN"] = "secret-token"
        app.config["RAZORPAY_ENABLED"] = True

        with _razorpay([]):
            resp = client.post("/internal/zoho-reconcile",
                               json={"from_date": "2026-08-01", "to_date": "2026-08-31"},
                               headers={"X-Internal-Token": "secret-token"})

        assert resp.status_code == 200
        assert resp.get_json()["report_only"] is True

    def test_a_malformed_date_is_rejected(self, client, app):
        app.config["INTERNAL_TASK_TOKEN"] = "secret-token"
        resp = client.post("/internal/zoho-reconcile", json={"from_date": "01-08-2026"},
                           headers={"X-Internal-Token": "secret-token"})
        assert resp.status_code == 400

    def test_the_reason_is_reported_not_just_the_payment_id(self, client, app):
        """The cron job prints this. Without the reason the operator gets a
        list of ids and no idea which need a form added and which need a
        webhook fixed."""
        app.config["INTERNAL_TASK_TOKEN"] = "secret-token"
        app.config["RAZORPAY_ENABLED"] = True
        pay = _payment("pay_Why1", 100, "9811100011")
        pay["notes"] = {"zform_custom": "iskcondwarka,MYTE,tok"}

        with _razorpay([pay]):
            resp = client.post("/internal/zoho-reconcile", json={},
                               headers={"X-Internal-Token": "secret-token"})

        orphan = resp.get_json()["orphan_payments"][0]
        assert "MYTE" in orphan["why_not_receipted"]


class TestAHandEnteredBackfillIsNotReceiptedTwice:
    """The duplicate case the outer handler cannot see.

    _handle_payment_captured looks the donation up by razorpay_order_id.
    A donation entered by hand through Admin -> Offline Donation records
    the payment in **bank_transaction_id** and has no order id at all, so
    that lookup misses it and the immediate path runs -- and would issue a
    second receipt for money already receipted.

    This is not hypothetical: pay_TUoekY3BLSiZXp was backfilled by hand,
    and an earlier version of the reconciliation report listed it as
    outstanding for exactly this reason. Receipt numbers are sequential
    and go into a Form 10BD filing; two for one payment is a correction
    that has to be made by hand at the tax end."""

    def _record_and_backfill(self, app, payment_id, order_id=None):
        from extensions import db
        from models import Campaign, Donation, Donor

        form = _setup_form(app, form_key="EBG")
        _record(form, payment_id, "Shivam Raj", "9319880507", 100)

        donor = Donor(full_name="Shivam Raj", phone="9319880507")
        db.session.add(donor)
        db.session.flush()
        db.session.add(Donation(
            donor_id=donor.id, campaign_id=Campaign.query.first().id, amount=100,
            status="success", payment_mode="online",
            receipt_number="032511/ISK500901",
            bank_transaction_id=payment_id,
            razorpay_order_id=order_id,
        ))
        db.session.commit()

    def test_the_razorpay_webhook_does_not_receipt_it_again(self, client, app):
        from models import Donation

        self._record_and_backfill(app, "pay_Hand1")

        resp = _post_captured(client, app, "pay_Hand1", 100, "+919319880507")

        assert resp.status_code == 200
        assert Donation.query.count() == 1
        assert Donation.query.one().receipt_number == "032511/ISK500901"

    def test_the_hourly_job_does_not_receipt_it_again_either(self, client, app):
        from models import Donation
        from public import reconcile_zoho_submissions

        self._record_and_backfill(app, "pay_Hand2")
        pay = _payment("pay_Hand2", 100, "9319880507")
        pay["notes"] = {"zform_custom": "iskcondwarka,EBG,tok"}

        with _razorpay([pay]):
            summary = reconcile_zoho_submissions(app.config)

        assert summary["created"] == []
        assert Donation.query.count() == 1

    def test_it_is_not_reported_as_outstanding_money_either(self, client, app):
        """It was, for a while, and that overstated the outstanding total
        badly enough to send someone looking for money that was never
        missing."""
        from public import reconcile_zoho_submissions

        self._record_and_backfill(app, "pay_Hand3")
        pay = _payment("pay_Hand3", 100, "9319880507")
        pay["notes"] = {"zform_custom": "iskcondwarka,EBG,tok"}

        with _razorpay([pay]):
            summary = reconcile_zoho_submissions(app.config)

        assert summary["orphan_payments"] == []
