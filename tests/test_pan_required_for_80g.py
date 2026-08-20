"""REG-036 (QA report, 2026-08-20): an 80G-eligible donation was accepted
with no PAN on file, as long as the amount stayed under the Rs. 49,000
high-value threshold. That's exactly the record Form 10BD can't actually
report -- the donate.html field label has always said "PAN (required for
80G receipt)", but nothing on the server enforced it below that threshold.

create_order() now computes what Donation.effective_is_80g would be for
the request (mirroring the model property) and refuses one with no PAN,
covering both ways a donation ends up 80G: a fixed-80G campaign (most of
them), and Live To Give's per-donation receipt_type choice.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture
def eighty_g_campaign(app):
    from extensions import db
    from models import Campaign
    with app.app_context():
        campaign = Campaign.query.filter_by(name="Annadan").first()
        assert campaign.is_80g, "fixture assumes Annadan is 80G-eligible per seed.py"
        return campaign.id


@pytest.fixture
def non_80g_campaign(app):
    from extensions import db
    from models import Campaign
    with app.app_context():
        campaign = Campaign.query.filter_by(name="BACE Contribution").first()
        if campaign is None:
            campaign = Campaign(name="BACE Contribution", is_80g=False)
            db.session.add(campaign)
            db.session.commit()
        assert not campaign.is_80g
        return campaign.id


@pytest.fixture
def live_to_give_setup(app):
    """Live To Give campaign + one 80G-eligible purpose + one non-80G one,
    matching how the real form is populated (Admin -> Live To Give
    Purposes)."""
    from extensions import db
    from models import Campaign, LiveToGivePurpose
    with app.app_context():
        campaign = Campaign.query.filter_by(name="Live To Give").first()
        if campaign is None:
            campaign = Campaign(name="Live To Give", is_80g=True)
            db.session.add(campaign)
            db.session.commit()
        eligible = LiveToGivePurpose.query.filter_by(name="Annadan Test Purpose").first()
        if eligible is None:
            eligible = LiveToGivePurpose(name="Annadan Test Purpose", is_80g=True)
            db.session.add(eligible)
        ineligible = LiveToGivePurpose.query.filter_by(name="Non-80G Test Purpose").first()
        if ineligible is None:
            ineligible = LiveToGivePurpose(name="Non-80G Test Purpose", is_80g=False)
            db.session.add(ineligible)
        db.session.commit()
        return {"campaign_id": campaign.id, "eligible_id": eligible.id, "ineligible_id": ineligible.id}


def _order(client, **overrides):
    data = {
        "amount": 251, "full_name": "PAN Test Donor", "phone": "9812345670", "consent": "on",
    }
    data.update(overrides)
    return client.post("/api/create-order", json=data)


class TestFixed80gCampaign:
    """Most campaigns fix is_80g on the Campaign row itself -- no per-
    donation choice, so every donation through one is 80G."""

    def test_no_pan_is_refused_below_the_high_value_threshold(self, app, client, eighty_g_campaign):
        resp = _order(client, campaign_id=eighty_g_campaign, amount=500)
        assert resp.status_code == 400
        assert "PAN" in resp.get_json()["error"]

    def test_with_pan_it_succeeds(self, app, client, eighty_g_campaign):
        resp = _order(client, campaign_id=eighty_g_campaign, amount=500, pan="ABCDE1234F")
        assert resp.status_code == 200

    def test_non_80g_campaign_needs_no_pan(self, app, client, non_80g_campaign):
        resp = _order(client, campaign_id=non_80g_campaign, amount=500)
        assert resp.status_code == 200


class TestLiveToGiveReceiptTypeChoice:
    """Live To Give lets the donor pick 80G/Non-80G per donation
    (receipt_type) -- effective_is_80g follows that choice, not the
    campaign's own default."""

    def test_choosing_80g_with_no_pan_is_refused(self, app, client, live_to_give_setup):
        resp = _order(
            client,
            campaign_id=live_to_give_setup["campaign_id"],
            live_to_give_purpose_id=live_to_give_setup["eligible_id"],
            receipt_type="80g",
        )
        assert resp.status_code == 400
        assert "PAN" in resp.get_json()["error"]

    def test_choosing_80g_with_pan_succeeds(self, app, client, live_to_give_setup):
        resp = _order(
            client,
            campaign_id=live_to_give_setup["campaign_id"],
            live_to_give_purpose_id=live_to_give_setup["eligible_id"],
            receipt_type="80g",
            pan="ABCDE1234F",
        )
        assert resp.status_code == 200

    def test_choosing_non80g_needs_no_pan_even_though_campaign_is_80g(self, app, client, live_to_give_setup):
        resp = _order(
            client,
            campaign_id=live_to_give_setup["campaign_id"],
            live_to_give_purpose_id=live_to_give_setup["eligible_id"],
            receipt_type="non80g",
        )
        assert resp.status_code == 200

    def test_no_answer_defaults_non80g_and_needs_no_pan(self, app, client, live_to_give_setup):
        """The form JS defaults the radio to "No" when a donor leaves it
        unanswered; this request simulates reaching the endpoint without
        that default having been applied client-side."""
        resp = _order(
            client,
            campaign_id=live_to_give_setup["campaign_id"],
            live_to_give_purpose_id=live_to_give_setup["eligible_id"],
        )
        assert resp.status_code == 200


class TestSpuriousPanNotStored:
    """REG-001 (QA report): the donate.html opt-out toggle now excludes
    the PAN field from FormData once "No" is selected (disabled, not just
    hidden) -- but the report's own recommendation asked for a server-side
    backstop too: "verify server-side that a non-80G donation record never
    stores a PAN even if one arrives in the request." These simulate a
    request that supplies a PAN anyway (stale form state, a non-JS client,
    a hand-crafted request) alongside a non-80G, below-threshold donation,
    and check it never reaches the donor's stored profile."""

    def test_pan_sent_with_a_non_80g_low_value_donation_is_not_stored(self, app, client, non_80g_campaign):
        from models import Donor
        resp = _order(
            client, campaign_id=non_80g_campaign, amount=500,
            phone="9812340099", pan="ABCDE1234F",
        )
        assert resp.status_code == 200
        donor = Donor.query.filter_by(phone="9812340099").first()
        assert donor is not None
        assert donor.pan is None

    def test_pan_sent_with_live_to_give_non80g_choice_is_not_stored(self, app, client, live_to_give_setup):
        from models import Donor
        resp = _order(
            client,
            campaign_id=live_to_give_setup["campaign_id"],
            live_to_give_purpose_id=live_to_give_setup["eligible_id"],
            receipt_type="non80g",
            phone="9812340098",
            pan="ABCDE1234F",
        )
        assert resp.status_code == 200
        donor = Donor.query.filter_by(phone="9812340098").first()
        assert donor is not None
        assert donor.pan is None

    def test_pan_is_still_stored_when_the_donation_is_actually_80g(self, app, client, eighty_g_campaign):
        """The backstop must not eat a legitimately-needed PAN."""
        from models import Donor
        resp = _order(
            client, campaign_id=eighty_g_campaign, amount=500,
            phone="9812340097", pan="ABCDE1234F",
        )
        assert resp.status_code == 200
        donor = Donor.query.filter_by(phone="9812340097").first()
        assert donor is not None
        assert donor.pan == "ABCDE1234F"

    def test_pan_is_still_stored_above_the_high_value_threshold_even_if_non_80g(self, app, client, non_80g_campaign):
        """Above Rs. 49,000 the temple must report the PAN regardless of
        80G status (income-tax high-value rule) -- the backstop only
        strips a PAN that isn't needed for either reason."""
        from models import Donor
        resp = _order(
            client, campaign_id=non_80g_campaign, amount=60000,
            phone="9812340096", pan="ABCDE1234F", address="123 Test Street",
        )
        assert resp.status_code == 200
        donor = Donor.query.filter_by(phone="9812340096").first()
        assert donor is not None
        assert donor.pan == "ABCDE1234F"


class TestFestivalSevaFormMarksPanRequired:
    """Festivals is a fixed-80G campaign with no per-donation opt-out --
    every donation through this form is 80G, so the field itself should
    say so upfront rather than let a donor discover it only after a server
    error on submit."""

    def test_pan_input_has_the_required_attribute(self, app, client):
        from extensions import db
        from models import Campaign
        campaign = Campaign.query.filter_by(name="Festivals").first()
        if campaign is None:
            campaign = Campaign(name="Festivals", is_80g=True)
            db.session.add(campaign)
            db.session.commit()

        body = client.get("/festival-seva").data.decode()
        assert body.count('id="pan"') >= 1
        pan_tag = body.split('id="pan"')[1].split('>')[0]
        assert "required" in pan_tag
