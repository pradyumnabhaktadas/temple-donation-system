"""Tests for Admin -> Import Zoho Report.

The manual counterpart to automatic reconciliation: export a form's report
from Zoho, drop it here, and the rows representing real captured payments
become receipts. It exists because the Zoho webhook has to be configured
per form and often isn't -- four of the six forms taking money in
September 2026 never were -- while a report export covers every form and
reaches back through history.

The file fixtures below are the real export shape from this account,
including the parts that trip things up: "First, Last" names, the combined
"Txn ID : pay_X Order ID : order_Y" string, cash rows with no payment at
all, and Zoho's own Payment Status being unreliable in both directions.
"""
import io
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import login

URL = "/admin/donations/import-zoho"

HEADER = "Added Time,Name,Phone,Mode of Payment,Payment Amount,Payment Status,Payment Transaction ID"

ROW_PAID = (
    '04-Sep-2026 21:55:22,"shivam, raj",919319880507,Online (UPI),100,Completed,'
    '"Txn ID : pay_TY1ckLpy6lUDMr Order ID : order_TY1bS1x9eSe5tb"'
)
ROW_CASH = '08-Jul-2026 20:09:36,"Divyansh, Narang",919818359809,Offline (Cash),,Processing not needed,'
ROW_FAILED = (
    '07-Jul-2026 19:28:50,"Harshit, Prajapati",919220830216,Online (UPI),100,Failed,'
    '"Txn ID : pay_FailedOne Order ID : order_FailedOne"'
)


def _csv(*rows):
    return (HEADER + "\n" + "\n".join(rows) + "\n").encode()


def _razorpay(captured=True):
    client = MagicMock()
    client.payment.fetch.side_effect = lambda pid: {
        "id": pid, "status": "captured" if captured else "authorized"
    }
    return patch("razorpay.Client", return_value=client)


def _upload(client, app, body, action="import", campaign_name="BACE Contribution", **extra):
    from models import Campaign
    app.config["RAZORPAY_ENABLED"] = True
    campaign = Campaign.query.filter_by(name=campaign_name).first()
    data = {
        "report_file": (io.BytesIO(body), "report.csv"),
        "campaign_id": str(campaign.id),
        "action": action,
    }
    data.update(extra)
    return client.post(URL, data=data, content_type="multipart/form-data", follow_redirects=True)


@pytest.fixture(autouse=True)
def _logged_in(client):
    login(client)


class TestImporting:
    def test_a_paid_row_becomes_a_donation_with_a_receipt(self, client, app):
        from models import Donation

        with _razorpay():
            resp = _upload(client, app, _csv(ROW_PAID))

        assert resp.status_code == 200
        donation = Donation.query.one()
        assert donation.donor.full_name == "Shivam Raj"
        assert donation.razorpay_payment_id == "pay_TY1ckLpy6lUDMr"
        assert donation.razorpay_order_id == "order_TY1bS1x9eSe5tb"
        assert float(donation.amount) == 100.0
        assert donation.receipt_number
        assert donation.status == "success"

    def test_a_cash_row_is_skipped_as_ordinary_not_flagged_as_an_error(self, client, app):
        """The commonest row in these exports. Someone registering to pay
        in cash has no Razorpay payment and never will."""
        from models import Donation

        with _razorpay():
            resp = _upload(client, app, _csv(ROW_CASH))

        assert Donation.query.count() == 0
        assert b"No payment on this row" in resp.data

    def test_a_row_razorpay_will_not_confirm_is_not_receipted(self, client, app):
        """Zoho's own status is not consulted -- Razorpay is asked, and
        here it declines."""
        from models import Donation

        with _razorpay(captured=False):
            resp = _upload(client, app, _csv(ROW_FAILED))

        assert Donation.query.count() == 0
        assert b"captured" in resp.data

    def test_a_mixed_file_imports_only_what_it_should(self, client, app):
        from models import Donation

        with _razorpay():
            _upload(client, app, _csv(ROW_PAID, ROW_CASH))

        assert Donation.query.count() == 1
        assert Donation.query.one().razorpay_payment_id == "pay_TY1ckLpy6lUDMr"

    def test_notifications_are_off_unless_asked_for(self, client, app):
        """A donor who gave six weeks ago shouldn't get a receipt reading
        as though it just happened."""
        import public

        with _razorpay(), patch.object(
            public, "_send_receipt_notifications_background"
        ) as send:
            _upload(client, app, _csv(ROW_PAID))
        assert not send.called

    def test_notifications_are_sent_when_asked_for(self, client, app):
        import public

        with _razorpay(), patch.object(
            public, "_send_receipt_notifications_background"
        ) as send:
            _upload(client, app, _csv(ROW_PAID), send_notifications="yes")
        assert send.called


class TestReUploadingIsSafe:
    """Re-uploading an overlapping export is the normal way to use this --
    exports are per-form and per-period, and they overlap. It must never
    produce a second receipt for one payment."""

    def test_the_same_file_twice_creates_one_donation(self, client, app):
        from models import Donation

        with _razorpay():
            _upload(client, app, _csv(ROW_PAID))
            resp = _upload(client, app, _csv(ROW_PAID))

        assert Donation.query.count() == 1
        assert b"Already recorded" in resp.data

    def test_a_payment_backfilled_by_hand_is_recognised(self, client, app):
        """September's 21 backfills carry their reference in
        bank_transaction_id, not razorpay_payment_id. Matching only the
        latter would re-import every one of them as a duplicate."""
        from extensions import db
        from models import Campaign, Donation, Donor

        donor = Donor(full_name="Shivam Raj", phone="9319880507")
        db.session.add(donor)
        db.session.flush()
        db.session.add(Donation(
            donor_id=donor.id, campaign_id=Campaign.query.first().id, amount=100,
            status="success", payment_mode="online",
            bank_transaction_id="pay_TY1ckLpy6lUDMr",
        ))
        db.session.commit()

        with _razorpay():
            _upload(client, app, _csv(ROW_PAID))

        assert Donation.query.count() == 1


class TestPreview:
    def test_preview_writes_nothing(self, client, app):
        from models import Donation

        with _razorpay():
            resp = _upload(client, app, _csv(ROW_PAID), action="preview")

        assert Donation.query.count() == 0
        assert b"Would import" in resp.data
        assert b"nothing has been saved" in resp.data


class TestRefusals:
    def test_a_file_with_no_campaign_chosen_is_refused(self, client, app):
        from models import Donation

        app.config["RAZORPAY_ENABLED"] = True
        resp = client.post(URL, data={
            "report_file": (io.BytesIO(_csv(ROW_PAID)), "report.csv"),
            "campaign_id": "",
            "action": "import",
        }, content_type="multipart/form-data", follow_redirects=True)

        assert Donation.query.count() == 0
        assert resp.status_code == 200

    def test_no_file_is_refused(self, client, app):
        resp = client.post(URL, data={"action": "import"},
                           content_type="multipart/form-data", follow_redirects=True)
        assert resp.status_code == 200
        assert b"choose the Zoho report" in resp.data

    def test_staff_cannot_reach_it(self, client, app):
        """Issuing receipts in bulk is an admin action, like every other
        import in this app."""
        client.get("/admin/logout", follow_redirects=True)
        login(client, username="teststaff")
        resp = client.get(URL, follow_redirects=True)
        assert b"Import Zoho Report" not in resp.data or resp.status_code in (302, 403)
