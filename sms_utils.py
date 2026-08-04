"""Donor OTP generation and delivery.

DEMO MODE (current state): no SMS provider is wired up yet. send_otp()
below doesn't actually text anything -- it returns the OTP so the calling
route can display it directly on the verify page with a "Demo Mode" banner.
This lets you test the entire donor login flow end-to-end before you've
picked/paid for an SMS vendor.

TO GO LIVE: pick a provider (MSG91, Fast2SMS, Twilio, AWS SNS, etc.), add
their API call inside send_otp() where marked below, and change the
`demo_mode=True` return so callers stop displaying the code on-screen.
Nothing else in the donor login flow (routes, templates, OTP hashing/
expiry/rate-limiting) needs to change -- they already just call this
function and check whether it ran in demo mode.
"""
import secrets


def generate_otp(length=6):
    """Cryptographically-random numeric OTP, e.g. '048213'."""
    return "".join(secrets.choice("0123456789") for _ in range(length))


def send_otp(phone, otp):
    """Sends the OTP to `phone`. Returns True if actually sent via a real
    provider, or False if running in demo mode (caller should show the OTP
    on-screen instead).
    """
    # --- Real SMS sending goes here once you've picked a provider ---
    # Example shape (MSG91's OTP API):
    #
    #   import requests
    #   resp = requests.post(
    #       "https://api.msg91.com/api/v5/otp",
    #       params={"template_id": "...", "mobile": f"91{phone}", "otp": otp},
    #       headers={"authkey": current_app.config["SMS_API_KEY"]},
    #       timeout=10,
    #   )
    #   return resp.ok
    #
    # ------------------------------------------------------------------

    return False  # DEMO MODE: nothing was actually sent
