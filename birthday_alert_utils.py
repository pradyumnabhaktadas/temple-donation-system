"""Daily admin reminders for donors whose own birthday is today (IST).

The reminder is intentionally independent of the collection report and is
only sent to contacts configured under Admin -> Settings -> Birthday Alert
Recipients. It never sends a message to the donor.
"""
import datetime

from extensions import db
from models import AdminActivityLog, BirthdayAlertRecipient, Donor
from utils import now_ist

import email_utils
import whatsapp_utils


def birthday_matches_date(dob, alert_date):
    """Whether a date-of-birth occurs on alert_date, including leap-day DOBs.

    In non-leap years, 29 February birthdays are observed on 28 February,
    matching the existing admin birthday calendar's user-friendly behaviour.
    """
    if dob.month == 2 and dob.day == 29:
        try:
            datetime.date(alert_date.year, 2, 29)
        except ValueError:
            return alert_date.month == 2 and alert_date.day == 28
    return dob.month == alert_date.month and dob.day == alert_date.day


def compute_birthday_alert(alert_date=None):
    alert_date = alert_date or now_ist().date()
    donors = [
        donor for donor in Donor.query.filter(Donor.dob.isnot(None)).order_by(Donor.full_name).all()
        if birthday_matches_date(donor.dob, alert_date)
    ]
    return {"alert_date": alert_date, "donors": donors}


def send_birthday_alert(app, alert_date=None, force=False):
    """Deliver today's birthday list once per date, unless force is requested."""
    data = compute_birthday_alert(alert_date)
    date_key = int(data["alert_date"].strftime("%Y%m%d"))
    already_sent = AdminActivityLog.query.filter_by(
        action="birthday_alert_sent", target_type="birthday_alert", target_id=date_key
    ).first()
    if already_sent and not force:
        return {"skipped": "already_sent", "alert_date": data["alert_date"]}
    if not data["donors"]:
        return {"skipped": "no_birthdays", "alert_date": data["alert_date"], "data": data}

    recipients = BirthdayAlertRecipient.query.filter_by(is_active=True).all()
    emails = [r.value for r in recipients if r.contact_type == "email"]
    whatsapp_numbers = [r.value for r in recipients if r.contact_type == "whatsapp"]
    org_name = app.config.get("ORG_PARENT_NAME") or app.config.get("ORG_NAME") or "ISKCON Dwarka"

    email_sent, email_error = email_utils.send_birthday_alert_email(app.config, emails, data, org_name)
    whatsapp_sent_count, whatsapp_error = 0, None
    for phone in whatsapp_numbers:
        try:
            sent, error = whatsapp_utils.send_birthday_alert_whatsapp(app.config, phone, data, org_name)
            if sent:
                whatsapp_sent_count += 1
            elif error:
                whatsapp_error = error
        except Exception as exc:
            app.logger.exception("Birthday-alert WhatsApp send failed for %s", phone)
            whatsapp_error = str(exc)

    details = (
        f"date={data['alert_date']} birthdays={len(data['donors'])} email_sent={email_sent} "
        f"({len(emails)} recipient(s))"
        + (f" email_error={email_error[:120]!r}" if email_error else "")
        + f" whatsapp_sent={whatsapp_sent_count}/{len(whatsapp_numbers)}"
        + (f" whatsapp_error={whatsapp_error[:120]!r}" if whatsapp_error else "")
    )
    db.session.add(AdminActivityLog(
        admin_username="system", action="birthday_alert_sent", target_type="birthday_alert",
        target_id=date_key, details=details,
    ))
    db.session.commit()
    return {
        "alert_date": data["alert_date"], "data": data, "email_sent": email_sent,
        "email_recipients": emails, "email_error": email_error,
        "whatsapp_sent_count": whatsapp_sent_count, "whatsapp_recipients": whatsapp_numbers,
        "whatsapp_error": whatsapp_error,
    }
