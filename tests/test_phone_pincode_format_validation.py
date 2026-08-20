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
        assert 'maxlength="10"' in tag
        assert 'pattern="[6-9][0-9]{9}"' in tag

    def test_festival_seva_phone_has_format_guidance(self, app, client):
        _ensure_festivals_campaign(app)
        html = client.get("/festival-seva").data.decode()
        tag = _phone_input(html)
        assert 'maxlength="10"' in tag
        assert 'pattern="[6-9][0-9]{9}"' in tag

    def test_bace_rent_phone_has_format_guidance(self, client):
        html = client.get("/bace-rent").data.decode()
        tag = _phone_input(html)
        assert 'maxlength="10"' in tag
        assert 'pattern="[6-9][0-9]{9}"' in tag


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
