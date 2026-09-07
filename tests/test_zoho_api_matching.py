"""Reconciliation asks Zoho's API who made a captured payment.

This is the only place a donor's name comes from now. The Zoho webhook is
gone: it fired once, at submission time, *before* the donor paid, so it
could never report a payment; the follow-up call Zoho's docs promise
never arrived on this account; and it had to be configured per form, which
four of the six forms taking money never were.

The local mirror of those calls is gone with it. It only ever held calls
received after the day it was deployed, which left real money stranded:

    pay_TYwWNlMsUDiq8W  Rs. 100  +917837679976
    pay_TYkwQc4pqV9HNu  Rs. 100  +919650150283
    pay_TYkDaxITUonK1O  Rs. 100  +919650150283

all three captured, all three on a form correctly mapped to a campaign,
all three reported as unnameable. Nothing the temple could do on the
settings page would have fixed them -- the record simply did not exist
yet. Zoho's API knows every one of them, and knows them for every form,
however its webhook is configured.

The matching rule is unchanged and is the whole point: the transaction ID
and nothing else. No phone, no amount, no timing, at any point.
"""
import datetime
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _payment(payment_id, amount, contact, form="EssenceofBhagavadGitaOnlyForBOYSP"):
    created = datetime.datetime.utcnow() - datetime.timedelta(minutes=30)
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


def _razorpay(payments, fetch_status="captured"):
    client = MagicMock()
    client.payment.all.return_value = {"items": list(payments), "count": len(payments)}
    client.payment.fetch.side_effect = lambda pid: {"id": pid, "status": fetch_status}
    return patch("razorpay.Client", return_value=client)


def _entry(name, phone, payment_id, amount="100", status="Completed"):
    """One Zoho entry in the shape this account's API actually returns --
    the composite Name control as "First, Last", and the transaction as
    the combined "Txn ID : ... Order ID : ..." string."""
    return {
        "Name": name,
        "Phone": phone,
        "Payment Amount": amount,
        "Payment Status": status,
        "Payment Transaction ID": (
            f"Txn ID : {payment_id} Order ID : {payment_id.replace('pay_', 'order_')}"
        ),
        "Added Time": "04-Sep-2026 21:55:22",
    }


def _zoho_api(entries, error=None):
    """Stands in for zoho_api.entries(). Returns one page then stops."""
    def _fake(config, link_name, start_index=1, limit=200):
        if error is not None:
            raise error
        return (list(entries) if start_index == 1 else []), "records"
    return patch("zoho_api.entries", side_effect=_fake)


def _form(app, form_key="EssenceofBhagavadGitaOnlyForBOYSP",
          campaign_name="BACE Contribution", is_test=False, link_name=None):
    from extensions import db
    from models import Campaign, ZohoForm
    campaign = Campaign.query.filter_by(name=campaign_name).first()
    entry = ZohoForm(form_key=form_key, link_name=link_name,
                     campaign_id=campaign.id if campaign else None, is_test=is_test)
    db.session.add(entry)
    db.session.commit()
    app.config["RAZORPAY_ENABLED"] = True
    return entry


def _reconcile(app, payments):
    from public import reconcile_zoho_submissions
    with _razorpay(payments):
        return reconcile_zoho_submissions(app.config)


@pytest.fixture(autouse=True)
def _zoho_configured(app):
    app.config.update(
        ZOHO_CLIENT_ID="cid", ZOHO_CLIENT_SECRET="csec", ZOHO_REFRESH_TOKEN="rtok",
        RAZORPAY_ENABLED=True,
    )


class TestTheThreePaymentsThatWereStuck:
    """The exact production case, replayed."""

    def test_a_payment_the_app_never_heard_about_is_receipted(self, client, app):
        """Nothing local ever recorded this payment. Razorpay says it was
        captured; Zoho says who made it; that is enough."""
        from models import Donation

        _form(app)

        with _zoho_api([_entry("Jatin, saini", "9650150283", "pay_TYkwQc4pqV9HNu")]):
            summary = _reconcile(app, [_payment("pay_TYkwQc4pqV9HNu", 100, "+919650150283")])

        assert len(summary["created"]) == 1
        donation = Donation.query.one()
        assert donation.donor.full_name == "Jatin Saini"
        assert donation.razorpay_payment_id == "pay_TYkwQc4pqV9HNu"
        assert donation.receipt_number

    def test_one_donors_two_payments_each_get_their_own_receipt(self, client, app):
        """Jatin Saini paid twice from one number for the same amount. The
        old phone-and-amount matching could not tell that from one payment
        made twice; on the transaction ID it is not even a hard case."""
        from models import Donation

        _form(app)
        entries = [
            _entry("Jatin, saini", "9650150283", "pay_TYkwQc4pqV9HNu"),
            _entry("Jatin, saini", "9650150283", "pay_TYkDaxITUonK1O"),
        ]
        with _zoho_api(entries):
            summary = _reconcile(app, [
                _payment("pay_TYkwQc4pqV9HNu", 100, "+919650150283"),
                _payment("pay_TYkDaxITUonK1O", 100, "+919650150283"),
            ])

        assert len(summary["created"]) == 2
        assert Donation.query.count() == 2
        assert {d.razorpay_payment_id for d in Donation.query.all()} == {
            "pay_TYkwQc4pqV9HNu", "pay_TYkDaxITUonK1O",
        }
        assert len({d.receipt_number for d in Donation.query.all()}) == 2


class TestTheMatchIsStillTheTransactionIdAndNothingElse:
    def test_an_entry_matching_on_phone_and_amount_but_not_id_is_refused(self, client, app):
        """Same donor, same number, same amount, same day -- and a
        different transaction. Not a match. This is the whole rule."""
        from models import Donation

        _form(app)
        with _zoho_api([_entry("Jatin, saini", "9650150283", "pay_SomethingElse")]):
            summary = _reconcile(app, [_payment("pay_TYkwQc4pqV9HNu", 100, "+919650150283")])

        assert Donation.query.count() == 0
        assert summary["created"] == []
        assert "no entry carrying this transaction id" in \
            summary["orphan_payments"][0]["why_not_receipted"]

    def test_the_amount_is_razorpays_not_zohos(self, client, app):
        """Zoho carries what the form asked for; Razorpay carries what left
        the donor's account. The receipt has to state the latter."""
        from models import Donation

        _form(app)
        with _zoho_api([_entry("shivam, raj", "9319880507", "pay_Amt9", amount="100")]):
            _reconcile(app, [_payment("pay_Amt9", 250, "+919319880507")])

        assert float(Donation.query.one().amount) == 250.0

class TestFailingTowardsReporting:
    def test_an_unreachable_zoho_is_reported_never_read_as_no_entry(self, client, app):
        """The distinction that matters most in this whole feature. "Zoho
        was down" and "Zoho has no such entry" must not look the same --
        reading the first as the second is how payments went missing."""
        from models import Donation
        import zoho_api

        _form(app)
        with _zoho_api([], error=zoho_api.ZohoApiError("Zoho is unreachable")):
            summary = _reconcile(app, [_payment("pay_Down1", 100, "+919650150283")])

        assert Donation.query.count() == 0
        why = summary["orphan_payments"][0]["why_not_receipted"]
        assert "couldn't ask Zoho" in why and "unreachable" in why

    def test_an_unconfigured_zoho_api_reports_rather_than_crashing(self, client, app):
        """Nothing breaks on a deployment that never set the credentials --
        it just goes back to reporting, as it did before."""
        from models import Donation

        app.config.update(ZOHO_CLIENT_ID="", ZOHO_CLIENT_SECRET="", ZOHO_REFRESH_TOKEN="")
        _form(app)

        summary = _reconcile(app, [_payment("pay_NoCreds1", 100, "+919650150283")])

        assert Donation.query.count() == 0
        assert "isn't configured" in summary["orphan_payments"][0]["why_not_receipted"]

    def test_an_unexpected_response_shape_does_not_end_the_run(self, client, app):
        from models import Donation

        _form(app)
        with _zoho_api([], error=TypeError("unexpected shape")):
            summary = _reconcile(app, [_payment("pay_Shape1", 100, "+919650150283")])

        assert Donation.query.count() == 0
        assert summary["error"] is None
        assert summary["orphan_payments"]

    def test_an_entry_with_no_name_is_reported_not_receipted_blank(self, client, app):
        from models import Donation

        _form(app)
        with _zoho_api([_entry("", "9650150283", "pay_NoName1")]):
            summary = _reconcile(app, [_payment("pay_NoName1", 100, "+919650150283")])

        assert Donation.query.count() == 0
        assert "no name" in summary["orphan_payments"][0]["why_not_receipted"]

    def test_a_test_form_is_never_asked_about_at_all(self, client, app):
        _form(app, form_key="TESTFORM", is_test=True)

        with patch("zoho_api.entries") as api:
            _reconcile(app, [_payment("pay_Test1", 100, "+919650150283", form="TESTFORM")])

        assert api.call_count == 0

    def test_a_form_with_no_campaign_is_never_asked_about_either(self, client, app):
        _form(app, campaign_name="__nonexistent__")

        with patch("zoho_api.entries") as api:
            _reconcile(app, [_payment("pay_NoCamp1", 100, "+919650150283")])

        assert api.call_count == 0


class TestOneApiReadPerFormPerRun:
    def test_ten_payments_on_one_form_cost_one_lookup(self, client, app):
        """Not one call per payment. This runs every half hour."""
        from models import Donation

        _form(app)
        entries = [_entry("Donor, one", f"90000000{n:02d}", f"pay_Many{n}") for n in range(10)]
        payments = [_payment(f"pay_Many{n}", 100, f"+9190000000{n:02d}") for n in range(10)]

        with _zoho_api(entries) as api:
            summary = _reconcile(app, payments)

        assert len(summary["created"]) == 10
        assert Donation.query.count() == 10
        assert api.call_count == 1

    def test_the_link_name_is_used_when_it_differs_from_razorpays_name(self, client, app):
        """Razorpay's form name and Zoho's API link name are the same
        string on these forms, but they don't have to be."""
        _form(app, link_name="EBG_Registration_v2")

        with _zoho_api([]) as api:
            _reconcile(app, [_payment("pay_Link1", 100, "+919650150283")])

        assert api.call_args[0][1] == "EBG_Registration_v2"


class TestFormsAddThemselves:
    """Copying a form name out of a log by hand is a typo waiting to
    happen, and a typo here is silent: the form stays "not set up" while
    everyone believes it's configured."""

    def test_an_unknown_form_is_added_automatically(self, client, app):
        from models import ZohoForm

        _reconcile(app, [_payment("pay_New1", 100, "+917982836653", form="IGFForm")])

        assert ZohoForm.query.filter_by(form_key="IGFForm").one().form_key == "IGFForm"

    def test_it_is_added_with_no_campaign_so_it_receipts_nothing(self, client, app):
        """The one judgement a person still has to make. Guessing it would
        file donations under the wrong campaign and put the wrong 80G
        status on a tax receipt."""
        from models import Donation, ZohoForm

        _reconcile(app, [_payment("pay_New2", 100, "+917982836653", form="IGFForm")])

        form = ZohoForm.query.filter_by(form_key="IGFForm").one()
        assert form.campaign_id is None
        assert form.can_receipt is False
        assert Donation.query.count() == 0

    def test_the_report_says_what_is_left_to_do(self, client, app):
        summary = _reconcile(app, [_payment("pay_New3", 100, "+917982836653", form="IGFForm")])

        why = summary["orphan_payments"][0]["why_not_receipted"]
        assert "no campaign set" in why and "Admin > Zoho Forms" in why

    def test_it_is_added_once_not_once_per_payment(self, client, app):
        from models import ZohoForm

        _reconcile(app, [
            _payment("pay_New4", 100, "+917982836653", form="IGFForm"),
            _payment("pay_New5", 100, "+917982836654", form="IGFForm"),
        ])

        assert ZohoForm.query.filter_by(form_key="IGFForm").count() == 1

    def test_it_is_recorded_in_the_activity_log(self, client, app):
        from models import AdminActivityLog

        _reconcile(app, [_payment("pay_New6", 100, "+917982836653", form="IGFForm")])

        entry = AdminActivityLog.query.filter_by(action="zoho_form_discovered").one()
        assert "IGFForm" in entry.details

    def test_a_website_payment_never_creates_a_form(self, client, app):
        from models import ZohoForm

        pay = _payment("pay_Web1", 100, "+919000000001")
        pay["notes"] = {"donation_id": "999", "campaign": "Annadan"}
        _reconcile(app, [pay])

        assert ZohoForm.query.count() == 0
