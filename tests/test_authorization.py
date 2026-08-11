"""Who can reach what.

Two roles exist: admin and staff. Staff record donations; admins also
manage users, backups, imports and the lookup lists. Hiding a link in the
nav is not access control -- the URL is still there -- so this checks the
routes themselves.

Written after finding that the camp management routes were only
@login_required, so a staff user could delete a camp (and rename one,
which rewrites donation rows) simply by knowing the URL. Every comparable
lookup-list route was admin-only; that one had been missed.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import login


# Routes an admin may use and staff may not. Kept explicit rather than
# derived from the decorators, so this fails if a decorator is removed.
ADMIN_ONLY_GET = [
    "/admin/settings/users",
    "/admin/settings/backup",
    "/admin/settings/activity-log",
    "/admin/donors/import",
    "/admin/donations/import-legacy",
    "/admin/donations/bulk-import",
    "/admin/iyf-camps/manage",
]

ADMIN_ONLY_POST = [
    ("/admin/settings/backup/run", {}),
    ("/admin/settings/backup/restore", {"mode": "preview"}),
    ("/admin/settings/users/add", {"username": "x", "role": "staff"}),
]

# Routes staff legitimately need to do their job.
STAFF_ALLOWED_GET = [
    "/admin/dashboard",
    "/admin/donations",
    "/admin/donors",
    "/admin/donations/manual",
    "/admin/iyf-camps",
    "/admin/iyf-camps/collections",
]


class TestLoggedOut:
    @pytest.mark.parametrize("url", ADMIN_ONLY_GET + STAFF_ALLOWED_GET)
    def test_admin_pages_require_login(self, app, client, url):
        resp = client.get(url, follow_redirects=False)
        assert resp.status_code != 200, f"{url} served to an anonymous visitor"
        assert "/admin/login" in resp.headers.get("Location", "")

    def test_no_admin_page_is_reachable_anonymously(self, app, client):
        """Belt and braces over the explicit list above: walk every admin
        GET route and confirm none of them render."""
        leaked = []
        for rule in app.url_map.iter_rules():
            if not rule.endpoint.startswith("admin.") or rule.endpoint == "admin.login":
                continue
            if rule.arguments or "GET" not in rule.methods:
                continue
            if client.get(str(rule), follow_redirects=False).status_code == 200:
                leaked.append(str(rule))
        assert not leaked, f"reachable without logging in: {leaked}"


class TestStaffRestrictions:
    @pytest.mark.parametrize("url", ADMIN_ONLY_GET)
    def test_staff_cannot_open_admin_pages(self, app, client, url):
        login(client, username="teststaff")
        resp = client.get(url, follow_redirects=False)
        assert resp.status_code != 200, f"staff could open {url}"

    @pytest.mark.parametrize("url,data", ADMIN_ONLY_POST)
    def test_staff_cannot_post_to_admin_actions(self, app, client, url, data):
        login(client, username="teststaff")
        resp = client.post(url, data=data, follow_redirects=False)
        assert resp.status_code in (301, 302, 403), f"staff POST to {url} was accepted"

    @pytest.mark.parametrize("url", STAFF_ALLOWED_GET)
    def test_staff_can_still_do_their_job(self, app, client, url):
        login(client, username="teststaff")
        assert client.get(url).status_code == 200, f"staff blocked from {url}"


class TestCampManagementIsAdminOnly:
    """Regression: these three were @login_required only."""

    def _make_camp(self, client):
        from models import Camp
        login(client)                                   # admin
        client.post("/admin/iyf-camps/manage", data={"name": "Camp A"},
                    follow_redirects=True)
        return Camp.query.one().id

    def test_staff_cannot_create_a_camp(self, app, client):
        from models import Camp
        login(client, username="teststaff")
        client.post("/admin/iyf-camps/manage", data={"name": "Sneaky Camp"},
                    follow_redirects=True)
        assert Camp.query.count() == 0

    def test_staff_cannot_delete_a_camp(self, app, client):
        from models import Camp
        camp_id = self._make_camp(client)
        login(client, username="teststaff")
        client.post(f"/admin/iyf-camps/manage/{camp_id}/delete", follow_redirects=True)
        assert Camp.query.count() == 1, "staff deleted a camp"

    def test_staff_cannot_rename_a_camp(self, app, client):
        """Renaming rewrites camp_name on existing donation rows, so it's a
        data change, not just a label change."""
        from models import Camp
        camp_id = self._make_camp(client)
        login(client, username="teststaff")
        client.post(f"/admin/iyf-camps/manage/{camp_id}/edit",
                    data={"name": "Renamed", "is_active": "yes"}, follow_redirects=True)
        assert Camp.query.one().name == "Camp A", "staff renamed a camp"

    def test_staff_can_still_record_camp_donations(self, app, client):
        """The restriction is on managing the list, not on doing the work."""
        from models import Donation
        self._make_camp(client)
        login(client, username="teststaff")
        client.post("/admin/iyf-camps/single", data={
            "camp_name": "Camp A", "full_name": "Student", "amount": "500",
        }, follow_redirects=True)
        assert Donation.query.count() == 1

    def test_manage_camps_link_hidden_from_staff(self, app, client):
        """A button that bounces you is worse than no button."""
        login(client, username="teststaff")
        html = client.get("/admin/iyf-camps").data.decode()
        assert "/admin/iyf-camps/manage" not in html

    def test_manage_camps_link_shown_to_admin(self, app, client):
        login(client)
        html = client.get("/admin/iyf-camps").data.decode()
        assert "/admin/iyf-camps/manage" in html
