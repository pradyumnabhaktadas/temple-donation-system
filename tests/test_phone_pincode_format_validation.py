"""REG-012/REG-054 (QA report, 2026-08-20): the Phone field on all 3
public donation forms (/, /festival-seva, /bace-rent) was plain
type="tel" with no pattern, maxlength, or inputmode -- the browser
accepted a 2-digit or 50-digit value, or letters, just as happily as a
real 10-digit mobile number. The Pincode field (on / and
/festival-seva -- /bace-rent has no address/pincode block at all, so
REG-054's mention of it appears to be stale relative to the current
form) had the same gap for 6-digit Indian PIN codes.

This is purely client-side guidance -- normalize_phone()/is_valid_phone()
(utils.py) already reject a malformed phone server-side regardless of
what the browser allowed through, and every one of these routes still
requires a full round trip to /api/create-order to actually place a
donation. These tests only confirm the HTML hint the report asked for
is actually present, matching the [6-9]\\d{9} pattern utils.PHONE_RE
already enforces server-side.

The pattern also accepts a "+"-prefixed foreign number (utils.INTL_PHONE_RE)
-- added later so donors giving from outside India aren't blocked by a
form built assuming every donor has a 10-digit Indian mobile number. See
utils.normalize_phone's docstring for why the "+" is required to
recognise a number as foreign rather than a malformed Indian one.
"""
import re


def _ensure_festivals_campaign(app):
    """/festival-seva redirects back to "/" (with no form at all) unless
    a "Festivals" campaign exists and is active -- conftest.py doesn't
    seed one (only Annadan/Temple Construction/BACE Contribution), so
    tests that need the actual festival_seva.html form rendered create
    it here, the same way tests/test_pan_required_for_80g.py already
    does for its own festival-seva coverage."""
    from extensions import db
    from models import Campaign
    with app.app_context():
        campaign = Campaign.query.filter_by(name="Festivals").first()
        if campaign is None:
            campaign = Campaign(name="Festivals", is_80g=True)
            db.session.add(campaign)
            db.session.commit()


def _phone_input(html):
    m = re.search(r'<input[^>]*name="phone"[^>]*>', html)
    assert m, "no input[name=phone] found on this page"
    return m.group(0)


def _pincode_input(html):
    m = re.search(r'<input[^>]*name="pincode"[^>]*>', html)
    assert m, "no input[name=pincode] found on this page"
    return m.group(0)


class TestPhoneFormatValidation:
    def test_donate_page_phone_has_format_guidance(self, client):
        html = client.get("/").data.decode()
        tag = _phone_input(html)
        assert 'maxlength="20"' in tag
        assert 'pattern="[6-9][0-9]{9}|\\+[1-9][0-9]{7,14}"' in tag

    def test_festival_seva_phone_has_format_guidance(self, app, client):
        _ensure_festivals_campaign(app)
        html = client.get("/festival-seva").data.decode()
        tag = _phone_input(html)
        assert 'maxlength="20"' in tag
        assert 'pattern="[6-9][0-9]{9}|\\+[1-9][0-9]{7,14}"' in tag

    def test_bace_rent_phone_has_format_guidance(self, client):
        html = client.get("/bace-rent").data.decode()
        tag = _phone_input(html)
        assert 'maxlength="20"' in tag
        assert 'pattern="[6-9][0-9]{9}|\\+[1-9][0-9]{7,14}"' in tag


class TestPincodeFormatValidation:
    def test_donate_page_pincode_has_format_guidance(self, client):
        html = client.get("/").data.decode()
        tag = _pincode_input(html)
        assert 'maxlength="6"' in tag
        assert 'pattern="[0-9]{6}"' in tag

    def test_festival_seva_pincode_has_format_guidance(self, app, client):
        _ensure_festivals_campaign(app)
        html = client.get("/festival-seva").data.decode()
        tag = _pincode_input(html)
        assert 'maxlength="6"' in tag
        assert 'pattern="[0-9]{6}"' in tag


class TestForeignDonorPhoneAccepted:
    """Some donations come from donors outside India -- the phone field
    must not hard-require a 10-digit Indian mobile number. See
    utils.normalize_phone/is_valid_phone for the "+"-prefixed foreign
    number support this end-to-end flow depends on."""

    def test_create_order_accepts_a_foreign_phone_number(self, app, client):
        from models import Campaign, Donor
        campaign = Campaign.query.filter_by(name="Annadan").first()

        resp = client.post(
            "/api/create-order",
            json={
                "campaign_id": campaign.id,
                "amount": 501,
                "full_name": "Foreign Donor",
                "phone": "+1 415 555 2671",
                "email": "foreign.donor@example.com",
                "pan": "ABCDE1234F",
                "consent": "on",
            },
        )
        assert resp.status_code == 200, resp.get_json()

        donor = Donor.query.filter_by(full_name="Foreign Donor").first()
        assert donor is not None
        assert donor.phone == "+14155552671"

    def test_create_order_still_rejects_a_bare_non_indian_digit_string(self, app, client):
        """Without a "+", a non-10-digit number stays ambiguous (typo vs.
        genuinely foreign) and is still rejected -- same as before."""
        from models import Campaign
        campaign = Campaign.query.filter_by(name="Annadan").first()

        resp = client.post(
            "/api/create-order",
            json={
                "campaign_id": campaign.id,
                "amount": 501,
                "full_name": "Ambiguous Number",
                "phone": "14155552671",
                "email": "ambiguous@example.com",
                "consent": "on",
            },
        )
        assert resp.status_code == 400
        assert "phone number doesn't look right" in resp.get_json()["error"]
