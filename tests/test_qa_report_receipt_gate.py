"""REG-026/REG-056 (QA report, 2026-08-20): /receipt/<id> and
/donate/success/<id> must never hand out donor PII/PAN to a caller with no
proof of ownership. Donation ids are sequential, so "proof" can never be
just knowing the id.

/receipt/<id> was already fixed before this report (a signed token is
required); REG-056 was that /donate/success/<id> had no such gate at all,
and printed a *working* one of those tokens straight into its own HTML --
so the fix on /receipt/<id> was being undone by its own companion page,
confirmed live on production by the report and reproduced here against a
real donation.
"""
import re

from utils import receipt_access_token


def _make_donation(app, status="success"):
    from extensions import db
    from models import Donation, Donor, Campaign
    with app.app_context():
        donor = Donor(full_name="Test Donor", phone="9811111199", pan="ABCDE1234F", address="Some address")
        db.session.add(donor)
        campaign = Campaign.query.first()
        if campaign is None:
            campaign = Campaign(name="Test Campaign", is_80g=True)
            db.session.add(campaign)
        db.session.commit()
        donation = Donation(donor_id=donor.id, campaign_id=campaign.id, amount=101, payment_mode="online",
                             status=status, receipt_number="TEST/0001" if status == "success" else None)
        db.session.add(donation)
        db.session.commit()
        return donation.id


def test_receipt_download_with_no_token_is_rejected(app, client):
    did = _make_donation(app)
    resp = client.get(f"/receipt/{did}")
    assert resp.status_code == 404


def test_receipt_download_with_wrong_token_is_rejected(app, client):
    did = _make_donation(app)
    wrong_token = "0" * 40
    resp = client.get(f"/receipt/{did}?t={wrong_token}")
    assert resp.status_code == 404


def test_receipt_download_with_valid_token_works(app, client):
    did = _make_donation(app)
    with app.app_context():
        tok = receipt_access_token(did, app.config["SECRET_KEY"])
    resp = client.get(f"/receipt/{did}?t={tok}")
    assert resp.status_code == 200
    assert resp.content_type == "application/pdf"


def test_success_page_with_no_auth_shows_nothing_donor_specific(app, client):
    """The REG-056 regression itself: with no token and no session, this
    page must not show the amount, the receipt number, or -- the actual
    exposure -- a working download link, to a caller who only knows the
    (guessable, sequential) donation id."""
    did = _make_donation(app)
    resp = client.get(f"/donate/success/{did}")
    body = resp.data.decode()

    assert resp.status_code == 200
    assert "101.00" not in body
    assert "TEST/0001" not in body
    # The specific leak: a usable /receipt/<id>?t=... link embedded in an
    # unauthenticated page's HTML.
    m = re.search(rf"/receipt/{did}\?t=([\w\-]+)", body)
    assert m is None, "an unauthenticated visitor should never receive a working receipt-download token"


def test_success_page_with_valid_token_shows_the_receipt(app, client):
    """The legitimate case: a donor's own browser, holding the token
    payment_callback()/verify-payment handed it, still sees their receipt."""
    did = _make_donation(app)
    with app.app_context():
        tok = receipt_access_token(did, app.config["SECRET_KEY"])
    resp = client.get(f"/donate/success/{did}?t={tok}")
    body = resp.data.decode()
    assert "101.00" in body
    assert "TEST/0001" in body
    assert f"/receipt/{did}?t=" in body


def test_success_page_for_a_pending_donation_is_unaffected(app, client):
    """The token gate only applies to the detail shown for a *successful*
    donation -- the pending/cancelled states never carried donor detail in
    the first place and must keep working with no token."""
    did = _make_donation(app, status="pending")
    resp = client.get(f"/donate/success/{did}")
    assert resp.status_code == 200
    assert b"haven" in resp.data.lower() or b"pending" in resp.data.lower()
