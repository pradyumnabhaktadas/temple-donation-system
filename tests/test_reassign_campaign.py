"""Tests for Admin -> Donations -> Reassign Campaign: the general fix for
a donation filed under the wrong campaign -- the case this was built for
is a BACE resident who donated through Live To Give by mistake instead of
BACE Contribution.

Campaign controls 80G eligibility and the receipt PDF's Purpose line, and
receipts are stored as the literal PDF issued, never regenerated on
demand -- so these tests check more than the campaign_id column: that the
receipt actually regenerates (same number, different bytes), 80G status
flips correctly, stale extra fields from the old campaign are cleared,
and the correction is only possible on a donation that actually has a
receipt to correct.
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


@pytest.fixture(autouse=True)
def _extra_campaigns(app):
    """conftest.py only seeds Annadan/Temple Construction/BACE
    Contribution -- Live To Give, Festivals, and a couple of lookups they
    need are specific to this feature, so this test file seeds them
    itself rather than assuming they're already there."""
    from extensions import db
    from models import Campaign, LiveToGivePurpose, Festival

    if not Campaign.query.filter_by(name="Live To Give").first():
        db.session.add(Campaign(name="Live To Give", is_80g=True, min_amount=101))
    if not Campaign.query.filter_by(name="Festivals").first():
        db.session.add(Campaign(name="Festivals", is_80g=False))
    db.session.commit()

    if not LiveToGivePurpose.query.filter_by(name="Cow Protection").first():
        db.session.add(LiveToGivePurpose(name="Cow Protection", is_80g=True))
    if not LiveToGivePurpose.query.filter_by(name="Book Distribution").first():
        db.session.add(LiveToGivePurpose(name="Book Distribution", is_80g=False))
    if not Festival.query.filter_by(name="Janmashtami").first():
        db.session.add(Festival(name="Janmashtami"))
    db.session.commit()


def _make_live_to_give_donation(client, app, full_name="Devotee One", phone="9319880507", amount="501"):
    """A real success donation with a real receipt, filed under Live To
    Give -- created the same way staff actually would (Offline Donation
    Single Entry, cash), not poked directly into the database, so the
    receipt this test corrects is a genuine one."""
    from models import Campaign, Donation
    campaign = Campaign.query.filter_by(name="Live To Give").first()

    with patch("public.send_receipt_email"), patch("public.send_receipt_whatsapp"):
        client.post("/admin/donations/manual", data={
            "campaign_id": str(campaign.id), "amount": amount, "full_name": full_name,
            "phone": phone, "payment_mode": "cash",
        }, follow_redirects=True)

    donation = Donation.query.filter_by(status="success").order_by(Donation.id.desc()).first()
    assert donation is not None, "setup failed: no donation was created"
    assert donation.receipt_number, "setup failed: no receipt was issued"
    return donation


def _bace_property(app, name="Nandgaon BACE"):
    from extensions import db
    from models import BaceProperty
    prop = BaceProperty(name=name)
    db.session.add(prop)
    db.session.commit()
    return prop.id


class TestReassignsTheCampaign:
    def test_moves_a_donation_from_live_to_give_to_bace_contribution(self, client, app):
        donation = _make_live_to_give_donation(client, app)
        prop_id = _bace_property(app)
        from models import Campaign, Donation
        bace = Campaign.query.filter_by(name="BACE Contribution").first()
        old_receipt_number = donation.receipt_number
        old_pdf = donation.receipt_pdf

        resp = client.post(
            f"/admin/donations/{donation.id}/reassign-campaign",
            data={"campaign_id": str(bace.id), "bace_property_id": str(prop_id)},
            follow_redirects=True,
        )
        assert resp.status_code == 200

        updated = Donation.query.get(donation.id)
        assert updated.campaign_id == bace.id
        assert updated.bace_property_id == prop_id
        # BACE Contribution is not 80G-eligible -- the whole point.
        assert updated.effective_is_80g is False
        # Same receipt: corrected in place, not a new one issued.
        assert updated.receipt_number == old_receipt_number
        # But the PDF itself changed to reflect the correction.
        assert updated.receipt_pdf != old_pdf

    def test_clears_the_old_campaigns_stale_fields(self, client, app):
        """A donation moved off Live To Give must not keep carrying a
        Live To Give purpose or an 80G override -- those belonged to the
        wrong campaign and would corrupt reporting if left behind."""
        donation = _make_live_to_give_donation(client, app)
        from extensions import db
        from models import LiveToGivePurpose, Campaign, Donation
        purpose = LiveToGivePurpose.query.filter_by(is_80g=True).first()
        donation.live_to_give_purpose_id = purpose.id
        donation.is_80g_requested = True
        db.session.commit()

        prop_id = _bace_property(app)
        bace = Campaign.query.filter_by(name="BACE Contribution").first()
        client.post(
            f"/admin/donations/{donation.id}/reassign-campaign",
            data={"campaign_id": str(bace.id), "bace_property_id": str(prop_id)},
            follow_redirects=True,
        )

        updated = Donation.query.get(donation.id)
        assert updated.live_to_give_purpose_id is None
        assert updated.is_80g_requested is None

    def test_moving_to_festivals_requires_choosing_a_festival(self, client, app):
        donation = _make_live_to_give_donation(client, app)
        from models import Campaign, Donation
        festivals_campaign = Campaign.query.filter_by(name="Festivals").first()

        client.post(
            f"/admin/donations/{donation.id}/reassign-campaign",
            data={"campaign_id": str(festivals_campaign.id)},
            follow_redirects=True,
        )

        unchanged = Donation.query.get(donation.id)
        assert unchanged.campaign.name == "Live To Give"

    def test_moving_to_bace_requires_choosing_a_property(self, client, app):
        donation = _make_live_to_give_donation(client, app)
        from models import Campaign, Donation
        bace = Campaign.query.filter_by(name="BACE Contribution").first()

        client.post(
            f"/admin/donations/{donation.id}/reassign-campaign",
            data={"campaign_id": str(bace.id)},
            follow_redirects=True,
        )

        unchanged = Donation.query.get(donation.id)
        assert unchanged.campaign.name == "Live To Give"

    def test_a_non_80g_purpose_cannot_be_forced_80g(self, client, app):
        donation = _make_live_to_give_donation(client, app)
        from models import LiveToGivePurpose, Campaign, Donation
        non_80g_purpose = LiveToGivePurpose.query.filter_by(is_80g=False).first()
        live_to_give = Campaign.query.filter_by(name="Live To Give").first()

        client.post(
            f"/admin/donations/{donation.id}/reassign-campaign",
            data={
                "campaign_id": str(live_to_give.id),
                "live_to_give_purpose_id": str(non_80g_purpose.id),
                "receipt_type": "80g",
            },
            follow_redirects=True,
        )

        unchanged = Donation.query.get(donation.id)
        assert unchanged.live_to_give_purpose_id is None

    def test_logs_the_correction(self, client, app):
        donation = _make_live_to_give_donation(client, app)
        prop_id = _bace_property(app)
        from models import Campaign, AdminActivityLog
        bace = Campaign.query.filter_by(name="BACE Contribution").first()

        client.post(
            f"/admin/donations/{donation.id}/reassign-campaign",
            data={"campaign_id": str(bace.id), "bace_property_id": str(prop_id)},
            follow_redirects=True,
        )

        entry = AdminActivityLog.query.filter_by(action="donation_reassign_campaign").first()
        assert entry is not None
        assert "Live To Give" in entry.details
        assert "BACE Contribution" in entry.details

    def test_resend_checkbox_triggers_notifications(self, client, app):
        donation = _make_live_to_give_donation(client, app)
        prop_id = _bace_property(app)
        from models import Campaign
        bace = Campaign.query.filter_by(name="BACE Contribution").first()

        with patch("public.send_receipt_email") as mock_email, \
             patch("public.send_receipt_whatsapp") as mock_whatsapp:
            client.post(
                f"/admin/donations/{donation.id}/reassign-campaign",
                data={
                    "campaign_id": str(bace.id), "bace_property_id": str(prop_id),
                    "resend_receipt": "yes",
                },
                follow_redirects=True,
            )
            mock_email.assert_called_once()
            mock_whatsapp.assert_called_once()

    def test_no_resend_by_default(self, client, app):
        donation = _make_live_to_give_donation(client, app)
        prop_id = _bace_property(app)
        from models import Campaign
        bace = Campaign.query.filter_by(name="BACE Contribution").first()

        with patch("public.send_receipt_email") as mock_email, \
             patch("public.send_receipt_whatsapp") as mock_whatsapp:
            client.post(
                f"/admin/donations/{donation.id}/reassign-campaign",
                data={"campaign_id": str(bace.id), "bace_property_id": str(prop_id)},
                follow_redirects=True,
            )
            mock_email.assert_not_called()
            mock_whatsapp.assert_not_called()


class TestOnlySuccessDonationsCanBeReassigned:
    def test_a_pending_donation_is_refused(self, client, app):
        from extensions import db
        from models import Donor, Campaign, Donation
        donor = Donor(full_name="Pending Donor", phone="9000000009")
        db.session.add(donor)
        db.session.commit()
        campaign = Campaign.query.filter_by(name="Live To Give").first()
        donation = Donation(
            donor_id=donor.id, campaign_id=campaign.id, amount=501,
            payment_mode="online", status="pending",
        )
        db.session.add(donation)
        db.session.commit()

        bace = Campaign.query.filter_by(name="BACE Contribution").first()
        resp = client.post(
            f"/admin/donations/{donation.id}/reassign-campaign",
            data={"campaign_id": str(bace.id)}, follow_redirects=True,
        )
        assert resp.status_code == 200

        unchanged = Donation.query.get(donation.id)
        assert unchanged.campaign_id == campaign.id

    def test_get_on_a_pending_donation_redirects_without_a_form(self, client, app):
        from extensions import db
        from models import Donor, Campaign, Donation
        donor = Donor(full_name="Pending Donor 2", phone="9000000008")
        db.session.add(donor)
        db.session.commit()
        campaign = Campaign.query.filter_by(name="Live To Give").first()
        donation = Donation(
            donor_id=donor.id, campaign_id=campaign.id, amount=501,
            payment_mode="online", status="pending",
        )
        db.session.add(donation)
        db.session.commit()

        resp = client.get(f"/admin/donations/{donation.id}/reassign-campaign", follow_redirects=False)
        assert resp.status_code == 302
