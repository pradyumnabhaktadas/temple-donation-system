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


def send_receipt_email(donation, donor, org_cfg, pdf_path):
    """Emails the receipt PDF at `pdf_path` to `donor`. Returns True if it was
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

        with open(pdf_path, "rb") as f:
            msg.add_attachment(
                f.read(),
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


def _from_header(cfg):
    from_addr = cfg.get("MAIL_FROM_ADDRESS") or cfg.get("SMTP_USERNAME") or ""
    from_name = cfg.get("MAIL_FROM_NAME")
    if from_name and from_addr:
        return f"{from_name} <{from_addr}>"
    return from_addr
