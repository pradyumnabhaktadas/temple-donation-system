"""Follow-up to REG-034 (user-reported, 2026-08-21): the public
/festival-seva page hardcoded "every Festival Seva donation is
80G-eligible" and always marked PAN as required, regardless of what the
"Festivals" campaign's own is_80g flag actually says. In production that
flag is meant to be False (Festival Seva isn't 80G-eligible) -- but the
page ignored it entirely.

The page now derives its PAN/address requirement, copy, and the "80G
eligible" hero badge from campaign.is_80g (passed in from
festival_seva_form() in public.py) instead of assuming either state, the
same way create_order()/Donation.effective_is_80g already do server-side.
This covers both configurations rather than re-hardcoding the opposite
assumption.
"""
import re


def _make_festivals_campaign(app, is_80g):
    from extensions import db
    from models import Campaign
    with app.app_context():
        campaign = Campaign.query.filter_by(name="Festivals").first()
        if campaign is None:
            campaign = Campaign(name="Festivals", is_80g=is_80g)
            db.session.add(campaign)
        else:
            campaign.is_80g = is_80g
        db.session.commit()


def _pan_input(html):
    m = re.search(r'<input[^>]*name="pan"[^>]*>', html)
    assert m, "no input[name=pan] found on the page"
    return m.group(0)


class TestFestival80GEligible:
    """Campaign.is_80g == True: PAN stays always-required/visible, same
    as before this fix -- this configuration must keep working exactly
    as it did."""

    def test_pan_is_required_and_visible(self, app, client):
        _make_festivals_campaign(app, is_80g=True)
        html = client.get("/festival-seva").data.decode()
        assert "required" in _pan_input(html)

    def test_hero_shows_80g_badge(self, app, client):
        _make_festivals_campaign(app, is_80g=True)
        html = client.get("/festival-seva").data.decode()
        assert "80G</b> eligible" in html

    def test_pan_field_not_hidden_by_default(self, app, client):
        _make_festivals_campaign(app, is_80g=True)
        html = client.get("/festival-seva").data.decode()
        m = re.search(r'<div id="pan-address-fields"([^>]*)>', html)
        assert m, "pan-address-fields wrapper not found"
        assert "display:none" not in m.group(1)


class TestFestivalNon80G:
    """Campaign.is_80g == False (the correct real-world setting): PAN and
    address are hidden and optional by default, exactly like
    /bace-rent -- collected (and required) only once the donation crosses
    the same high-value threshold that makes them legally necessary
    regardless of 80G status."""

    def test_pan_is_not_required_by_default(self, app, client):
        _make_festivals_campaign(app, is_80g=False)
        html = client.get("/festival-seva").data.decode()
        assert "required" not in _pan_input(html)

    def test_hero_does_not_show_80g_badge(self, app, client):
        _make_festivals_campaign(app, is_80g=False)
        html = client.get("/festival-seva").data.decode()
        assert "80G</b> eligible" not in html

    def test_pan_address_block_hidden_by_default(self, app, client):
        _make_festivals_campaign(app, is_80g=False)
        html = client.get("/festival-seva").data.decode()
        m = re.search(r'<div id="pan-address-fields"([^>]*)>', html)
        assert m, "pan-address-fields wrapper not found"
        assert "display:none" in m.group(1)

    def test_server_side_still_only_requires_pan_above_high_value(self, app, client):
        """The client-side hiding above is just UI -- this confirms the
        actual submission path (create_order) matches: a low-value
        Festival Seva donation with no PAN succeeds once the campaign is
        non-80G, and a high-value one without PAN is still refused."""
        _make_festivals_campaign(app, is_80g=False)
        from models import Campaign
        campaign = Campaign.query.filter_by(name="Festivals").first()

        low = client.post("/api/create-order", json={
            "campaign_id": campaign.id, "amount": 501,
            "full_name": "Low Value Donor", "phone": "9177005566", "consent": "on",
        })
        assert low.status_code == 200, low.get_json()

        high = client.post("/api/create-order", json={
            "campaign_id": campaign.id, "amount": 50001,
            "full_name": "High Value Donor", "phone": "9177006677", "consent": "on",
        })
        assert high.status_code == 400
        assert "PAN" in high.get_json()["error"] or "pan" in high.get_json()["error"].lower()
