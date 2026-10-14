"""Birthday alert recipient management and delivery, with no real network."""
import datetime

from conftest import login


class TestBirthdayAlertRecipients:
    def test_admin_can_add_and_manage_recipients(self, app, client):
        login(client)
        response = client.post("/admin/birthday-alert-recipients", data={
            "contact_type": "email", "value": "care@example.org"
        }, follow_redirects=True)
        assert response.status_code == 200
        assert b"care@example.org" in response.data
        from models import BirthdayAlertRecipient
        with app.app_context():
            recipient = BirthdayAlertRecipient.query.one()
            recipient_id = recipient.id
        client.post(f"/admin/birthday-alert-recipients/{recipient_id}/toggle", follow_redirects=True)
        with app.app_context():
            assert BirthdayAlertRecipient.query.get(recipient_id).is_active is False

    def test_invalid_phone_is_rejected(self, app, client):
        login(client)
        client.post("/admin/birthday-alert-recipients", data={
            "contact_type": "whatsapp", "value": "123"
        }, follow_redirects=True)
        from models import BirthdayAlertRecipient
        with app.app_context():
            assert BirthdayAlertRecipient.query.count() == 0


class TestBirthdayAlerts:
    def test_send_is_idempotent_and_never_targets_donors(self, app, monkeypatch):
        from extensions import db
        from models import BirthdayAlertRecipient, Donor
        with app.app_context():
            db.session.add_all([
                Donor(full_name="Birthday Donor", phone="9876500801", dob=datetime.date(1990, 9, 20)),
                Donor(full_name="Not Today", phone="9876500802", dob=datetime.date(1990, 9, 21)),
                BirthdayAlertRecipient(contact_type="email", value="admin@example.org"),
                BirthdayAlertRecipient(contact_type="whatsapp", value="9876500803"),
            ])
            db.session.commit()
            email_calls, whatsapp_calls = [], []
            monkeypatch.setattr("email_utils.send_birthday_alert_email", lambda cfg, to, data, org: (email_calls.append((to, data)) or (True, None)))
            monkeypatch.setattr("whatsapp_utils.send_birthday_alert_whatsapp", lambda cfg, phone, data, org: (whatsapp_calls.append((phone, data)) or (True, None)))
            from birthday_alert_utils import send_birthday_alert
            result = send_birthday_alert(app, alert_date=datetime.date(2026, 9, 20))
            assert result["email_sent"] is True
            assert email_calls[0][0] == ["admin@example.org"]
            assert [d.full_name for d in email_calls[0][1]["donors"]] == ["Birthday Donor"]
            assert whatsapp_calls[0][0] == "9876500803"
            assert send_birthday_alert(app, alert_date=datetime.date(2026, 9, 20))["skipped"] == "already_sent"

    def test_leap_day_is_observed_on_february_28_in_non_leap_year(self):
        from birthday_alert_utils import birthday_matches_date
        assert birthday_matches_date(datetime.date(2000, 2, 29), datetime.date(2027, 2, 28))
        assert not birthday_matches_date(datetime.date(2000, 2, 29), datetime.date(2027, 3, 1))
