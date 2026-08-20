import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import donor_portal
from extensions import db
from models import Donor, DonorLoginOTP


def _make_donor(phone="9812345678", full_name="Test Donor"):
    donor = Donor(full_name=full_name, phone=phone)
    db.session.add(donor)
    db.session.commit()
    return donor


class TestOtpRequest:
    def test_unknown_phone_gets_no_otp(self, app, client):
        """QA report REG-040: an unregistered number used to get a
        distinct "No donor account found" message and land back on the
        login page -- a donor-privacy oracle letting anyone test whether a
        given phone number has ever donated. It now gets the same generic
        response a registered number does (see the matching assertion in
        test_the_response_is_identical_for_a_registered_number below)."""
        resp = client.post("/my-donations/send-otp", data={"phone": "9999999999"}, follow_redirects=True)
        assert b"No donor account found" not in resp.data
        assert b"account with us" in resp.data
        assert DonorLoginOTP.query.count() == 0

    def test_the_response_is_identical_for_a_registered_number(self, app, client, monkeypatch):
        """The actual REG-040 regression check: capture both responses and
        compare them, rather than each in isolation -- a future wording
        change to one side without the other would slip past a test that
        only checks each message independently."""
        _make_donor(phone="9812345678")
        monkeypatch.setattr(donor_portal, "generate_otp", lambda length: "123456")
        app.config["IS_PRODUCTION"] = True
        try:
            registered = client.post(
                "/my-donations/send-otp", data={"phone": "9812345678"}, follow_redirects=True
            )
            unregistered = client.post(
                "/my-donations/send-otp", data={"phone": "9999999999"}, follow_redirects=True
            )
            assert registered.request.path == unregistered.request.path, \
                "both must land on the same page (the verify page), not a different one for each"
            # The phone number itself is legitimately echoed back into the
            # verify form (the donor just typed it, in this same request --
            # that's not new information an enumeration attempt gains), so
            # it's stripped out before comparing everything else.
            normalize = lambda body: body.decode().replace("9812345678", "PHONE").replace("9999999999", "PHONE")
            assert normalize(registered.data) == normalize(unregistered.data), \
                "both responses must render identically once the submitted phone number itself is factored out"
        finally:
            app.config["IS_PRODUCTION"] = False

    def test_known_phone_gets_otp_in_demo_mode(self, app, client, monkeypatch):
        _make_donor()
        monkeypatch.setattr(donor_portal, "generate_otp", lambda length: "123456")

        resp = client.post("/my-donations/send-otp", data={"phone": "9812345678"}, follow_redirects=True)
        assert b"DEMO MODE" in resp.data
        assert b"123456" in resp.data
        assert DonorLoginOTP.query.count() == 1

    def test_demo_mode_never_discloses_the_otp_in_production(self, app, client, monkeypatch):
        """QA report REG-039/REG-055: with no SMS provider configured,
        this endpoint used to flash the real OTP into the HTTP response
        regardless of environment -- knowing a donor's phone number was
        enough to read their login code straight off the page and get into
        their account (donation history, address, PAN). DEMO MODE is only
        for local development; in production it must refuse instead."""
        _make_donor()
        # Deliberately not "123456" -- the donor phone number 9812345678
        # contains that exact substring, which made this test pass for the
        # wrong reason (matching the phone number echoed into the page's
        # own canonical-URL meta tag, not a disclosed OTP).
        monkeypatch.setattr(donor_portal, "generate_otp", lambda length: "778899")
        app.config["IS_PRODUCTION"] = True
        try:
            resp = client.post("/my-donations/send-otp", data={"phone": "9812345678"}, follow_redirects=True)
            assert b"778899" not in resp.data
            assert b"DEMO MODE" not in resp.data
            # Same generic message everyone gets (see REG-040 above) --
            # nothing in the response reveals that this specific phone
            # exists or that its OTP happened to fail to send.
            assert b"account with us" in resp.data
            # The record was created (rate limiting still counts it) but
            # left permanently unusable -- there's no code the donor could
            # have received to submit against it.
            record = DonorLoginOTP.query.one()
            assert record.consumed is True
        finally:
            app.config["IS_PRODUCTION"] = False

    def test_otp_is_hashed_not_stored_in_plaintext(self, app, client, monkeypatch):
        _make_donor()
        monkeypatch.setattr(donor_portal, "generate_otp", lambda length: "123456")
        client.post("/my-donations/send-otp", data={"phone": "9812345678"})

        record = DonorLoginOTP.query.first()
        assert record.otp_hash != "123456"
        assert record.check_otp("123456") is True
        assert record.check_otp("000000") is False

    def test_rate_limit_after_max_requests(self, app, client, monkeypatch):
        """The per-phone hourly cap still applies -- it just no longer
        says so out loud (see REG-040 above: a distinct "too many
        attempts" message would itself reveal that this number has an
        account and has been requesting codes). A rate-limited request now
        gets the same generic response and, crucially, no new OTP record
        -- confirmed here by count, since the response text can't tell the
        two states apart on purpose."""
        _make_donor()
        monkeypatch.setattr(donor_portal, "generate_otp", lambda length: "123456")
        app.config["OTP_MAX_REQUESTS_PER_HOUR"] = 2

        for _ in range(2):
            client.post("/my-donations/send-otp", data={"phone": "9812345678"})
        assert DonorLoginOTP.query.count() == 2

        resp = client.post("/my-donations/send-otp", data={"phone": "9812345678"}, follow_redirects=True)
        assert b"account with us" in resp.data
        assert DonorLoginOTP.query.count() == 2, "a 3rd, rate-limited request must not create a new record"


class TestOtpVerify:
    def test_correct_otp_logs_in(self, app, client, monkeypatch):
        donor = _make_donor()
        monkeypatch.setattr(donor_portal, "generate_otp", lambda length: "123456")
        client.post("/my-donations/send-otp", data={"phone": donor.phone})

        resp = client.post(
            "/my-donations/verify", data={"phone": donor.phone, "otp": "123456"}, follow_redirects=True
        )
        assert resp.status_code == 200
        assert b"Welcome" in resp.data
        with client.session_transaction() as sess:
            assert sess["donor_id"] == donor.id

    def test_wrong_otp_rejected(self, app, client, monkeypatch):
        donor = _make_donor()
        monkeypatch.setattr(donor_portal, "generate_otp", lambda length: "123456")
        client.post("/my-donations/send-otp", data={"phone": donor.phone})

        resp = client.post(
            "/my-donations/verify", data={"phone": donor.phone, "otp": "000000"}, follow_redirects=True
        )
        assert b"Incorrect OTP" in resp.data
        with client.session_transaction() as sess:
            assert "donor_id" not in sess

    def test_max_attempts_invalidates_otp(self, app, client, monkeypatch):
        donor = _make_donor()
        monkeypatch.setattr(donor_portal, "generate_otp", lambda length: "123456")
        app.config["OTP_MAX_VERIFY_ATTEMPTS"] = 2
        client.post("/my-donations/send-otp", data={"phone": donor.phone})

        for _ in range(2):
            client.post("/my-donations/verify", data={"phone": donor.phone, "otp": "000000"})

        # The correct OTP should no longer work -- it's been invalidated
        # after too many wrong guesses.
        resp = client.post(
            "/my-donations/verify", data={"phone": donor.phone, "otp": "123456"}, follow_redirects=True
        )
        with client.session_transaction() as sess:
            assert "donor_id" not in sess

    def test_expired_otp_rejected(self, app, client, monkeypatch):
        import datetime

        donor = _make_donor()
        monkeypatch.setattr(donor_portal, "generate_otp", lambda length: "123456")
        client.post("/my-donations/send-otp", data={"phone": donor.phone})

        record = DonorLoginOTP.query.first()
        record.expires_at = datetime.datetime.utcnow() - datetime.timedelta(minutes=1)
        db.session.commit()

        resp = client.post(
            "/my-donations/verify", data={"phone": donor.phone, "otp": "123456"}, follow_redirects=True
        )
        assert b"expired" in resp.data
        with client.session_transaction() as sess:
            assert "donor_id" not in sess


class TestAccountPage:
    def test_requires_login(self, client):
        resp = client.get("/my-donations/account", follow_redirects=True)
        assert b"Donor Login" in resp.data

    def test_logged_in_donor_sees_account(self, app, client, monkeypatch):
        donor = _make_donor()
        monkeypatch.setattr(donor_portal, "generate_otp", lambda length: "123456")
        client.post("/my-donations/send-otp", data={"phone": donor.phone})
        client.post("/my-donations/verify", data={"phone": donor.phone, "otp": "123456"})

        resp = client.get("/my-donations/account")
        assert resp.status_code == 200
        assert donor.full_name.encode() in resp.data

    def test_update_profile(self, app, client, monkeypatch):
        donor = _make_donor()
        monkeypatch.setattr(donor_portal, "generate_otp", lambda length: "123456")
        client.post("/my-donations/send-otp", data={"phone": donor.phone})
        client.post("/my-donations/verify", data={"phone": donor.phone, "otp": "123456"})

        client.post(
            "/my-donations/account/update",
            data={"full_name": donor.full_name, "email": "updated@example.com", "pan": ""},
            follow_redirects=True,
        )

        updated = Donor.query.get(donor.id)
        assert updated.email == "updated@example.com"

    def test_update_rejects_bad_pan(self, app, client, monkeypatch):
        donor = _make_donor()
        monkeypatch.setattr(donor_portal, "generate_otp", lambda length: "123456")
        client.post("/my-donations/send-otp", data={"phone": donor.phone})
        client.post("/my-donations/verify", data={"phone": donor.phone, "otp": "123456"})

        resp = client.post(
            "/my-donations/account/update",
            data={"full_name": donor.full_name, "pan": "NOTAPAN"},
            follow_redirects=True,
        )
        # Jinja HTML-escapes the apostrophe in "doesn't" to &#39; when the
        # flash message is rendered, so check for the unescaped tail of the
        # message rather than the raw string with an apostrophe in it.
        assert b"look right" in resp.data
        assert Donor.query.get(donor.id).pan is None

    def test_logout_clears_session(self, app, client, monkeypatch):
        donor = _make_donor()
        monkeypatch.setattr(donor_portal, "generate_otp", lambda length: "123456")
        client.post("/my-donations/send-otp", data={"phone": donor.phone})
        client.post("/my-donations/verify", data={"phone": donor.phone, "otp": "123456"})

        client.get("/my-donations/logout")
        with client.session_transaction() as sess:
            assert "donor_id" not in sess
