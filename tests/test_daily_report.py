"""Tests for the 4 AM daily collection report feature:

- Admin CRUD for DailyReportRecipient (Admin -> Settings -> Daily Report
  Recipients) -- add/validate/toggle/delete, admin-only.
- daily_report_utils.compute_report()'s day/week/month bucketing, including
  the IST-midnight edge case (donation_date is stored as naive UTC; a
  donation made just after midnight IST but still "yesterday" in UTC must
  land in the correct IST day's bucket -- see the module docstring for why
  this matters).
- daily_report_utils.send_report()'s idempotency guard and its calls into
  the (mocked, so no real network) email/WhatsApp senders.

Donations are seeded directly via the ORM (not the public payment flow)
for the same reason as test_analytics.py: exact control over
donation_date to land rows in specific day/week/month buckets.
"""
import datetime

from conftest import login


def _mk_donor(db, name, phone):
    from models import Donor
    donor = Donor(full_name=name, phone=phone)
    db.session.add(donor)
    db.session.flush()
    return donor


def _mk_donation(db, donor, campaign, amount, payment_mode, donation_date):
    from models import Donation
    donation = Donation(
        donor_id=donor.id, campaign_id=campaign.id, amount=amount,
        payment_mode=payment_mode, status="success", donation_date=donation_date,
    )
    db.session.add(donation)
    return donation


class TestDailyReportRecipientCRUD:
    def test_admin_can_add_email_and_whatsapp_recipients(self, app, client):
        login(client)
        resp = client.post(
            "/admin/daily-report-recipients",
            data={"contact_type": "email", "value": "treasurer@example.org"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"treasurer@example.org" in resp.data

        resp = client.post(
            "/admin/daily-report-recipients",
            data={"contact_type": "whatsapp", "value": "9876543210"},
            follow_redirects=True,
        )
        assert b"9876543210" in resp.data

        from models import DailyReportRecipient
        with app.app_context():
            assert DailyReportRecipient.query.count() == 2

    def test_invalid_email_is_rejected(self, app, client):
        login(client)
        client.post(
            "/admin/daily-report-recipients",
            data={"contact_type": "email", "value": "not-an-email"},
            follow_redirects=True,
        )
        from models import DailyReportRecipient
        with app.app_context():
            assert DailyReportRecipient.query.count() == 0

    def test_invalid_phone_is_rejected(self, app, client):
        login(client)
        client.post(
            "/admin/daily-report-recipients",
            data={"contact_type": "whatsapp", "value": "12345"},
            follow_redirects=True,
        )
        from models import DailyReportRecipient
        with app.app_context():
            assert DailyReportRecipient.query.count() == 0

    def test_duplicate_recipient_is_rejected(self, app, client):
        login(client)
        client.post(
            "/admin/daily-report-recipients",
            data={"contact_type": "email", "value": "dup@example.org"},
            follow_redirects=True,
        )
        client.post(
            "/admin/daily-report-recipients",
            data={"contact_type": "email", "value": "dup@example.org"},
            follow_redirects=True,
        )
        from models import DailyReportRecipient
        with app.app_context():
            assert DailyReportRecipient.query.count() == 1

    def test_toggle_and_delete(self, app, client):
        from extensions import db
        from models import DailyReportRecipient
        with app.app_context():
            r = DailyReportRecipient(contact_type="email", value="x@example.org")
            db.session.add(r)
            db.session.commit()
            rid = r.id

        login(client)
        client.post(f"/admin/daily-report-recipients/{rid}/toggle", follow_redirects=True)
        with app.app_context():
            assert DailyReportRecipient.query.get(rid).is_active is False

        client.post(f"/admin/daily-report-recipients/{rid}/delete", follow_redirects=True)
        with app.app_context():
            assert DailyReportRecipient.query.get(rid) is None

    def test_staff_cannot_manage_recipients(self, client):
        """admin_role_required -- staff accounts get redirected, same as
        every other Settings-only page (Manage Users, Data Backup, etc.)."""
        login(client, username="teststaff", password="TestPass123!")
        resp = client.post(
            "/admin/daily-report-recipients",
            data={"contact_type": "email", "value": "blocked@example.org"},
            follow_redirects=True,
        )
        assert b"requires an administrator account" in resp.data


class TestComputeReport:
    def test_day_week_month_buckets_and_campaign_breakdown(self, app):
        from extensions import db
        from models import Campaign

        with app.app_context():
            annadan = Campaign.query.filter_by(name="Annadan").first()
            bace = Campaign.query.filter_by(name="BACE Contribution").first()

            report_date = datetime.date(2026, 8, 26)  # a Wednesday
            week_start = datetime.date(2026, 8, 24)   # the Monday of that week
            month_start = datetime.date(2026, 8, 1)

            donor = _mk_donor(db, "Report Donor", "9876500601")
            # "Today" (report_date): two donations, two campaigns.
            _mk_donation(db, donor, annadan, 1000, "cash",
                         datetime.datetime.combine(report_date, datetime.time(10, 0)))
            _mk_donation(db, donor, bace, 500, "online",
                         datetime.datetime.combine(report_date, datetime.time(11, 0)))
            # Earlier this week, but not today.
            _mk_donation(db, donor, annadan, 2000, "cash",
                         datetime.datetime.combine(week_start, datetime.time(9, 0)))
            # Earlier this month, but not this week.
            _mk_donation(db, donor, annadan, 3000, "cash",
                         datetime.datetime.combine(month_start, datetime.time(9, 0)))
            # Last month entirely -- must not appear in any bucket.
            _mk_donation(db, donor, annadan, 9999, "cash",
                         datetime.datetime(2026, 7, 15, 9, 0))
            db.session.commit()

            from daily_report_utils import compute_report
            data = compute_report(report_date=report_date)

            assert data["today"]["amount"] == 1500
            assert data["today"]["count"] == 2
            assert {c["name"]: c["pct"] for c in data["today"]["campaigns"]} == {
                "Annadan": 66.7, "BACE Contribution": 33.3,
            }

            assert data["week"]["amount"] == 3500  # 1500 today + 2000 Monday
            assert data["month"]["amount"] == 6500  # 3500 week + 3000 month-start row

    def test_ist_midnight_boundary_uses_ist_not_utc_date(self, app):
        """A donation stored at 19:00 UTC on Aug 25 is 00:30 IST on Aug 26
        (UTC+5:30) -- it must land in Aug 26's bucket, not Aug 25's, even
        though donation_date (naive UTC) says Aug 25."""
        from extensions import db
        from models import Campaign

        with app.app_context():
            annadan = Campaign.query.filter_by(name="Annadan").first()
            donor = _mk_donor(db, "Midnight Donor", "9876500602")
            _mk_donation(db, donor, annadan, 777, "cash",
                         datetime.datetime(2026, 8, 25, 19, 0, 0))  # 00:30 IST Aug 26
            db.session.commit()

            from daily_report_utils import compute_report
            data_26 = compute_report(report_date=datetime.date(2026, 8, 26))
            data_25 = compute_report(report_date=datetime.date(2026, 8, 25))

            assert data_26["today"]["amount"] == 777
            assert data_25["today"]["amount"] == 0

    def test_empty_period_has_zero_totals_and_no_campaigns(self, app):
        from daily_report_utils import compute_report
        with app.app_context():
            data = compute_report(report_date=datetime.date(2020, 1, 1))
            assert data["today"]["amount"] == 0
            assert data["today"]["count"] == 0
            assert data["today"]["campaigns"] == []


class TestSendReport:
    def test_sends_email_and_whatsapp_and_logs_activity(self, app, monkeypatch):
        from extensions import db
        from models import Campaign, DailyReportRecipient, AdminActivityLog

        with app.app_context():
            annadan = Campaign.query.filter_by(name="Annadan").first()
            donor = _mk_donor(db, "Send Donor", "9876500603")
            report_date = datetime.date(2026, 8, 26)
            _mk_donation(db, donor, annadan, 500, "cash",
                         datetime.datetime.combine(report_date, datetime.time(10, 0)))
            db.session.add(DailyReportRecipient(contact_type="email", value="a@example.org"))
            db.session.add(DailyReportRecipient(contact_type="whatsapp", value="9876543210"))
            db.session.commit()

            email_calls = []
            whatsapp_calls = []

            def fake_email(cfg, to_addresses, data, org_name):
                email_calls.append((to_addresses, org_name))
                return True

            def fake_whatsapp(cfg, phone, data, org_name):
                whatsapp_calls.append(phone)
                return True

            monkeypatch.setattr("email_utils.send_daily_report_email", fake_email)
            monkeypatch.setattr("whatsapp_utils.send_daily_report_whatsapp", fake_whatsapp)

            from daily_report_utils import send_report
            result = send_report(app, report_date=report_date)

            assert result["email_sent"] is True
            assert email_calls[0][0] == ["a@example.org"]
            assert result["whatsapp_sent_count"] == 1
            assert whatsapp_calls == ["9876543210"]

            log = AdminActivityLog.query.filter_by(action="daily_report_sent").first()
            assert log is not None
            assert log.target_id == 20260826
            assert log.admin_username == "system"

    def test_second_run_for_same_date_is_skipped(self, app, monkeypatch):
        from extensions import db

        with app.app_context():
            monkeypatch.setattr("email_utils.send_daily_report_email", lambda *a, **k: True)
            monkeypatch.setattr("whatsapp_utils.send_daily_report_whatsapp", lambda *a, **k: True)

            from daily_report_utils import send_report
            report_date = datetime.date(2026, 8, 26)
            first = send_report(app, report_date=report_date)
            assert not first.get("skipped")

            second = send_report(app, report_date=report_date)
            assert second.get("skipped") == "already_sent"

    def test_force_resends_even_if_already_sent(self, app, monkeypatch):
        with app.app_context():
            calls = []
            monkeypatch.setattr("email_utils.send_daily_report_email", lambda *a, **k: (calls.append(1) or True))
            monkeypatch.setattr("whatsapp_utils.send_daily_report_whatsapp", lambda *a, **k: True)

            from extensions import db
            from models import DailyReportRecipient
            db.session.add(DailyReportRecipient(contact_type="email", value="force@example.org"))
            db.session.commit()

            from daily_report_utils import send_report
            report_date = datetime.date(2026, 8, 27)
            send_report(app, report_date=report_date)
            result = send_report(app, report_date=report_date, force=True)

            assert not result.get("skipped")
            assert len(calls) == 2

    def test_no_recipients_configured_still_logs_and_does_not_crash(self, app):
        with app.app_context():
            from daily_report_utils import send_report
            result = send_report(app, report_date=datetime.date(2026, 8, 28))
            assert result["email_recipients"] == []
            assert result["whatsapp_recipients"] == []
            assert result["email_sent"] is False
