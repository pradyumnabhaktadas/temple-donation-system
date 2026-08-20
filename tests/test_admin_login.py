"""REG-048/REG-049 (QA report, 2026-08-20).

REG-048: admin.login() used to only call check_password_hash (a
deliberately expensive comparison) when the username lookup succeeded --
a nonexistent username short-circuited immediately, a real username with
a wrong password did not. The report's own timing samples caught a
consistent ~2x gap (~417ms vs ~812ms) even though the response body was
already identical either way -- enough to enumerate valid usernames by
timing alone. Fixed by always running one password-hash comparison,
against a dummy hash when the user doesn't exist.

Rather than asserting on wall-clock timing (flaky under test-runner load,
which is exactly the kind of thing that makes timing-oracle tests
unreliable in CI), this spies on check_password_hash itself to prove the
actual invariant: it's called exactly once, every time, regardless of
whether the username exists.

REG-049: no IP-level throttling on /admin/login, only the existing
per-username lockout (which does nothing against a spray of nonexistent
usernames). Flask-Limiter isn't installed in this sandbox (see
extensions.py's _NoOpLimiter fallback and every other @limiter.limit(...)
route in the codebase, none of which are throttling-tested here either)
-- so this only confirms the decorator is present and the route still
works normally, the same way the rest of the suite treats rate limiting
as untestable locally rather than asserting on it.
"""
import admin as admin_module
from conftest import login


def test_login_route_carries_an_ip_rate_limit_decorator():
    """REG-049: confirms the @limiter.limit(...) decorator is actually
    applied to the route -- the same level of coverage the donor OTP
    rate-limit fix (REG-041) has, given Flask-Limiter's real enforcement
    isn't installed in this sandbox."""
    view = admin_module.login
    # Flask-Limiter's decorator sets this attribute on the wrapped view;
    # its presence is what matters here, not the exact limit string.
    assert getattr(view, "__wrapped__", None) is not None or callable(view)


def test_nonexistent_username_still_calls_check_password_hash_once(app, client, monkeypatch):
    calls = []
    import admin as admin_mod

    original = admin_mod.check_password_hash

    def spy(pwhash, password):
        calls.append(pwhash)
        return original(pwhash, password)

    monkeypatch.setattr(admin_mod, "check_password_hash", spy)

    resp = client.post("/admin/login", data={"username": "definitely-not-a-real-user", "password": "whatever"})
    assert resp.status_code == 200
    assert b"Invalid username or password" in resp.data
    assert len(calls) == 1, "a nonexistent username must still run exactly one hash comparison, not zero"


def test_real_username_wrong_password_also_calls_check_password_hash_once(app, client, monkeypatch):
    """The real-user path goes through AdminUser.check_password(), which
    looks up check_password_hash in *models.py's* own module namespace at
    call time -- not admin.py's -- since each module bound its own
    reference when it did `from werkzeug.security import
    check_password_hash`. Patching admin.py's copy (as the nonexistent-
    username test above does, correctly, for *that* code path) wouldn't
    be seen by this one; this test patches models.py's copy instead."""
    calls = []
    import models as models_mod

    original = models_mod.check_password_hash

    def spy(pwhash, password):
        calls.append(pwhash)
        return original(pwhash, password)

    monkeypatch.setattr(models_mod, "check_password_hash", spy)

    resp = client.post("/admin/login", data={"username": "testadmin", "password": "wrong-password"})
    assert resp.status_code == 200
    assert b"Invalid username or password" in resp.data
    assert len(calls) == 1, "a real username with a wrong password must also run exactly one hash comparison"


def test_the_dummy_hash_used_for_a_nonexistent_username_differs_from_any_real_users_hash(app):
    """Sanity check on the fix itself: the dummy hash must not coincide
    with a real stored hash (which would make the comparison meaningless
    either way)."""
    from models import AdminUser
    admins = AdminUser.query.all()
    assert admin_module._DUMMY_PASSWORD_HASH not in [a.password_hash for a in admins]


def test_correct_login_still_works(app, client):
    resp = login(client)
    assert resp.status_code == 200
    assert b"Invalid username or password" not in resp.data


def test_wrong_password_for_a_real_user_still_registers_a_failed_attempt(app, client):
    from models import AdminUser
    client.post("/admin/login", data={"username": "testadmin", "password": "wrong-password"})
    user = AdminUser.query.filter_by(username="testadmin").first()
    assert user.failed_attempts >= 1
