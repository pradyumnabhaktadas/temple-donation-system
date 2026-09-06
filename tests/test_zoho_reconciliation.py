"""Tests for reconciliation: turning Razorpay's record of captured
payments into donations and receipts.

Zoho's webhook fires once, at submission time, before the donor pays, so
it usually carries no transaction ID -- and on this account the follow-up
call it promises frequently never arrives. It also has to be configured
per form, and four of the six forms taking money in September 2026 never
were, so those donations were invisible here entirely.

Reconciliation doesn't depend on any of that. Razorpay knows which
payments were captured and which have no donation behind them; the Google
Sheet Zoho writes every submission into supplies the donor's name. Neither
source is asked a question it can't answer -- see zoho_sheet for why that
direction round is the safe one.

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
    """Zoho's pre-payment call: the whole form, no transaction ID.

    Acknowledged and forgotten now -- nothing is stored, because
    reconciliation finds the payment in Razorpay and names the donor from
    the sheet regardless. Kept here to assert exactly that.

    Defaults to a Non-80G campaign because that is what these forms are:
    event registrations collecting a fee and no PAN."""
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


def _setup_form(app, form_key="EBG", campaign_name="BACE Contribution",
                sheet_url="https://docs.google.com/x/pub?output=csv", is_test=False):
    """A configured Zoho form, as Admin -> Zoho Forms would create it."""
    from extensions import db
    from models import Campaign, ZohoForm
    campaign = Campaign.query.filter_by(name=campaign_name).first()
    entry = ZohoForm(
        form_key=form_key, campaign_id=campaign.id if campaign else None,
        sheet_csv_url=sheet_url, is_test=is_test,
    )
    db.session.add(entry)
    db.session.commit()
    app.config["RAZORPAY_ENABLED"] = True
    return entry


def _reconcile(app, payments, **kwargs):
    from public import reconcile_zoho_submissions
    with _razorpay(payments, fetch_status=kwargs.pop("fetch_status", "captured")):
        return reconcile_zoho_submissions(app.config, **kwargs)




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


class TestIgnoredTestForms:
    """A test form charges real money through the live gateway, so its
    payments look exactly like donations and would sit in the report
    forever. A report carrying permanent known-noise is one people stop
    reading -- which is how the original failures went unnoticed for
    weeks. Filtered out, but never silently."""

    def _test_payment(self):
        p = _payment("pay_TX5LUCwylz2UWo", 10, "9650150283")
        p["notes"] = {"zform_custom": "iskcondwarka,TestingWebsitewithFormsintergration,tok"}
        return p

    def _real_payment(self):
        p = _payment("pay_TY1ckLpy6lUDMr", 100, "9319880507")
        p["notes"] = {"zform_custom": "iskcondwarka,EssenceofBhagavadGitaOnlyForBOYSP,tok"}
        return p

    def test_a_listed_form_is_kept_out_of_the_actionable_list(self, client, app):
        from public import reconcile_zoho_submissions

        _setup_form(app, form_key="TestingWebsitewithFormsintergration", is_test=True)
        _setup_form(app, form_key="EssenceofBhagavadGitaOnlyForBOYSP")

        with _razorpay([self._test_payment(), self._real_payment()]):
            summary = reconcile_zoho_submissions(app.config)

        assert [p["payment_id"] for p in summary["orphan_payments"]] == ["pay_TY1ckLpy6lUDMr"]

    def test_it_is_reported_separately_rather_than_disappearing(self, client, app):
        """A form listed here by mistake must show up as a suspiciously
        busy 'ignored' line, not vanish."""
        from public import reconcile_zoho_submissions

        _setup_form(app, form_key="TestingWebsitewithFormsintergration", is_test=True)

        with _razorpay([self._test_payment()]):
            summary = reconcile_zoho_submissions(app.config)

        assert [p["payment_id"] for p in summary["ignored_payments"]] == ["pay_TX5LUCwylz2UWo"]

    def test_an_ignored_payment_never_becomes_a_receipt(self, client, app):
        """The consequential half: ignoring it only in the report would
        leave the sheet lookup free to turn a test charge into a real tax
        receipt for whoever the sheet names."""
        from models import Donation
        from public import reconcile_zoho_submissions

        _setup_form(app, form_key="TestingWebsitewithFormsintergration", is_test=True)

        sheet = ("Name,Phone,Payment Amount\n"
                 '"Jatin, saini",919650150283,10\n')

        pay = _payment("pay_TX5LUCwylz2UWo", 10, "9650150283")
        pay["notes"] = {"zform_custom": "iskcondwarka,TestingWebsitewithFormsintergration,tok"}

        with patch("zoho_sheet.requests.get",
                   return_value=MagicMock(status_code=200, text=sheet)), \
             _razorpay([pay]):
            summary = reconcile_zoho_submissions(app.config)

        assert Donation.query.count() == 0
        assert summary["created"] == []

    def test_matching_is_case_and_space_insensitive(self, client, app):
        from public import reconcile_zoho_submissions

        _setup_form(app, form_key="TestingWebsitewithFormsintergration", is_test=True)

        with _razorpay([self._test_payment()]):
            summary = reconcile_zoho_submissions(app.config)

        assert summary["orphan_payments"] == []
        assert len(summary["ignored_payments"]) == 1

    def test_nothing_is_ignored_when_the_setting_is_empty(self, client, app):
        """The default. An unset ignore-list must not accidentally filter."""
        from public import reconcile_zoho_submissions

        _setup_form(app, form_key="TestingWebsitewithFormsintergration")   # not marked test
        _setup_form(app, form_key="EssenceofBhagavadGitaOnlyForBOYSP")

        with _razorpay([self._test_payment(), self._real_payment()]):
            summary = reconcile_zoho_submissions(app.config)

        assert len(summary["orphan_payments"]) == 2
        assert summary["ignored_payments"] == []

    def test_a_website_payment_is_unaffected_by_the_list(self, client, app):
        """The list names Zoho forms. A website payment has no form name,
        and must not be caught by a stray empty match."""
        from public import unreconciled_razorpay_payments

        _setup_form(app, form_key="TestingWebsitewithFormsintergration", is_test=True)
        pay = _payment("pay_Web9", 100, "9000000009")
        pay["notes"] = {"donation_id": "9999", "campaign": "Annadan"}

        with _razorpay([pay]):
            payments, _ = unreconciled_razorpay_payments(app.config)

        assert payments[0]["ignored"] is False


SUBMISSIONS_SHEET = """Added Time,Name,Phone,Mode of Payment,Payment Amount,Payment Status
04-Sep-2026 21:55:22,"shivam, raj",919319880507,Online (UPI),100,Completed
06-Sep-2026 17:33:15,"Jatin, saini",919650150283,Online (UPI),100,Completed
04-Sep-2026 16:06:00,"Kshitij, Kansal",919023555958,Online (UPI),1100,Completed
"""


class TestAutomaticReceiptsFromTheSubmissionsSheet:
    """The end state: nobody uploads anything, nobody configures a webhook
    per form, and a donation that Zoho never told us about still becomes a
    receipt on its own.

    Razorpay supplies the payment -- id, amount, phone, and the form name
    in its notes. The Google Sheet that Zoho writes every submission into
    supplies the one thing Razorpay lacks, the donor's name. Neither
    source is asked a question it can't answer."""

    def _sheet(self, text=SUBMISSIONS_SHEET, status=200):
        return patch("zoho_sheet.requests.get", return_value=MagicMock(status_code=status, text=text))

    def _configure(self, app, form_key="EssenceofBhagavadGitaOnlyForBOYSP",
                   campaign_name="BACE Contribution"):
        _setup_form(app, form_key=form_key, campaign_name=campaign_name)

    def _zoho_payment(self, payment_id, amount, contact,
                      form="EssenceofBhagavadGitaOnlyForBOYSP"):
        """A captured payment carrying the Zoho form name in its notes,
        exactly as production does."""
        pay = _payment(payment_id, amount, contact)
        pay["notes"] = {"zform_custom": f"iskcondwarka,{form},tok"}
        return pay

    def test_a_payment_zoho_never_reported_becomes_a_receipt_by_itself(self, client, app):
        from models import Donation
        from public import reconcile_zoho_submissions

        self._configure(app)
        with self._sheet(), _razorpay([self._zoho_payment("pay_TY1ckLpy6lUDMr", 100, "+919319880507")]):
            summary = reconcile_zoho_submissions(app.config)

        assert len(summary["created"]) == 1
        donation = Donation.query.one()
        assert donation.donor.full_name == "Shivam Raj"
        assert donation.razorpay_payment_id == "pay_TY1ckLpy6lUDMr"
        assert donation.receipt_number
        assert float(donation.amount) == 100.0
        assert summary["orphan_payments"] == [], "nothing left needing a human"

    def test_the_amount_comes_from_razorpay_not_the_sheet(self, client, app):
        """Razorpay is what actually moved the money, and the receipt has
        to state what was received."""
        from models import Donation
        from public import reconcile_zoho_submissions

        self._configure(app)
        with self._sheet(), _razorpay([self._zoho_payment("pay_Big1", 1100, "+919023555958")]):
            reconcile_zoho_submissions(app.config)

        assert float(Donation.query.one().amount) == 1100.0

    def test_an_unmapped_form_is_reported_never_filed_somewhere_arbitrary(self, client, app):
        """Filing a donation under the wrong campaign quietly corrupts
        every report built on those figures."""
        from models import Donation
        from public import reconcile_zoho_submissions

        self._configure(app, form_key="SomeOtherForm")
        with self._sheet(), _razorpay([self._zoho_payment("pay_Unmapped1", 100, "+919319880507")]):
            summary = reconcile_zoho_submissions(app.config)

        assert Donation.query.count() == 0
        assert "isn't set up" in summary["orphan_payments"][0]["why_not_receipted"]

    def test_a_payer_the_sheet_does_not_know_is_reported_with_the_reason(self, client, app):
        from models import Donation
        from public import reconcile_zoho_submissions

        self._configure(app)
        with self._sheet(), _razorpay([self._zoho_payment("pay_Stranger1", 100, "+919999999999")]):
            summary = reconcile_zoho_submissions(app.config)

        assert Donation.query.count() == 0
        assert "no submission" in summary["orphan_payments"][0]["why_not_receipted"]

    def test_an_unreadable_sheet_reports_and_receipts_nothing(self, client, app):
        """A failed fetch must never be read as "no submissions" -- that
        would silently stop every automatic receipt while looking healthy."""
        from models import Donation
        from public import reconcile_zoho_submissions

        self._configure(app)
        with self._sheet(text="<html>Sign in</html>"), \
             _razorpay([self._zoho_payment("pay_Sheet1", 100, "+919319880507")]):
            summary = reconcile_zoho_submissions(app.config)

        assert Donation.query.count() == 0
        assert summary["sheet_error"]
        assert len(summary["orphan_payments"]) == 1

    def test_nothing_changes_when_the_sheet_is_not_configured(self, client, app):
        """Existing behaviour has to survive: report, don't receipt."""
        from models import Donation
        from public import reconcile_zoho_submissions

        app.config["RAZORPAY_ENABLED"] = True   # no ZohoForm rows at all
        with _razorpay([self._zoho_payment("pay_NoSheet1", 100, "+919319880507")]):
            summary = reconcile_zoho_submissions(app.config)

        assert Donation.query.count() == 0
        assert len(summary["orphan_payments"]) == 1

    def test_an_already_receipted_payment_is_not_receipted_again(self, client, app):
        from models import Donation
        from public import reconcile_zoho_submissions

        self._configure(app)
        payment = self._zoho_payment("pay_Twice1", 100, "+919319880507")
        with self._sheet(), _razorpay([payment]):
            reconcile_zoho_submissions(app.config)
        with self._sheet(), _razorpay([payment]):
            reconcile_zoho_submissions(app.config)

        assert Donation.query.count() == 1

    def test_a_test_form_is_still_never_receipted(self, client, app):
        from models import Donation
        from public import reconcile_zoho_submissions

        _setup_form(app, form_key="TestingWebsitewithFormsintergration", is_test=True)
        pay = self._zoho_payment("pay_TestForm2", 10, "+919650150283",
                            form="TestingWebsitewithFormsintergration")
        with self._sheet(), _razorpay([pay]):
            reconcile_zoho_submissions(app.config)

        assert Donation.query.count() == 0


class TestImmediateReceiptOnRazorpayWebhook:
    """The receipt should land while the donor is still looking at the
    confirmation screen, not up to an hour later.

    Zoho's own webhook fires *before* the donor pays, so it can never tell
    us a payment succeeded. Razorpay can, within seconds, on a channel
    already configured and already signature-verified. This runs the same
    sheet lookup the hourly job runs -- one code path, two triggers."""

    SECRET = "wh-secret"
    SHEET = ("Name,Phone,Payment Amount\n"
             '"shivam, raj",919319880507,100\n')

    def _post_captured(self, client, app, payment_id, amount, contact, notes=None):
        import hashlib
        import hmac as hmac_mod
        import json as json_mod

        app.config["RAZORPAY_WEBHOOK_SECRET"] = self.SECRET
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
        sig = hmac_mod.new(self.SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
        return client.post(
            "/webhooks/razorpay", data=body,
            headers={"Content-Type": "application/json", "X-Razorpay-Signature": sig},
        )

    def _configure(self, app):
        _setup_form(app, form_key="EBG")

    def _sheet(self, text=None):
        return patch("zoho_sheet.requests.get",
                     return_value=MagicMock(status_code=200, text=text or self.SHEET))

    def test_the_receipt_is_issued_the_moment_the_payment_lands(self, client, app):
        from models import Donation

        self._configure(app)
        with self._sheet():
            resp = self._post_captured(client, app, "pay_TY1ckLpy6lUDMr", 100, "+919319880507")

        assert resp.status_code == 200
        assert resp.get_json()["matched"] == "zoho_submissions_sheet"
        donation = Donation.query.one()
        assert donation.donor.full_name == "Shivam Raj"
        assert donation.receipt_number
        assert donation.razorpay_payment_id == "pay_TY1ckLpy6lUDMr"

    def test_a_payer_the_sheet_cannot_name_is_left_for_the_hourly_job(self, client, app):
        from models import Donation

        self._configure(app)
        with self._sheet():
            resp = self._post_captured(client, app, "pay_Stranger1", 100, "+919999999999")

        assert resp.status_code == 200
        assert resp.get_json()["matched"] is False
        assert Donation.query.count() == 0

    def test_an_unreadable_sheet_does_not_break_the_webhook(self, client, app):
        """Razorpay retries a non-2xx. This is a best-effort accelerator,
        so a sheet problem must acknowledge and leave it to the sweep."""
        from models import Donation

        self._configure(app)
        with self._sheet(text="<html>Sign in</html>"):
            resp = self._post_captured(client, app, "pay_SheetDown1", 100, "+919319880507")

        assert resp.status_code == 200
        assert Donation.query.count() == 0

    def test_a_test_form_gets_no_instant_receipt_either(self, client, app):
        from models import Donation

        self._configure(app)
        with self._sheet():
            self._post_captured(
                client, app, "pay_TestForm3", 100, "+919319880507",
                notes={"zform_custom": "iskcondwarka,TestingWebsitewithFormsintergration,tok"},
            )

        assert Donation.query.count() == 0

    def test_a_redelivered_webhook_does_not_receipt_twice(self, client, app):
        from models import Donation

        self._configure(app)
        with self._sheet():
            self._post_captured(client, app, "pay_Redeliver9", 100, "+919319880507")
            self._post_captured(client, app, "pay_Redeliver9", 100, "+919319880507")

        assert Donation.query.count() == 1

    def test_this_sites_own_checkout_is_untouched(self, client, app):
        """The pre-existing path must still win: a payment whose order
        matches a donation is finalised as before, never diverted."""
        from extensions import db
        from models import Campaign, Donation, Donor

        donor = Donor(full_name="Site Donor", phone="9319880507")
        db.session.add(donor)
        db.session.flush()
        db.session.add(Donation(
            donor_id=donor.id, campaign_id=Campaign.query.first().id, amount=100,
            status="pending", payment_mode="online", razorpay_order_id="order_SiteFlow9",
        ))
        db.session.commit()

        self._configure(app)
        with self._sheet():
            resp = self._post_captured(client, app, "pay_SiteFlow9", 100, "+919319880507")

        assert resp.status_code == 200
        body = resp.get_json()
        assert body.get("receipt_number") and body.get("matched") != "zoho_submissions_sheet"
        assert Donation.query.count() == 1


class TestHistoricalScan:
    """A fixed date range is for auditing an old period. It must report
    and change nothing -- pending submissions only exist from the day this
    was deployed, so matching them against, say, August would find no
    candidates for any of them and close every open one as unpaid."""

    def test_a_historical_scan_changes_nothing(self, client, app):
        """A dated scan is for auditing an old period, so it reports and
        creates nothing -- even where the sheet could name the payer."""
        from models import Donation
        from public import reconcile_zoho_submissions

        _setup_form(app, form_key="EBG")
        sheet = ("Name,Phone,Payment Amount\n"
                 '"shivam, raj",919000000001,100\n')

        pay = _payment("pay_Hist1", 100, "9000000001")
        pay["notes"] = {"zform_custom": "iskcondwarka,EBG,tok"}

        with patch("zoho_sheet.requests.get",
                   return_value=MagicMock(status_code=200, text=sheet)), \
             _razorpay([pay]):
            summary = reconcile_zoho_submissions(
                app.config,
                from_date=datetime.date(2026, 8, 1), to_date=datetime.date(2026, 8, 31),
            )

        assert summary["report_only"] is True
        assert summary["created"] == []
        assert Donation.query.count() == 0, \
            "a historical audit must never write, however confidently it could"

    def test_it_still_reports_the_payments_it_found(self, client, app):
        from public import reconcile_zoho_submissions

        app.config["RAZORPAY_ENABLED"] = True
        with _razorpay([_payment("pay_Hist2", 501, "9758517155")]):
            summary = reconcile_zoho_submissions(
                app.config, from_date=datetime.date(2026, 8, 1),
            )

        assert [p["payment_id"] for p in summary["orphan_payments"]] == ["pay_Hist2"]

    def test_the_route_accepts_a_date_range(self, client, app):
        app.config["INTERNAL_TASK_TOKEN"] = "secret-token"
        app.config["RAZORPAY_ENABLED"] = True

        with _razorpay([_payment("pay_Hist3", 100, "9000000002")]):
            resp = client.post(
                "/internal/zoho-reconcile",
                json={"from_date": "2026-08-01", "to_date": "2026-08-31"},
                headers={"X-Internal-Token": "secret-token"},
            )

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["report_only"] is True
        assert body["created"] == []
        assert [p["payment_id"] for p in body["orphan_payments"]] == ["pay_Hist3"]

    def test_a_malformed_date_is_rejected(self, client, app):
        app.config["INTERNAL_TASK_TOKEN"] = "secret-token"
        resp = client.post(
            "/internal/zoho-reconcile", json={"from_date": "01-08-2026"},
            headers={"X-Internal-Token": "secret-token"},
        )
        assert resp.status_code == 400


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


class TestDailyReportSafetyNet:
    """Reconciliation also runs from the daily report, because this
    project's cron jobs have silently failed for days at a time before."""

    def test_the_daily_report_runs_reconciliation(self, client, app):
        from daily_report_utils import run_reconciliation_safely
        from models import Donation

        _setup_form(app, form_key="EBG")
        sheet = ("Name,Phone,Payment Amount\n"
                 '"shivam, raj",919625901202,100\n')

        pay = _payment("pay_Daily1", 100, "9625901202")
        pay["notes"] = {"zform_custom": "iskcondwarka,EBG,tok"}

        with patch("zoho_sheet.requests.get",
                   return_value=MagicMock(status_code=200, text=sheet)), \
             _razorpay([pay]):
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
        app.config["INTERNAL_TASK_TOKEN"] = "secret-token"
        _setup_form(app, form_key="EBG")
        sheet = ("Name,Phone,Payment Amount\n"
                 '"shivam, raj",919625901202,100\n')

        pay = _payment("pay_Route1", 100, "9625901202")
        pay["notes"] = {"zform_custom": "iskcondwarka,EBG,tok"}

        with patch("zoho_sheet.requests.get",
                   return_value=MagicMock(status_code=200, text=sheet)), \
             _razorpay([pay]):
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
