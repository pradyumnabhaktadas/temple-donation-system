import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from whatsapp_utils import send_receipt_whatsapp, _to_e164


def _fake_donation(donation_id=42):
    return SimpleNamespace(id=donation_id, receipt_number="032511/ISK500000", amount=501.0)


def _fake_donor(whatsapp_or_phone="9876543210", full_name="Test Donor"):
    return SimpleNamespace(whatsapp_or_phone=whatsapp_or_phone, full_name=full_name)


def _fake_pdf_bytes():
    return b"%PDF-1.4 fake receipt content"


def _configure(app):
    app.config["WHATSAPP_AIRTEL_USERNAME"] = "test-user"
    app.config["WHATSAPP_AIRTEL_PASSWORD"] = "test-pass"
    app.config["WHATSAPP_FROM_NUMBER"] = "918178798462"
    app.config["WHATSAPP_TEMPLATE_ID"] = "01kzdy128ke65be98yhg9fjazx"
    app.config["PUBLIC_BASE_URL"] = "https://givetokrishna.com"


class TestWhatsAppReceipt:
    """Receipt delivery over WhatsApp uses the Airtel IQ WhatsApp Business
    API directly (see whatsapp_utils.py), gated on WHATSAPP_AIRTEL_USERNAME/
    WHATSAPP_AIRTEL_PASSWORD/WHATSAPP_FROM_NUMBER/WHATSAPP_TEMPLATE_ID/
    PUBLIC_BASE_URL, matching the
    demo-mode pattern used elsewhere in this codebase (Razorpay, SMS OTP,
    SMTP email). It should never raise -- a broken/unconfigured send must
    not break the donation flow, since the PDF is already generated,
    emailed, and downloadable regardless. Unlike Meta's Cloud API, Airtel
    doesn't need a separate media-upload step -- it fetches the PDF itself
    from a public URL, so sending is a single POST."""

    def test_demo_mode_when_not_configured(self, app):
        # conftest's app fixture pins WHATSAPP_*/PUBLIC_BASE_URL to empty,
        # so this is demo mode. That pinning matters: app.py calls
        # load_dotenv(), so before it this test read the developer's own
        # .env and started failing the moment real Airtel credentials were
        # added there -- the send genuinely wasn't in demo mode any more.
        with app.app_context(), patch("whatsapp_utils.requests.post") as mock_post:
            sent = send_receipt_whatsapp(_fake_donation(), _fake_donor(), {}, _fake_pdf_bytes())

        assert sent is False
        mock_post.assert_not_called()

    def test_skips_when_donor_has_no_whatsapp_reachable_number(self, app):
        _configure(app)
        with app.app_context(), patch("whatsapp_utils.requests.post") as mock_post:
            sent = send_receipt_whatsapp(
                _fake_donation(), _fake_donor(whatsapp_or_phone=None), {}, _fake_pdf_bytes()
            )

        assert sent is False
        mock_post.assert_not_called()

    def test_sends_via_airtel_when_configured(self, app):
        _configure(app)
        send_resp = MagicMock(ok=True)

        with app.app_context(), patch("whatsapp_utils.requests.post", return_value=send_resp) as mock_post:
            donation = _fake_donation()
            donor = _fake_donor()
            sent = send_receipt_whatsapp(
                donation, donor, {"ORG_NAME": "Sri Sri Rukmini Dwarkadhish Temple"}, _fake_pdf_bytes()
            )

        assert sent is True
        assert mock_post.call_count == 1

        call = mock_post.call_args
        assert call.args[0] == "https://iqwhatsapp.airtel.in/gateway/airtel-xchange/basic/whatsapp-manager/v1/template/send"

        headers = call.kwargs["headers"]
        assert "Authorization" not in headers  # built by requests' auth= kwarg instead
        assert call.kwargs["auth"] == ("test-user", "test-pass")
        assert "X-Correlation-Id" in headers
        assert "X-Date" in headers
        assert "Cookie" not in headers  # not configured -> not sent

        payload = call.kwargs["json"]
        assert payload["templateId"] == "01kzdy128ke65be98yhg9fjazx"
        assert payload["to"] == "919876543210"
        assert payload["from"] == "918178798462"
        assert payload["message"]["variables"] == ["Test Donor", "501.00", "Sri Sri Rukmini Dwarkadhish Temple"]
        assert payload["mediaAttachment"]["type"] == "DOCUMENT"
        # The signed token is not optional here: Airtel fetches this URL
        # server-side with no session of its own, so it's the only thing
        # authorising the download now that /receipt/<id> is no longer open
        # to anyone who can guess an id. Without it every WhatsApp receipt
        # would come back 404.
        from utils import receipt_access_token
        expected_token = receipt_access_token(42, app.config["SECRET_KEY"])
        assert payload["mediaAttachment"]["URL"] == (
            f"https://givetokrishna.com/receipt/42?t={expected_token}"
        )

    def test_optional_cookie_header_only_sent_when_configured(self, app):
        _configure(app)
        app.config["WHATSAPP_AIRTEL_COOKIE"] = "a=1; b=2"
        send_resp = MagicMock(ok=True)

        with app.app_context(), patch("whatsapp_utils.requests.post", return_value=send_resp) as mock_post:
            send_receipt_whatsapp(_fake_donation(), _fake_donor(), {}, _fake_pdf_bytes())

        assert mock_post.call_args.kwargs["headers"]["Cookie"] == "a=1; b=2"

    def test_send_failure_returns_false_without_raising(self, app):
        _configure(app)
        send_resp = MagicMock(ok=False, status_code=401, text="invalid token")

        with app.app_context(), patch("whatsapp_utils.requests.post", return_value=send_resp):
            sent = send_receipt_whatsapp(_fake_donation(), _fake_donor(), {}, _fake_pdf_bytes())

        assert sent is False

    def test_network_exception_is_swallowed_not_raised(self, app):
        _configure(app)
        with app.app_context(), patch("whatsapp_utils.requests.post", side_effect=OSError("connection refused")):
            sent = send_receipt_whatsapp(_fake_donation(), _fake_donor(), {}, _fake_pdf_bytes())

        assert sent is False

    def test_online_donation_flow_triggers_whatsapp_when_configured(self, app, client):
        """End-to-end: the full create-order -> simulate-payment flow should
        call send_receipt_whatsapp once configured, without the API
        response changing shape or failing."""
        from models import Campaign

        _configure(app)
        campaign = Campaign.query.filter_by(name="Annadan").first()

        order_resp = client.post(
            "/api/create-order",
            json={
                "campaign_id": campaign.id,
                "amount": 251,
                "full_name": "WhatsApp Test Donor",
                "phone": "9123456780",
                "consent": "on",
                "pan": "ABCDE1234F",  # Annadan is 80G-eligible; see REG-036
            },
        )
        donation_id = order_resp.get_json()["donation_id"]

        with patch("public.send_receipt_whatsapp", return_value=True) as mock_send:
            resp = client.post("/api/simulate-payment", json={"donation_id": donation_id})

        assert resp.status_code == 200
        assert mock_send.call_count == 1


class TestToE164:
    def test_ten_digit_indian_number_gets_country_code_prefixed(self):
        assert _to_e164("9876543210") == "919876543210"

    def test_already_prefixed_number_is_left_alone(self):
        assert _to_e164("919876543210") == "919876543210"

    def test_strips_non_digit_formatting(self):
        assert _to_e164("+91 98765-43210") == "919876543210"
