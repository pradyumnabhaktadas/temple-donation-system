import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from email_utils import send_receipt_email


def _fake_donation():
    return SimpleNamespace(receipt_number="032511/ISK500000", amount=501.0)


def _fake_donor(email="donor@example.com"):
    return SimpleNamespace(full_name="Test Donor", email=email)


def _fake_pdf_bytes():
    # generate_receipt_pdf() now returns raw bytes (built in-memory, see
    # pdf_utils.py) rather than a file path -- send_receipt_email() takes
    # those bytes directly.
    return b"%PDF-1.4 fake receipt content"


class TestEmailReceipt:
    """Receipt emailing is stdlib smtplib-based (no new pip dependency) and
    gated on SMTP_HOST, matching the demo-mode pattern used elsewhere in this
    codebase (Razorpay, SMS OTP). It should never raise -- a broken/absent
    mail server must not break the donation flow, since the PDF is already
    generated and downloadable regardless."""

    def test_demo_mode_when_smtp_host_not_configured(self, app):
        # conftest's app fixture doesn't set SMTP_HOST, so this is demo mode.
        with app.app_context(), patch("email_utils.smtplib.SMTP") as mock_smtp:
            sent = send_receipt_email(_fake_donation(), _fake_donor(), {}, _fake_pdf_bytes())

        assert sent is False
        mock_smtp.assert_not_called()

    def test_skips_when_donor_has_no_email(self, app):
        app.config["SMTP_HOST"] = "smtp.example.com"
        with app.app_context(), patch("email_utils.smtplib.SMTP") as mock_smtp:
            sent = send_receipt_email(_fake_donation(), _fake_donor(email=None), {}, _fake_pdf_bytes())

        assert sent is False
        mock_smtp.assert_not_called()

    def test_sends_via_smtp_when_configured(self, app):
        app.config["SMTP_HOST"] = "smtp.example.com"
        app.config["SMTP_PORT"] = 587
        app.config["SMTP_USERNAME"] = "temple@example.com"
        app.config["SMTP_PASSWORD"] = "app-password"
        app.config["MAIL_FROM_NAME"] = "ISKCON Dwarka"

        mock_server = MagicMock()
        mock_smtp_cm = MagicMock()
        mock_smtp_cm.__enter__.return_value = mock_server

        with app.app_context(), patch("email_utils.smtplib.SMTP", return_value=mock_smtp_cm) as mock_smtp:
            donation = _fake_donation()
            donor = _fake_donor()
            sent = send_receipt_email(
                donation, donor, {"ORG_NAME": "Sri Sri Rukmini Dwarkadhish Temple"}, _fake_pdf_bytes()
            )

        assert sent is True
        mock_smtp.assert_called_once_with("smtp.example.com", 587, timeout=15)
        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once_with("temple@example.com", "app-password")
        assert mock_server.send_message.call_count == 1

        sent_msg = mock_server.send_message.call_args[0][0]
        assert sent_msg["To"] == "donor@example.com"
        assert "ISKCON Dwarka" in sent_msg["From"]
        assert "032511/ISK500000" in sent_msg["Subject"]
        attachments = list(sent_msg.iter_attachments())
        assert len(attachments) == 1
        assert attachments[0].get_filename() == "Receipt_032511_ISK500000.pdf"

    def test_smtp_failure_is_swallowed_not_raised(self, app):
        app.config["SMTP_HOST"] = "smtp.example.com"

        with app.app_context(), patch("email_utils.smtplib.SMTP", side_effect=OSError("connection refused")):
            sent = send_receipt_email(_fake_donation(), _fake_donor(), {}, _fake_pdf_bytes())

        assert sent is False

    def test_online_donation_flow_triggers_email_when_configured(self, app, client):
        """End-to-end: the full create-order -> simulate-payment flow should
        call send_receipt_email once SMTP is configured, without the API
        response changing shape or failing."""
        from models import Campaign

        app.config["SMTP_HOST"] = "smtp.example.com"
        campaign = Campaign.query.filter_by(name="Annadan").first()

        order_resp = client.post(
            "/api/create-order",
            json={
                "campaign_id": campaign.id,
                "amount": 251,
                "full_name": "Email Test Donor",
                "phone": "9123456780",
                "email": "emailtest@example.com",
                "consent": "on",
                "pan": "ABCDE1234F",  # Annadan is 80G-eligible; see REG-036
            },
        )
        donation_id = order_resp.get_json()["donation_id"]

        with patch("public.send_receipt_email", return_value=True) as mock_send:
            resp = client.post("/api/simulate-payment", json={"donation_id": donation_id})

        assert resp.status_code == 200
        assert mock_send.call_count == 1
