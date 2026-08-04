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
        resp = client.post("/my-donations/send-otp", data={"phone": "9999999999"}, follow_redirects=True)
        assert b"No donor account found" in resp.data
        assert DonorLoginOTP.query.count() == 0

    def test_known_phone_gets_otp_in_demo_mode(self, app, client, monkeypatch):
        _make_donor()
        monkeypatch.setattr(donor_portal, "generate_otp", lambda length: "123456")

        resp = client.post("/my-donations/send-otp", data={"phone": "9812345678"}, follow_redirects=True)
        assert b"DEMO MODE" in resp.data
        assert b"123456" in resp.data
        assert DonorLoginOTP.query.count() == 1

    def test_otp_is_hashed_not_stored_in_plaintext(self, app, client, monkeypatch):
        _make_donor()
        monkeypatch.setattr(donor_portal, "generate_otp", lambda length: "123456")
        client.post("/my-donations/send-otp", data={"phone": "9812345678"})

        record = DonorLoginOTP.query.first()
        assert record.otp_hash != "123456"
        assert record.check_otp("123456") is True
        assert record.check_otp("000000") is False

    def test_rate_limit_after_max_requests(self, app, client, monkeypatch):
        _make_donor()
        monkeypatch.setattr(donor_portal, "generate_otp", lambda length: "123456")
        app.config["OTP_MAX_REQUESTS_PER_HOUR"] = 2

        for _ in range(2):
            client.post("/my-donations/send-otp", data={"phone": "9812345678"})
        resp = client.post("/my-donations/send-otp", data={"phone": "9812345678"}, follow_redirects=True)
        assert b"Too many login attempts" in resp.data


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
