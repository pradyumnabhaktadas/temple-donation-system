"""Emailing donation receipts to donors.

DEMO MODE (current state): no SMTP server is configured yet. send_receipt_email()
below doesn't actually send anything -- it just returns False so callers know
nothing went out. This mirrors the same demo-mode pattern used by Razorpay
(config.py) and OTP delivery (sms_utils.py): the rest of the donation flow
works identically whether or not email is wired up.

TO GO LIVE: set SMTP_HOST (plus SMTP_PORT/SMTP_USERNAME/SMTP_PASSWORD/
MAIL_FROM_ADDRESS) in your .env. Gmail works fine here -- use an "App
Password", not your normal Gmail password (Google Account -> Security ->
2-Step Verification -> App passwords). No new pip package is needed; this
uses only Python's built-in smtplib/email modules.
"""
import smtplib
import ssl
from email.message import EmailMessage

from flask import current_app

from utils import retry


def send_receipt_email(donation, donor, org_cfg, pdf_bytes):
    """Emails the receipt PDF (raw bytes, as returned by
    pdf_utils.generate_receipt_pdf) to `donor`. Returns True if it was
    actually sent, False if skipped (demo mode, or donor has no email on
    file). Never raises -- a failed/unconfigured email should never break the
    donation or receipt-generation flow, so any SMTP error is caught, logged,
    and swallowed.
    """
    if not donor.email:
        return False

    cfg = current_app.config
    smtp_host = cfg.get("SMTP_HOST")
    if not smtp_host:
        return False  # DEMO MODE: nothing was actually sent

    try:
        msg = EmailMessage()
        msg["Subject"] = f"Your donation receipt - {donation.receipt_number}"
        msg["From"] = _from_header(cfg)
        msg["To"] = donor.email

        org_name = org_cfg.get("ORG_PARENT_NAME") or org_cfg.get("ORG_NAME") or "the temple"
        msg.set_content(
            f"Dear {donor.full_name or 'Donor'},\n\n"
            f"Thank you for your generous donation of Rs. {donation.amount:,.2f} "
            f"to {org_name}. Your receipt ({donation.receipt_number}) is attached "
            f"to this email as a PDF.\n\n"
            f"This is a computer-generated email and does not require a signature.\n\n"
            f"Hare Krishna,\n{org_name}"
        )

        msg.add_attachment(
            pdf_bytes,
            maintype="application",
            subtype="pdf",
            filename=f"Receipt_{donation.receipt_number.replace('/', '_')}.pdf",
        )

        smtp_port = int(cfg.get("SMTP_PORT", 587))
        use_tls = cfg.get("SMTP_USE_TLS", True)
        username = cfg.get("SMTP_USERNAME")
        password = cfg.get("SMTP_PASSWORD")

        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            if use_tls:
                server.starttls(context=ssl.create_default_context())
            if username:
                server.login(username, password)
            server.send_message(msg)

        return True
    except Exception:
        # Email delivery is a nice-to-have on top of the receipt PDF, which
        # is already generated and downloadable regardless. Never let an SMTP
        # hiccup (bad credentials, network blip, etc.) surface as a donation
        # failure -- just log it so it can be noticed and fixed.
        current_app.logger.exception(
            "Failed to email receipt %s to %s", donation.receipt_number, donor.email
        )
        return False


def send_backup_email(cfg, to_email, filename, zip_bytes):
    """Emails a weekly data backup ZIP (see backup_utils.build_backup_zip)
    to `to_email`. Same demo-mode/never-raises contract as
    send_receipt_email above -- an unconfigured or failing SMTP send just
    means the backup file itself (already saved to disk by the caller)
    is the only copy, not a hard failure of the backup run."""
    if not to_email:
        return False

    smtp_host = cfg.get("SMTP_HOST")
    if not smtp_host:
        return False  # DEMO MODE: nothing was actually sent

    try:
        msg = EmailMessage()
        msg["Subject"] = f"Weekly data backup - {filename}"
        msg["From"] = _from_header(cfg)
        msg["To"] = to_email
        msg.set_content(
            "Attached is this week's full data backup (donors, donations, and lookup lists) "
            "as a ZIP of CSV files.\n\n"
            "This is a computer-generated email and does not require a signature."
        )
        msg.add_attachment(zip_bytes, maintype="application", subtype="zip", filename=filename)

        smtp_port = int(cfg.get("SMTP_PORT", 587))
        use_tls = cfg.get("SMTP_USE_TLS", True)
        username = cfg.get("SMTP_USERNAME")
        password = cfg.get("SMTP_PASSWORD")

        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            if use_tls:
                server.starttls(context=ssl.create_default_context())
            if username:
                server.login(username, password)
            server.send_message(msg)

        return True
    except Exception:
        current_app.logger.exception("Failed to email data backup %s to %s", filename, to_email)
        return False


def send_daily_report_email(cfg, to_addresses, report_data, org_name):
    """Emails the 4 AM daily collection report (see daily_report_utils.py)
    to every address in `to_addresses`. Same demo-mode/never-raises
    contract as the other senders in this file. Recipients are BCC'd on a
    single message rather than one email per recipient -- keeps this a
    single SMTP round trip and doesn't expose the recipient list to
    everyone on it."""
    if not to_addresses:
        return False

    smtp_host = cfg.get("SMTP_HOST")
    if not smtp_host:
        return False  # DEMO MODE: nothing was actually sent

    from daily_report_utils import _render_email_html

    try:
        msg = EmailMessage()
        report_date = report_data["report_date"]
        msg["Subject"] = f"Daily Collection Report - {report_date.strftime('%d %b %Y')}"
        msg["From"] = _from_header(cfg)
        # A real address in To: (the from address itself) rather than
        # leaving To: blank, since some receiving servers flag/ reject a
        # message with no To: header at all; the actual recipients are Bcc.
        msg["To"] = _from_header(cfg) or to_addresses[0]
        msg["Bcc"] = ", ".join(to_addresses)

        html = _render_email_html(report_data, org_name)
        msg.set_content(
            f"Daily Collection Report for {report_date.strftime('%d %b %Y')}\n\n"
            f"Today: Rs. {report_data['today']['amount']:,.2f}\n"
            f"This week: Rs. {report_data['week']['amount']:,.2f}\n"
            f"This month: Rs. {report_data['month']['amount']:,.2f}\n\n"
            "View this email in an HTML-capable client for the full campaign-wise breakdown."
        )
        msg.add_alternative(html, subtype="html")

        smtp_port = int(cfg.get("SMTP_PORT", 587))
        use_tls = cfg.get("SMTP_USE_TLS", True)
        username = cfg.get("SMTP_USERNAME")
        password = cfg.get("SMTP_PASSWORD")

        def _send():
            with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
                if use_tls:
                    server.starttls(context=ssl.create_default_context())
                if username:
                    server.login(username, password)
                server.send_message(msg)

        # This is the one send in this file that runs unattended (the 4 AM
        # cron job, not a donor waiting on a page) -- see retry()'s
        # docstring for why a couple of retries is worth it here but not for
        # send_receipt_email above.
        retry(
            _send,
            on_retry=lambda attempt, exc: current_app.logger.warning(
                "Daily report email attempt %d failed, retrying: %s", attempt, exc
            ),
        )

        return True
    except Exception:
        current_app.logger.exception("Failed to email daily report to %s", to_addresses)
        return False


def _from_header(cfg):
    from_addr = cfg.get("MAIL_FROM_ADDRESS") or cfg.get("SMTP_USERNAME") or ""
    from_name = cfg.get("MAIL_FROM_NAME")
    if from_name and from_addr:
        return f"{from_name} <{from_addr}>"
    return from_addr
