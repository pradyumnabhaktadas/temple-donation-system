"""REG-020 (QA report, 2026-08-20): axe's `label` rule (critical impact)
found that no form field on the site had a programmatically associated
label -- every <label> lacked a `for` attribute, and most inputs had no
`id` at all. A screen reader user tabbing through any donation form heard
"edit text, blank" for every field, with no indication of what to type.

The report's own axe scan covered /, /festival-seva, /bace-rent, and
/admin/login specifically -- this file checks each of those four pages
the same way: every <label for="..."> must point at an id that actually
exists on the page, and every <input>/<select>/<textarea> that isn't
inside a <label> (implicit association) must have a matching id that some
label points at, or another accepted labelling mechanism
(aria-label/aria-labelledby).
"""
import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _strip_scripts(html):
    """<script> bodies can contain incidental text that looks like a tag
    (e.g. a comment mentioning "<input>" in prose) -- strip them before
    scanning for real markup."""
    return re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.S)


def _labelled_ids(html):
    """ids referenced by a <label for="...">."""
    return set(re.findall(r'<label\b[^>]*\bfor="([^"]+)"', html))


def _field_tags(html):
    """Every real <input>, <select>, <textarea> opening tag (i.e. one with
    at least one attribute -- a bare "<input>" can only be incidental text
    that survived _strip_scripts, never an actual field), skipping hidden
    inputs (never shown to a screen reader) and honeypot/CSRF-style fields
    that carry no visible label by design."""
    tags = re.findall(r'<(?:input|select|textarea)\b[^>]+>', html)
    return [t for t in tags if 'type="hidden"' not in t]


def _field_id(tag):
    m = re.search(r'\bid="([^"]+)"', tag)
    return m.group(1) if m else None


def _has_own_label(tag):
    return 'aria-label=' in tag or 'aria-labelledby=' in tag


def _assert_every_field_is_labelled(html, page_name):
    html = _strip_scripts(html)
    labelled_ids = _labelled_ids(html)
    for tag in _field_tags(html):
        if _has_own_label(tag):
            continue
        field_id = _field_id(tag)
        assert field_id, f"{page_name}: a visible form field has no id and no aria-label -- {tag[:80]}"
        assert field_id in labelled_ids, (
            f"{page_name}: field id={field_id!r} has no <label for={field_id!r}> anywhere on the page -- {tag[:80]}"
        )


def test_donate_form_fields_are_all_labelled(client):
    html = client.get("/").data.decode()
    _assert_every_field_is_labelled(html, "/")


def test_festival_seva_fields_are_all_labelled(client):
    html = client.get("/festival-seva").data.decode()
    _assert_every_field_is_labelled(html, "/festival-seva")


def test_bace_rent_fields_are_all_labelled(client):
    html = client.get("/bace-rent").data.decode()
    _assert_every_field_is_labelled(html, "/bace-rent")


def test_admin_login_fields_are_all_labelled(client):
    html = client.get("/admin/login").data.decode()
    _assert_every_field_is_labelled(html, "/admin/login")


def test_offline_donation_form_fields_are_all_labelled(app, client):
    """The report's worst offender after /admin/donations itself (23 of 27
    unlinked inputs) -- the Single Entry + Bulk Upload offline-donation
    tabs share one page."""
    from conftest import login
    login(client)
    html = client.get("/admin/donations/manual").data.decode()
    _assert_every_field_is_labelled(html, "/admin/donations/manual")


def test_donations_log_fields_are_all_labelled(app, client):
    """The report's worst offender overall (50 of 105 unlinked inputs) --
    covers both the top filter bar and the per-row cancel/mark-paid modals,
    which only render once a donation exists to generate them."""
    from conftest import login
    from extensions import db
    from models import Campaign, Donation, Donor

    with app.app_context():
        donor = Donor(full_name="Label Test Donor", phone="9800000011")
        db.session.add(donor)
        campaign = Campaign.query.filter_by(name="Annadan").first()
        db.session.commit()
        success = Donation(donor_id=donor.id, campaign_id=campaign.id, amount=501, payment_mode="cash",
                            status="success", recorded_by="testadmin", receipt_number="TEST/LBL/0001")
        pending = Donation(donor_id=donor.id, campaign_id=campaign.id, amount=501, payment_mode="online",
                            status="pending", recorded_by="online")
        db.session.add_all([success, pending])
        db.session.commit()

    login(client)
    html = client.get("/admin/donations?status=all").data.decode()
    _assert_every_field_is_labelled(html, "/admin/donations")
