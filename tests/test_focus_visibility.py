"""REG-002 (QA report, 2026-08-20): an axe/keyboard-navigation pass found
almost no visible focus indicators on the donation forms -- the theme's
own .form-control:focus ring is a 25%-opacity saffron glow, easy to miss
against the cream body background.

static/style.css now adds a universal :focus-visible rule (keyboard/
assistive-tech navigation only, not every mouse click) using a white ring
then a dark ring (box-shadow, not outline, since outline can't carry two
colors) -- the white gap keeps the dark ring visible even when a focusable
element sits on the site's own dark maroon/navy sections (.donation-hero,
.final-cta-banner), where a single maroon outline would blend in.

These are lightweight checks that the rule is present and every kind of
page still renders with it in effect -- not a full contrast audit, which
needs an actual renderer."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _style_css():
    here = os.path.dirname(__file__)
    with open(os.path.join(here, "..", "static", "style.css")) as f:
        return f.read()


def test_focus_visible_rule_present_and_not_a_single_color_outline():
    css = _style_css()
    assert ":focus-visible" in css
    # A plain single-color `outline` would vanish against the site's own
    # dark maroon/navy sections -- the fix must use the white-then-dark
    # double ring instead.
    assert "box-shadow: 0 0 0 2px #fff, 0 0 0 4.5px var(--maroon-dark);" in css


def test_focus_visible_rule_covers_every_interactive_element_type():
    css = _style_css()
    # Anchor on the actual selector (not the explanatory comment above it,
    # which also mentions ":focus-visible" in prose).
    rule_start = css.index("a:focus-visible")
    selector_block = css[rule_start:rule_start + 400]
    for tag in ("a", "button", "input", "select", "textarea", "summary", "[tabindex]"):
        assert f"{tag}:focus-visible" in selector_block, f"{tag} missing from the focus-visible selector list"


def test_stylesheet_is_actually_served(client):
    resp = client.get("/static/style.css")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert ":focus-visible" in body


def test_public_donate_page_still_renders_with_the_new_rule_in_effect(client):
    resp = client.get("/")
    assert resp.status_code == 200


def test_admin_login_page_still_renders_with_the_new_rule_in_effect(client):
    resp = client.get("/admin/login")
    assert resp.status_code == 200
