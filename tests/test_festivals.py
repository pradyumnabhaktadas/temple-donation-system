"""Regression coverage for the admin Festival list."""

from conftest import login
from models import Festival


class TestFestivalManagement:
    def test_trailing_slash_opens_the_festival_page(self, app, client):
        """Old bookmarks and the Safari URL shown by the administrator use
        `/admin/festivals/`; it must be as usable as the canonical URL."""
        login(client)

        response = client.get("/admin/festivals/", follow_redirects=False)

        assert response.status_code == 200

    def test_admin_can_add_a_festival_from_the_trailing_slash_url(self, app, client):
        login(client)

        response = client.post(
            "/admin/festivals/",
            data={"name": "Govardhan Puja 2026", "event_date": "2026-10-20"},
            follow_redirects=False,
        )

        assert response.status_code == 302
        festival = Festival.query.filter_by(name="Govardhan Puja 2026").one()
        assert festival.event_date.isoformat() == "2026-10-20"
