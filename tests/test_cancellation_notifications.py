"""Tests for the donor notification sent when a donation/receipt is
cancelled: an automatic email in the background (best-effort, same
pattern as the receipt notifications), and a manual wa.me link on the
Donations page since there's no generic WhatsApp-send capability in this
app (see whatsapp_utils.cancellation_whatsapp_link).

Drives the real /admin/donations/<id>/cancel route through the Flask test
client, same standard as test_iyf_camps.py -- not asserting on internals.
"""
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import login


@pytest.fixture(autouse=True)
def _logged_in(client):
    login(client)


def _make_success_donation(client, full_name="Cancel Test Donor", phone="9319880507", amount="501"):
    """A real success donation with a real receipt, created the way staff
    actually would (Offline Donation Single Entry, cash)."""
    from models import Campaign, Donation
    campaign = Campaign.query.filter_by(name="Annadan").first()

    with patch("public.send_receipt_email"), patch("public.send_receipt_whatsapp"):
        client.post("/admin/donations/manual", data={
            "campaign_id": str(campaign.id), "amount": amount, "full_name": full_name,
            "phone": phone, "payment_mode": "cash",
        }, follow_redirects=True)

    donation = Donation.query.filter_by(status="success").order_by(Donation.id.desc()).first()
    assert donation is not None, "setup failed: no donation was created"
    assert donation.receipt_number, "setup failed: no receipt was issued"
    return donation


class TestCancellationEmail:
    def test_cancelling_sends_an_email_to_the_donor(self, client, app):
        donation = _make_success_donation(client)
        from extensions import db
        from models import Donor
        donor = Donor.query.get(donation.donor_id)
        donor.email = "donor@example.com"
        db.session.commit()

        with patch("public.send_cancellation_email") as mock_email:
            client.post(
                f"/admin/donations/{donation.id}/cancel",
                data={"cancellation_reason": "Duplicate entry"},
                follow_redirects=True,
            )
            mock_email.assert_called_once()
            called_donation, called_donor, called_org_cfg, called_reason = mock_email.call_args[0]
            assert called_donation.id == donation.id
            assert called_donor.id == donor.id
            assert called_reason == "Duplicate entry"

    def test_no_email_attempted_without_an_address_on_file(self, client, app):
        """send_cancellation_email itself already no-ops without an email
        on file (see email_utils.py) -- this just confirms the wiring
        still runs the background job without blowing up when there's
        nothing to send to."""
        donation = _make_success_donation(client, full_name="No Email Donor", phone="9319880508")

        with patch("public.send_cancellation_email") as mock_email:
            resp = client.post(
                f"/admin/donations/{donation.id}/cancel",
                data={"cancellation_reason": "Wrong campaign"},
                follow_redirects=True,
            )
            assert resp.status_code == 200
            mock_email.assert_called_once()

    def test_restoring_does_not_send_a_cancellation_email(self, client, app):
        donation = _make_success_donation(client)
        client.post(
            f"/admin/donations/{donation.id}/cancel",
            data={"cancellation_reason": "Test"}, follow_redirects=True,
        )
        with patch("public.send_cancellation_email") as mock_email:
            client.post(f"/admin/donations/{donation.id}/restore", data={}, follow_redirects=True)
            mock_email.assert_not_called()


class TestCancellationWhatsAppLink:
    def test_a_wa_link_appears_for_a_cancelled_donation_with_a_phone(self, client, app):
        donation = _make_success_donation(client, phone="9319880507")
        client.post(
            f"/admin/donations/{donation.id}/cancel",
            data={"cancellation_reason": "Duplicate"}, follow_redirects=True,
        )

        resp = client.get("/admin/donations?status=cancelled")
        body = resp.get_data(as_text=True)
        assert "WhatsApp cancellation notice" in body
        assert "wa.me/919319880507" in body

    def test_no_wa_link_without_a_phone_on_file(self, client, app):
        from extensions import db
        from models import Campaign, Donor, Donation
        campaign = Campaign.query.filter_by(name="Annadan").first()
        donor = Donor(full_name="Phoneless Donor", phone=None)
        db.session.add(donor)
        db.session.flush()
        donation = Donation(
            donor_id=donor.id, campaign_id=campaign.id, amount=501,
            payment_mode="cash", status="success", receipt_number="TEST/NOPHONE001",
        )
        db.session.add(donation)
        db.session.commit()

        with patch("public.send_cancellation_email"):
            client.post(
                f"/admin/donations/{donation.id}/cancel",
                data={"cancellation_reason": "Test"}, follow_redirects=True,
            )

        resp = client.get("/admin/donations?status=cancelled")
        body = resp.get_data(as_text=True)
        assert "Phoneless Donor" in body
        assert "WhatsApp cancellation notice" not in body


class TestOnlySuccessDonationsCanBeCancelled:
    def test_a_pending_donation_is_refused_and_no_notification_is_sent(self, client, app):
        from extensions import db
        from models import Donor, Campaign, Donation
        donor = Donor(full_name="Pending Donor", phone="9000000007")
        db.session.add(donor)
        db.session.commit()
        campaign = Campaign.query.filter_by(name="Annadan").first()
        donation = Donation(
            donor_id=donor.id, campaign_id=campaign.id, amount=501,
            payment_mode="online", status="pending",
        )
        db.session.add(donation)
        db.session.commit()

        with patch("public.send_cancellation_email") as mock_email:
            client.post(
                f"/admin/donations/{donation.id}/cancel",
                data={"cancellation_reason": "n/a"}, follow_redirects=True,
            )
            mock_email.assert_not_called()

        unchanged = Donation.query.get(donation.id)
        assert unchanged.status == "pending"
