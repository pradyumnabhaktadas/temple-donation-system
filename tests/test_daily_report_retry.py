"""Tests for utils.retry() and its use in the daily report's unattended
sends (email_utils.send_daily_report_email, whatsapp_utils.send_daily_report_whatsapp).

Context: the 4 AM automatic run has intermittently failed both channels at
once (AdminActivityLog entries for 2026-08-29 and 2026-08-30 both show
email_sent=False, whatsapp_sent=0/2), while a manual re-run minutes later
from an interactive shell succeeds every time. Nothing in the surrounding
code changed between those two failures, so the working theory is a
transient cold-start hiccup in the Render Cron Job container rather than a
code bug -- these tests exist to prove the retry actually retries (not that
it fixes an unreproducible external flake).

time.sleep is monkeypatched to a no-op throughout so these tests run at
normal speed instead of waiting out the real delay_seconds between
attempts.
"""
import datetime
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils import retry


def _fake_report_data():
    period = {"amount": 500.0, "count": 1, "campaigns": [{"name": "Annadan", "amount": 500.0, "pct": 100.0}]}
    return {
        "report_date": datetime.date(2026, 8, 30),
        "week_start": datetime.date(2026, 8, 24),
        "month_start": datetime.date(2026, 8, 1),
        "today": period,
        "week": period,
        "month": period,
    }


class TestRetryHelper:
    def test_succeeds_on_first_try_without_sleeping(self, monkeypatch):
        sleep_calls = []
        monkeypatch.setattr("utils.time.sleep", lambda s: sleep_calls.append(s))

        calls = []

        def fn():
            calls.append(1)
            return "ok"

        assert retry(fn, attempts=3, delay_seconds=5) == "ok"
        assert len(calls) == 1
        assert sleep_calls == []

    def test_retries_then_succeeds(self, monkeypatch):
        sleep_calls = []
        monkeypatch.setattr("utils.time.sleep", lambda s: sleep_calls.append(s))

        attempts_seen = []

        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("transient")
            return "ok"

        result = retry(
            fn, attempts=3, delay_seconds=5,
            on_retry=lambda attempt, exc: attempts_seen.append(attempt),
        )

        assert result == "ok"
        assert calls["n"] == 3
        assert attempts_seen == [1, 2]  # on_retry fires after attempt 1 and 2, not after the successful 3rd
        assert sleep_calls == [5, 5]

    def test_raises_last_exception_after_exhausting_attempts(self, monkeypatch):
        monkeypatch.setattr("utils.time.sleep", lambda s: None)

        def fn():
            raise ValueError("still broken")

        with pytest.raises(ValueError, match="still broken"):
            retry(fn, attempts=3, delay_seconds=1)


class TestDailyReportEmailRetry:
    def test_retries_transient_smtp_failure_then_succeeds(self, app, monkeypatch):
        monkeypatch.setattr("utils.time.sleep", lambda s: None)
        app.config["SMTP_HOST"] = "smtp.example.com"

        from email_utils import send_daily_report_email

        mock_server = MagicMock()
        attempt = {"n": 0}

        def smtp_side_effect(*args, **kwargs):
            attempt["n"] += 1
            if attempt["n"] < 3:
                raise OSError("connection refused")
            cm = MagicMock()
            cm.__enter__.return_value = mock_server
            return cm

        with app.app_context(), patch("email_utils.smtplib.SMTP", side_effect=smtp_side_effect) as mock_smtp:
            sent, error = send_daily_report_email(app.config, ["a@example.org"], _fake_report_data(), "ISKCON Dwarka")

        assert sent is True
        assert error is None
        assert mock_smtp.call_count == 3
        assert mock_server.send_message.call_count == 1

    def test_gives_up_after_exhausting_retries(self, app, monkeypatch):
        monkeypatch.setattr("utils.time.sleep", lambda s: None)
        app.config["SMTP_HOST"] = "smtp.example.com"

        from email_utils import send_daily_report_email

        with app.app_context(), patch("email_utils.smtplib.SMTP", side_effect=OSError("connection refused")) as mock_smtp:
            sent, error = send_daily_report_email(app.config, ["a@example.org"], _fake_report_data(), "ISKCON Dwarka")

        assert sent is False
        # The actual reason now survives the swallow -- this is exactly
        # what AdminActivityLog's "email_error=" field surfaces (see
        # daily_report_utils.send_report), which is what was missing
        # during the 2026-08-29/08-30/08-31 incident.
        assert "connection refused" in error
        assert mock_smtp.call_count == 3  # default attempts=3, never raised out of send_daily_report_email


class TestDailyReportWhatsappRetry:
    def _configure(self, app):
        app.config["WHATSAPP_AIRTEL_USERNAME"] = "test-user"
        app.config["WHATSAPP_AIRTEL_PASSWORD"] = "test-pass"
        app.config["WHATSAPP_FROM_NUMBER"] = "918178798462"
        app.config["WHATSAPP_REPORT_TEMPLATE_ID"] = "01m16jfj9pg8zs0rxwk2p54g8j"

    def test_retries_non_ok_response_then_succeeds(self, app, monkeypatch):
        monkeypatch.setattr("utils.time.sleep", lambda s: None)
        self._configure(app)

        from whatsapp_utils import send_daily_report_whatsapp

        bad_resp = MagicMock(ok=False, status_code=500, text="server error")
        good_resp = MagicMock(ok=True, status_code=200, text='{"status":"INITIATED"}')

        with app.app_context(), patch(
            "whatsapp_utils.requests.post", side_effect=[bad_resp, bad_resp, good_resp]
        ) as mock_post:
            sent, error = send_daily_report_whatsapp(app.config, "9876543210", _fake_report_data(), "ISKCON Dwarka")

        assert sent is True
        assert error is None
        assert mock_post.call_count == 3

    def test_gives_up_after_exhausting_retries(self, app, monkeypatch):
        monkeypatch.setattr("utils.time.sleep", lambda s: None)
        self._configure(app)

        from whatsapp_utils import send_daily_report_whatsapp

        bad_resp = MagicMock(ok=False, status_code=500, text="server error")

        with app.app_context(), patch("whatsapp_utils.requests.post", return_value=bad_resp) as mock_post:
            sent, error = send_daily_report_whatsapp(app.config, "9876543210", _fake_report_data(), "ISKCON Dwarka")

        assert sent is False
        # Same visibility fix as the email side -- the actual Airtel
        # response (status + body) survives instead of being swallowed.
        assert "500" in error and "server error" in error
        assert mock_post.call_count == 3
