"""REG-024 (QA report, 2026-08-20): no page on the site had a <main>
landmark, which is how a screen reader jumps straight to a page's real
content instead of tabbing through the header/nav first every time.

base.html wraps {% block content %} in <main>, but admin/base_admin.html
overrides the whole {% block body %} rather than extending base.html's, so
it needed its own <main> too -- these two are the only templates in the
repo that override that block (checked via grep), so together they cover
every page."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import login


def test_public_page_has_a_main_landmark(client):
    body = client.get("/").data.decode()
    assert "<main" in body


def test_admin_login_page_has_a_main_landmark(client):
    body = client.get("/admin/login").data.decode()
    assert "<main" in body


def test_admin_dashboard_has_a_main_landmark(app, client):
    login(client)
    body = client.get("/admin/dashboard").data.decode()
    assert "<main" in body
