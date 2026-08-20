"""Every GET page in the app, loaded for real.

A page that 500s because a route forgot to pass a variable, or a template
references something that no longer exists, is the most common way a
change like a nav edit or a shared-partial edit breaks something far away
from where it was made. Nothing here asserts about content -- the point is
purely that every page renders.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import login


def _get_rules(app):
    """Every GET route with no required URL parameters."""
    out = []
    for rule in app.url_map.iter_rules():
        if "GET" not in rule.methods:
            continue
        if rule.arguments:            # needs an id we'd have to invent
            continue
        if rule.endpoint == "static":
            continue
        out.append(str(rule))
    return sorted(out)


class TestEveryPageRenders:
    def test_public_pages(self, app, client):
        failures = []
        for url in _get_rules(app):
            if url.startswith("/admin") or url.startswith("/api"):
                continue
            resp = client.get(url, follow_redirects=True)
            if resp.status_code >= 500:
                failures.append((url, resp.status_code))
        assert not failures, failures

    def test_admin_pages_as_admin(self, app, client):
        login(client)
        failures = []
        for url in _get_rules(app):
            if not url.startswith("/admin"):
                continue
            resp = client.get(url, follow_redirects=True)
            if resp.status_code >= 500:
                failures.append((url, resp.status_code))
        assert not failures, failures

    def test_admin_pages_as_staff(self, app, client):
        """Staff see a reduced nav; the role checks in the templates are a
        place a change can 500 for one role and not the other."""
        login(client, username="teststaff")
        failures = []
        for url in _get_rules(app):
            if not url.startswith("/admin"):
                continue
            resp = client.get(url, follow_redirects=True)
            if resp.status_code >= 500:
                failures.append((url, resp.status_code))
        assert not failures, failures

    def test_admin_pages_with_data_present(self, app, client):
        """Empty tables hide a lot: an empty list never exercises the row
        markup. Load every admin page again with a donation on file."""
        login(client)
        from models import Campaign
        campaign = Campaign.query.filter_by(name="Annadan").first()
        client.post("/admin/donations/manual", data={
            "campaign_id": campaign.id, "full_name": "Ravi Sharma",
            "phone": "9876543210", "amount": "1100", "payment_mode": "cash",
            "donation_date": "2026-08-01",
        }, follow_redirects=True)
        client.post("/admin/iyf-camps/manage", data={"name": "Utkarsha 2026"},
                    follow_redirects=True)
        client.post("/admin/iyf-camps/single", data={
            "camp_name": "Utkarsha 2026", "batch_name": "Batch A",
            "full_name": "Anita Verma", "amount": "500",
        }, follow_redirects=True)

        failures = []
        for url in _get_rules(app):
            if not url.startswith("/admin"):
                continue
            resp = client.get(url, follow_redirects=True)
            if resp.status_code >= 500:
                failures.append((url, resp.status_code))
        assert not failures, failures


class TestDonationsLogFilters:
    """The date presets added to the Donations Log."""

    @pytest.mark.parametrize("rng", ["this_month", "last_month", "last_3_months",
                                     "this_fy", "all", "nonsense"])
    def test_every_range_loads(self, app, client, rng):
        login(client)
        assert client.get(f"/admin/donations?range={rng}").status_code == 200

    def test_explicit_dates_beat_preset(self, app, client):
        login(client)
        resp = client.get("/admin/donations?range=this_month&date_from=2020-01-01")
        assert resp.status_code == 200
        assert b"Custom range" in resp.data

    def test_default_is_current_month(self, app, client):
        login(client)
        html = client.get("/admin/donations").data.decode()
        assert 'value="this_month" selected' in html

    def test_dashboard_links_ask_for_all_time(self, app, client):
        """Defaulting the log to this month would otherwise hide the older
        stuck donations those links exist to find.

        The panel only renders when something qualifies, and the dashboard
        deliberately ignores very recent pending donations -- one started
        seconds ago isn't abandoned, the donor is probably still paying.
        So this backdates it past that cutoff."""
        import datetime
        from extensions import db
        from models import Campaign, Donation

        campaign = Campaign.query.filter_by(name="Annadan").first()
        resp = client.post("/api/create-order", json={
            "campaign_id": campaign.id, "amount": 100, "full_name": "Abandoned Donor",
            "phone": "9811111111", "consent": "on",
            "pan": "ABCDE1234F",  # Annadan is 80G-eligible; see REG-036
        })
        donation = Donation.query.get(resp.get_json()["donation_id"])
        donation.donation_date = datetime.datetime.utcnow() - datetime.timedelta(days=2)
        db.session.commit()

        login(client)
        html = client.get("/admin/dashboard").data.decode()
        assert "Failed / Abandoned" in html, "panel did not render"
        assert "status=pending" in html and "range=all" in html

    def test_export_respects_the_range(self, app, client):
        login(client)
        assert client.get("/admin/export/donations?range=all").status_code == 200
        assert client.get("/admin/export/donations?range=this_month").status_code == 200


class TestPaginationJump:
    def test_page_jump_preserves_filters(self, app, client):
        login(client)
        resp = client.get("/admin/donations?status=all&range=all&page=1")
        assert resp.status_code == 200

    def test_out_of_range_page_does_not_500(self, app, client):
        login(client)
        assert client.get("/admin/donations?page=9999").status_code == 200
        assert client.get("/admin/donations?page=0").status_code == 200
        assert client.get("/admin/donations?page=abc").status_code == 200
