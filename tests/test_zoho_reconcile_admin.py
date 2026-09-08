"""Tests for Admin -> Zoho Reconcile: the read-only, date-range-scoped
list of captured Razorpay payments still missing a donation/receipt.

This replaces the automatic hourly/daily reconciliation for day-to-day use
-- Zoho reconciliation is manual now (this page to see what's outstanding,
then Import Zoho Report to turn that period's Zoho export into receipts)
-- so the page itself must make no Zoho API call and change nothing, just
read Razorpay + this app's own donations table. See public.py's
unreconciled_razorpay_payments(), which this page is a thin wrapper
around.
"""
import datetime
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import login

URL = "/admin/donations/zoho-reconcile"


def _payment(payment_id, amount_rupees, contact, created_at, notes=None, order_id=None):
    return {
        "id": payment_id,
        "order_id": order_id or payment_id.replace("pay_", "order_"),
        "amount": int(round(amount_rupees * 100)),
        "status": "captured",
        "contact": contact,
        "email": "donor@example.com",
        "created_at": int(created_at.replace(tzinfo=datetime.timezone.utc).timestamp()),
        "notes": notes or {},
    }


def _razorpay(payments):
    client = MagicMock()
    client.payment.all.return_value = {"items": payments, "count": len(payments)}
    return patch("razorpay.Client", return_value=client)


@pytest.fixture(autouse=True)
def _logged_in(client):
    login(client)


def test_lists_a_captured_payment_with_no_donation_behind_it(client, app):
    app.config["RAZORPAY_ENABLED"] = True
    when = datetime.datetime(2026, 9, 5, 10, 30)
    pay = _payment("pay_Orphan1", 251, "9319880507", when)

    with _razorpay([pay]):
        resp = client.get(f"{URL}?from=2026-09-05&to=2026-09-05")

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "pay_Orphan1" in body
    assert "251.00" in body
    assert "9319880507" in body


def test_a_payment_already_recorded_does_not_appear(client, app):
    """The whole point: once staff have uploaded the Zoho export and a
    Donation row exists for this payment, it must drop off the list --
    otherwise the checklist never empties even after the work is done."""
    from extensions import db
    from models import Campaign, Donor, Donation

    app.config["RAZORPAY_ENABLED"] = True
    when = datetime.datetime(2026, 9, 5, 10, 30)
    pay = _payment("pay_Done1", 251, "9319880507", when)

    campaign = Campaign.query.first()
    donor = Donor(full_name="Test Donor", phone="9319880507")
    db.session.add(donor)
    db.session.commit()
    db.session.add(Donation(
        donor_id=donor.id, campaign_id=campaign.id, amount=251, payment_mode="online",
        status="success", razorpay_payment_id="pay_Done1",
    ))
    db.session.commit()

    with _razorpay([pay]):
        resp = client.get(f"{URL}?from=2026-09-05&to=2026-09-05")

    assert resp.status_code == 200
    assert "pay_Done1" not in resp.get_data(as_text=True)


def test_a_single_date_scopes_the_razorpay_window_to_that_day(client, app):
    """The page hands the chosen range straight to Razorpay as a from/to
    window -- Razorpay is what actually filters, so what this can check is
    that the right window is asked for, the same way
    unreconciled_razorpay_payments' own tests do."""
    app.config["RAZORPAY_ENABLED"] = True
    seen = {}
    client_mock = MagicMock()

    def _record(params):
        seen.update(params)
        return {"items": [], "count": 0}

    client_mock.payment.all.side_effect = _record
    with patch("razorpay.Client", return_value=client_mock):
        resp = client.get(f"{URL}?from=2026-09-05&to=2026-09-05")

    assert resp.status_code == 200
    assert seen["from"] == datetime.datetime(2026, 9, 5, tzinfo=datetime.timezone.utc).timestamp()
    assert seen["to"] == datetime.datetime(2026, 9, 6, tzinfo=datetime.timezone.utc).timestamp()


def test_a_multi_day_range_covers_every_day_in_it(client, app):
    app.config["RAZORPAY_ENABLED"] = True
    day1 = _payment("pay_Day1", 100, "9000000001", datetime.datetime(2026, 9, 5, 9, 0))
    day3 = _payment("pay_Day3", 100, "9000000002", datetime.datetime(2026, 9, 7, 9, 0))

    with _razorpay([day1, day3]):
        resp = client.get(f"{URL}?from=2026-09-05&to=2026-09-07")

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "pay_Day1" in body
    assert "pay_Day3" in body


def test_from_after_till_is_swapped_rather_than_returning_nothing(client, app):
    app.config["RAZORPAY_ENABLED"] = True
    pay = _payment("pay_Swapped", 100, "9000000003", datetime.datetime(2026, 9, 5, 9, 0))

    with _razorpay([pay]):
        resp = client.get(f"{URL}?from=2026-09-07&to=2026-09-05")

    assert resp.status_code == 200
    assert "pay_Swapped" in resp.get_data(as_text=True)


def test_no_range_defaults_to_today_without_error(client, app):
    app.config["RAZORPAY_ENABLED"] = True
    with _razorpay([]):
        resp = client.get(URL)
    assert resp.status_code == 200


def test_an_invalid_date_falls_back_to_today_rather_than_500ing(client, app):
    app.config["RAZORPAY_ENABLED"] = True
    with _razorpay([]):
        resp = client.get(f"{URL}?from=not-a-date&to=also-not-a-date")
    assert resp.status_code == 200


def test_razorpay_not_configured_shows_a_message_not_an_error(client, app):
    app.config["RAZORPAY_ENABLED"] = False
    resp = client.get(URL)
    assert resp.status_code == 200
    assert "isn't configured" in resp.get_data(as_text=True)


def test_a_test_form_payment_still_shows_up_but_flagged(client, app):
    """A form ticked as a test form under (the now-removed) Zoho Forms
    settings must still appear here -- a form flagged by mistake should
    look like a suspiciously busy line, never vanish outright."""
    from extensions import db
    from models import Campaign, ZohoForm

    app.config["RAZORPAY_ENABLED"] = True
    campaign = Campaign.query.first()
    db.session.add(ZohoForm(form_key="TESTFORM", campaign_id=campaign.id, is_test=True))
    db.session.commit()

    when = datetime.datetime(2026, 9, 5, 10, 0)
    pay = _payment(
        "pay_TestForm1", 50, "9000000002", when,
        notes={"zform_custom": "iskcondwarka,TESTFORM,tok"},
    )

    with _razorpay([pay]):
        resp = client.get(f"{URL}?from=2026-09-05&to=2026-09-05")

    body = resp.get_data(as_text=True)
    assert "pay_TestForm1" in body
    assert "test form" in body.lower()
