"""Tests for the internal daily-report HTTP relay.

Context: every automatic 4 AM run of the temple-daily-report Cron Job
failed to send both email and WhatsApp (2026-08-29, 08-30, 08-31), while
manual re-runs and every donor-facing receipt this same app sends all day
succeed. Adding retries inside the send functions (test_daily_report_retry.py)
didn't change that. The fix moved the actual sending out of the Cron Job's
own process: daily_report.py is now a thin HTTP client that POSTs to this
app's own /internal/daily-report/send, and that route -- running inside
the always-on web app, not the cron container -- does the real work via
the existing daily_report_utils.send_report(). See config.py's
INTERNAL_TASK_TOKEN docstring.

Two things are covered here:
  1. The route itself (TestInternalDailyReportSendRoute) -- auth via
     INTERNAL_TASK_TOKEN, date validation, and that a correct request
     actually drives send_report() end to end.
  2. daily_report.py's role as an HTTP client (TestDailyReportCliClient) --
     built with requests.post mocked out, so these run with no network at
     all.
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import login

TOKEN = "test-internal-token"


class TestInternalDailyReportSendRoute:
    def test_returns_503_when_not_configured(self, client, app):
        app.config["INTERNAL_TASK_TOKEN"] = ""
        resp = client.post("/internal/daily-report/send", json={})
        assert resp.status_code == 503

    def test_rejects_missing_token(self, client, app):
        app.config["INTERNAL_TASK_TOKEN"] = TOKEN
        resp = client.post("/internal/daily-report/send", json={})
        assert resp.status_code == 401

    def test_rejects_wrong_token(self, client, app):
        app.config["INTERNAL_TASK_TOKEN"] = TOKEN
        resp = client.post(
            "/internal/daily-report/send",
            json={},
            headers={"X-Internal-Token": "wrong-token"},
        )
        assert resp.status_code == 401

    def test_rejects_malformed_date(self, client, app):
        app.config["INTERNAL_TASK_TOKEN"] = TOKEN
        resp = client.post(
            "/internal/daily-report/send",
            json={"date": "30-08-2026"},
            headers={"X-Internal-Token": TOKEN},
        )
        assert resp.status_code == 400

    def test_correct_token_drives_send_report_and_returns_json(self, client, app, monkeypatch):
        app.config["INTERNAL_TASK_TOKEN"] = TOKEN

        from extensions import db
        from models import Campaign, DailyReportRecipient, Donor, Donation

        with app.app_context():
            annadan = Campaign.query.filter_by(name="Annadan").first()
            donor = Donor(full_name="Relay Donor", phone="9876500604")
            db.session.add(donor)
            db.session.flush()
            db.session.add(Donation(
                donor_id=donor.id, campaign_id=annadan.id, amount=750,
                payment_mode="cash", status="success",
                donation_date=datetime.datetime(2026, 8, 26, 10, 0),
            ))
            db.session.add(DailyReportRecipient(contact_type="email", value="relay@example.org"))
            db.session.add(DailyReportRecipient(contact_type="whatsapp", value="9876543211"))
            db.session.commit()

        email_calls = []
        whatsapp_calls = []
        monkeypatch.setattr(
            "email_utils.send_daily_report_email",
            lambda cfg, to, data, org: (email_calls.append(to) or True, None),
        )
        monkeypatch.setattr(
            "whatsapp_utils.send_daily_report_whatsapp",
            lambda cfg, phone, data, org: (whatsapp_calls.append(phone) or True, None),
        )

        resp = client.post(
            "/internal/daily-report/send",
            json={"date": "2026-08-26"},
            headers={"X-Internal-Token": TOKEN},
        )

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["report_date"] == "2026-08-26"
        assert body["data"]["today"]["amount"] == 750.0
        assert body["email_sent"] is True
        assert body["email_recipients_count"] == 1
        assert body["whatsapp_sent_count"] == 1
        assert body["whatsapp_recipients_count"] == 1
        assert email_calls == [["relay@example.org"]]
        assert whatsapp_calls == ["9876543211"]

    def test_second_call_for_same_date_is_skipped_unless_forced(self, client, app, monkeypatch):
        app.config["INTERNAL_TASK_TOKEN"] = TOKEN
        monkeypatch.setattr("email_utils.send_daily_report_email", lambda *a, **k: (True, None))
        monkeypatch.setattr("whatsapp_utils.send_daily_report_whatsapp", lambda *a, **k: (True, None))

        headers = {"X-Internal-Token": TOKEN}
        first = client.post("/internal/daily-report/send", json={"date": "2026-08-27"}, headers=headers)
        assert first.status_code == 200
        assert not first.get_json().get("skipped")

        second = client.post("/internal/daily-report/send", json={"date": "2026-08-27"}, headers=headers)
        assert second.get_json().get("skipped") == "already_sent"

        forced = client.post(
            "/internal/daily-report/send", json={"date": "2026-08-27", "force": True}, headers=headers,
        )
        assert not forced.get_json().get("skipped")


class TestSendDailyReportNowButton:
    """The admin-panel "Send Report Now" button (admin.send_daily_report_now)
    -- runs inline under TESTING (see the route's own TESTING check), so
    these don't need a real background thread to observe the result."""

    def test_staff_cannot_trigger(self, client):
        login(client, username="teststaff", password="TestPass123!")
        resp = client.post("/admin/daily-report-recipients/send-now", follow_redirects=True)
        assert b"requires an administrator account" in resp.data

    def test_requires_login(self, client):
        resp = client.post("/admin/daily-report-recipients/send-now")
        assert resp.status_code in (302, 401)
        if resp.status_code == 302:
            assert "/admin/login" in resp.headers["Location"]

    def test_admin_trigger_logs_activity_and_runs_send_report(self, app, client, monkeypatch):
        from models import AdminActivityLog

        monkeypatch.setattr("email_utils.send_daily_report_email", lambda *a, **k: (True, None))
        monkeypatch.setattr("whatsapp_utils.send_daily_report_whatsapp", lambda *a, **k: (True, None))

        login(client)
        resp = client.post("/admin/daily-report-recipients/send-now", follow_redirects=True)
        assert resp.status_code == 200
        assert b"triggered" in resp.data

        with app.app_context():
            manual_trigger = AdminActivityLog.query.filter_by(action="daily_report_manual_trigger").first()
            assert manual_trigger is not None
            assert manual_trigger.admin_username == "testadmin"

            # send_report() itself ran inline (TESTING=True) and logged its
            # own "system" entry, same as the 4 AM automatic run would.
            sent_log = AdminActivityLog.query.filter_by(action="daily_report_sent").first()
            assert sent_log is not None
            assert sent_log.admin_username == "system"

    def test_second_click_same_day_reports_already_sent_without_resending(self, app, client, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "email_utils.send_daily_report_email",
            lambda cfg, to, data, org: (calls.append(1) or True, None),
        )
        monkeypatch.setattr("whatsapp_utils.send_daily_report_whatsapp", lambda *a, **k: (True, None))

        with app.app_context():
            from extensions import db
            from models import DailyReportRecipient
            db.session.add(DailyReportRecipient(contact_type="email", value="button@example.org"))
            db.session.commit()

        login(client)
        client.post("/admin/daily-report-recipients/send-now", follow_redirects=True)
        client.post("/admin/daily-report-recipients/send-now", follow_redirects=True)

        # send_report()'s own idempotency guard (AdminActivityLog lookup by
        # report_date) means the second trigger the same day is a no-op --
        # the button doesn't force a resend.
        assert len(calls) == 1


class TestDailyReportCliClient:
    """daily_report.py itself, with requests.post mocked out -- these never
    touch the network."""

    def _run(self, monkeypatch, capsys, argv, post_side_effect=None, env=None):
        import daily_report

        calls = []

        def fake_post(url, json=None, headers=None, timeout=None):
            calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
            if post_side_effect is not None:
                return post_side_effect(url, json, headers, timeout)
            raise AssertionError("post_side_effect not provided")

        monkeypatch.setattr(daily_report.requests, "post", fake_post)
        monkeypatch.setattr(sys, "argv", ["daily_report.py"] + argv)

        monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
        monkeypatch.delenv("INTERNAL_TASK_TOKEN", raising=False)
        for key, value in (env or {}).items():
            monkeypatch.setenv(key, value)

        exit_code = daily_report.main()
        captured = capsys.readouterr()
        return exit_code, captured, calls

    def test_missing_public_base_url_fails_fast_without_calling_post(self, monkeypatch, capsys):
        exit_code, captured, calls = self._run(
            monkeypatch, capsys, [], env={"INTERNAL_TASK_TOKEN": TOKEN},
        )
        assert exit_code == 1
        assert calls == []
        assert "PUBLIC_BASE_URL" in captured.err

    def test_missing_internal_task_token_fails_fast_without_calling_post(self, monkeypatch, capsys):
        exit_code, captured, calls = self._run(
            monkeypatch, capsys, [], env={"PUBLIC_BASE_URL": "https://example.org"},
        )
        assert exit_code == 1
        assert calls == []
        assert "INTERNAL_TASK_TOKEN" in captured.err

    def test_invalid_date_flag_fails_fast_without_calling_post(self, monkeypatch, capsys):
        exit_code, captured, calls = self._run(
            monkeypatch, capsys, ["--date", "30-08-2026"],
            env={"PUBLIC_BASE_URL": "https://example.org", "INTERNAL_TASK_TOKEN": TOKEN},
        )
        assert exit_code == 1
        assert calls == []
        assert "Invalid --date" in captured.err

    def test_posts_to_internal_endpoint_with_token_header_and_prints_summary(self, monkeypatch, capsys):
        class FakeResponse:
            status_code = 200

            def json(self):
                return {
                    "report_date": "2026-08-26",
                    "data": {
                        "today": {"amount": 750.0, "count": 1},
                        "week": {"amount": 750.0, "count": 1},
                        "month": {"amount": 750.0, "count": 1},
                    },
                    "email_sent": True,
                    "email_recipients_count": 1,
                    "email_error": None,
                    "whatsapp_sent_count": 1,
                    "whatsapp_recipients_count": 1,
                    "whatsapp_error": None,
                }

        exit_code, captured, calls = self._run(
            monkeypatch, capsys, ["--date", "2026-08-26", "--force"],
            post_side_effect=lambda *a, **k: FakeResponse(),
            env={"PUBLIC_BASE_URL": "https://example.org/", "INTERNAL_TASK_TOKEN": TOKEN},
        )

        assert exit_code == 0
        assert len(calls) == 1
        call = calls[0]
        assert call["url"] == "https://example.org/internal/daily-report/send"
        assert call["json"] == {"force": True, "date": "2026-08-26"}
        assert call["headers"] == {"X-Internal-Token": TOKEN}
        assert "Today:      Rs. 750.00" in captured.out
        assert "Email to 1 recipient(s): sent" in captured.out
        assert "WhatsApp: sent to 1/1 recipient(s)" in captured.out

    def test_prints_skipped_message(self, monkeypatch, capsys):
        class FakeResponse:
            status_code = 200

            def json(self):
                return {"skipped": "already_sent", "report_date": "2026-08-26"}

        exit_code, captured, calls = self._run(
            monkeypatch, capsys, [],
            post_side_effect=lambda *a, **k: FakeResponse(),
            env={"PUBLIC_BASE_URL": "https://example.org", "INTERNAL_TASK_TOKEN": TOKEN},
        )

        assert exit_code == 0
        assert "Skipped" in captured.out

    def test_non_200_response_is_reported_as_failure(self, monkeypatch, capsys):
        class FakeResponse:
            status_code = 401
            text = "Unauthorized"

        exit_code, captured, calls = self._run(
            monkeypatch, capsys, [],
            post_side_effect=lambda *a, **k: FakeResponse(),
            env={"PUBLIC_BASE_URL": "https://example.org", "INTERNAL_TASK_TOKEN": "wrong"},
        )

        assert exit_code == 1
        assert "401" in captured.err

    def test_connection_error_is_reported_as_failure(self, monkeypatch, capsys):
        import requests

        def raise_connection_error(*a, **k):
            raise requests.exceptions.ConnectionError("boom")

        exit_code, captured, calls = self._run(
            monkeypatch, capsys, [],
            post_side_effect=raise_connection_error,
            env={"PUBLIC_BASE_URL": "https://example.org", "INTERNAL_TASK_TOKEN": TOKEN},
        )

        assert exit_code == 1
        assert "Could not reach" in captured.err
