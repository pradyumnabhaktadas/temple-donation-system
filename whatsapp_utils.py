"""Sending donation receipts to donors over WhatsApp, via Airtel IQ's
WhatsApp Business API (iqwhatsapp.airtel.in) -- what this temple actually
has access to. (An earlier version of this file called Meta's WhatsApp
Cloud API directly; Airtel was the account actually available, so this was
swapped over. If you ever switch providers again, only this file needs to
change -- nothing in public.py/admin.py calling send_receipt_whatsapp()
does.)

DEMO MODE (current state until the env vars below are set): send_receipt_whatsapp()
doesn't actually send anything -- it just returns False so callers know
nothing went out. Same pattern as email_utils.py/sms_utils.py: the rest of
the donation flow works identically whether or not this is wired up.

HOW THIS WORKS: unlike Meta's Cloud API (which needs the PDF uploaded as
"media" first, then referenced by ID), Airtel's endpoint just wants a
publicly-fetchable URL to the file -- their servers download it themselves.
We already have exactly that: the existing /receipt/<id> route serves the
stored receipt PDF straight from the database with no auth required (same
route donors' own "Download receipt" links use), so PUBLIC_BASE_URL below
plus that route path is all that's needed -- no new upload/storage code.

REQUIRED ENV VARS TO GO LIVE:
    WHATSAPP_AIRTEL_USERNAME     -- HTTP Basic auth username Airtel gave you
    WHATSAPP_AIRTEL_PASSWORD     -- HTTP Basic auth password Airtel gave you
                                    (requests builds the "Basic ..." header
                                    from these two -- no manual base64
                                    encoding needed/stored anywhere)
    WHATSAPP_FROM_NUMBER         -- the temple's registered WhatsApp Business
                                    number Airtel assigned
    WHATSAPP_TEMPLATE_ID         -- the approved template's ID from Airtel's
                                    WhatsApp Manager
    PUBLIC_BASE_URL              -- the site's own public URL (e.g.
                                    https://givetokrishna.com), used to build
                                    the receipt URL Airtel fetches the PDF from

Optional:
    WHATSAPP_AIRTEL_BASE_URL     -- Airtel's send endpoint (has a working default)
    WHATSAPP_AIRTEL_COOKIE       -- see the note in _headers() below before
                                    relying on this in production

DAILY REPORT TEMPLATE (send_daily_report_whatsapp, below): a *separate*
template from the receipt one above, since WhatsApp Business templates are
approved for one fixed set of variables and this isn't donor-facing. Not
live yet -- WHATSAPP_REPORT_TEMPLATE_ID (config.py) is blank until a
template matching this exact 5-variable order is submitted to and approved
by Airtel/Meta:
    {{1}} = org name
    {{2}} = report date, e.g. "29 Aug 2026"
    {{3}} = today's collection, e.g. "Rs. 12,500 (4 donations)"
    {{4}} = this week's collection, same format
    {{5}} = this month's collection, same format
No document attachment (plain text/template only) -- the campaign-wise
breakdown is in the accompanying email, not the WhatsApp message.

⚠️ Two things about the original curl example this was built from that are
worth flagging back to whoever manages the Airtel account, since neither
could be verified against Airtel's own docs from here:
  1. The `X-Date` header's exact expected format wasn't specified anywhere
     in the example (it was a literal "{{date}}" placeholder) -- this
     guesses the standard HTTP-date format (RFC 7231, e.g. "Wed, 21 Oct
     2026 07:28:00 GMT"). If sends start failing with an auth/date error,
     this is the first thing to check against Airtel's actual API reference.
  2. The example curl included a `Cookie` header with two session-looking
     values. Session cookies captured from a browser/Postman session are
     usually tied to that login session and expire -- they're not normally
     part of a stable server-to-server API contract the way the
     Authorization header is. This integration does NOT send a Cookie
     header unless WHATSAPP_AIRTEL_COOKIE is explicitly set -- worth
     confirming with Airtel/your team whether it's actually required before
     setting it, rather than copying the captured value in blind.
"""
import datetime
import uuid

import requests
from flask import current_app

from utils import format_inr, receipt_access_token, retry


DEFAULT_AIRTEL_BASE_URL = "https://iqwhatsapp.airtel.in/gateway/airtel-xchange/basic/whatsapp-manager/v1/template/send"


def send_receipt_whatsapp(donation, donor, org_cfg, pdf_bytes):
    """Sends the receipt PDF to `donor` over WhatsApp via Airtel. Returns
    True if it was actually sent, False if skipped (demo mode, or donor has
    no WhatsApp-reachable number on file). Never raises -- same contract as
    send_receipt_email(): a failed/unconfigured send should never break the
    donation or receipt-generation flow. `pdf_bytes` isn't uploaded directly
    (see module docstring) -- it's assumed to already be saved on
    `donation.receipt_pdf` (true by the time this is called from
    _finalize_success()) and fetched by Airtel from the public receipt URL.
    """
    phone = donor.whatsapp_or_phone
    if not phone:
        return False

    cfg = current_app.config
    username = cfg.get("WHATSAPP_AIRTEL_USERNAME")
    password = cfg.get("WHATSAPP_AIRTEL_PASSWORD")
    from_number = cfg.get("WHATSAPP_FROM_NUMBER")
    template_id = cfg.get("WHATSAPP_TEMPLATE_ID")
    public_base_url = cfg.get("PUBLIC_BASE_URL")
    if not (username and password and from_number and template_id and public_base_url):
        return False  # DEMO MODE: nothing was actually sent

    try:
        base_url = cfg.get("WHATSAPP_AIRTEL_BASE_URL") or DEFAULT_AIRTEL_BASE_URL
        org_name = org_cfg.get("ORG_PARENT_NAME") or org_cfg.get("ORG_NAME") or "the temple"
        # The signed token is required now that /receipt/<id> is no longer
        # open to anyone who can guess an id (see utils.receipt_access_token
        # and public._may_download_receipt). Airtel fetches this URL
        # server-side with no session of its own, so the token in the URL
        # is the only thing that can authorise it -- without it, every
        # WhatsApp receipt would come back 404.
        receipt_token = receipt_access_token(donation.id, cfg["SECRET_KEY"])
        receipt_url = f"{public_base_url.rstrip('/')}/receipt/{donation.id}?t={receipt_token}"

        payload = {
            "templateId": template_id,
            "to": _to_e164(phone),
            "from": _to_e164(from_number),
            "message": {
                # Positional, must match the approved template's {{1}} {{2}}
                # {{3}} order exactly: donor name, amount, org name -- same
                # 3-variable template described in README "Sending receipts
                # via WhatsApp".
                "variables": [
                    (donor.full_name or "Donor")[:60],
                    f"{donation.amount:,.2f}",
                    org_name[:60],
                ]
            },
            "mediaAttachment": {
                "type": "DOCUMENT",
                "fileName": f"Receipt_{(donation.receipt_number or 'receipt').replace('/', '_')}",
                "URL": receipt_url,
            },
        }
        resp = requests.post(
            base_url, headers=_headers(cfg), auth=(username, password), json=payload, timeout=15
        )
        if not resp.ok:
            current_app.logger.error(
                "WhatsApp (Airtel) receipt send failed for donation %s: %s %s",
                donation.receipt_number, resp.status_code, resp.text[:500],
            )
            return False
        return True
    except Exception:
        # Same policy as email: WhatsApp delivery is additive on top of the
        # always-available PDF download, never let a hiccup here (bad
        # token, network blip, Airtel outage) surface as a donation failure.
        current_app.logger.exception(
            "Failed to send WhatsApp receipt %s to %s", donation.receipt_number, phone
        )
        return False


def send_daily_report_whatsapp(cfg, phone, report_data, org_name):
    """Sends the 4 AM daily collection report (see daily_report_utils.py)
    to `phone` via Airtel, using WHATSAPP_REPORT_TEMPLATE_ID -- a distinct
    template from the receipt one (see module docstring for the required
    variable order). Returns True if sent, False if skipped (demo mode: no
    report template configured yet). Never raises, same contract as
    send_receipt_whatsapp."""
    if not phone:
        return False

    username = cfg.get("WHATSAPP_AIRTEL_USERNAME")
    password = cfg.get("WHATSAPP_AIRTEL_PASSWORD")
    from_number = cfg.get("WHATSAPP_FROM_NUMBER")
    template_id = cfg.get("WHATSAPP_REPORT_TEMPLATE_ID")
    if not (username and password and from_number and template_id):
        return False  # DEMO MODE: no report template approved/configured yet

    try:
        base_url = cfg.get("WHATSAPP_AIRTEL_BASE_URL") or DEFAULT_AIRTEL_BASE_URL
        report_date = report_data["report_date"]

        def fmt(period):
            # Matches the exact sample values ("Rs.12,500") the template was
            # submitted for approval with -- Indian digit grouping via
            # format_inr, no decimals, no space after "Rs.". Free-text
            # template variables aren't usually validated against their
            # approval-time sample, but there's no reason to risk it when
            # matching costs nothing.
            return f"Rs.{format_inr(period['amount'])}"

        payload = {
            "templateId": template_id,
            "to": _to_e164(phone),
            "from": _to_e164(from_number),
            "message": {
                # Positional, must match the approved report template's
                # {{1}}..{{5}} order exactly -- see this module's docstring.
                "variables": [
                    org_name[:60],
                    report_date.strftime("%d-%b-%Y"),
                    fmt(report_data["today"])[:100],
                    fmt(report_data["week"])[:100],
                    fmt(report_data["month"])[:100],
                ]
            },
        }

        def _send():
            resp = requests.post(
                base_url, headers=_headers(cfg), auth=(username, password), json=payload, timeout=15
            )
            if not resp.ok:
                # Raised (not returned) so retry() below treats a non-2xx
                # response the same as a connection/timeout failure -- both
                # are worth retrying for this unattended send.
                raise RuntimeError(f"{resp.status_code} {resp.text[:500]}")
            return resp

        # This is the one send in this file that runs unattended (the 4 AM
        # cron job, not a donor waiting on a page) -- see retry()'s
        # docstring for why a couple of retries is worth it here but not for
        # send_receipt_whatsapp above.
        resp = retry(
            _send,
            on_retry=lambda attempt, exc: current_app.logger.warning(
                "Daily report WhatsApp attempt %d to %s failed, retrying: %s", attempt, phone, exc
            ),
        )
        # Airtel returning 2xx here only means the request was *accepted*,
        # not that WhatsApp actually delivered it (a template mismatch, an
        # unreachable/DND number, or a template still propagating on
        # Airtel's side after approval can all silently drop it downstream
        # with no error back to us) -- log the response body so a "the
        # script says sent but nothing arrived" report can be cross-checked
        # against this message/status id in Airtel's WhatsApp Manager
        # dashboard instead of being a dead end.
        current_app.logger.info(
            "WhatsApp (Airtel) daily report accepted for %s: %s %s",
            phone, resp.status_code, resp.text[:500],
        )
        return True
    except Exception:
        current_app.logger.exception("Failed to send WhatsApp daily report to %s", phone)
        return False


def _headers(cfg):
    # Authorization is deliberately not built here -- requests' own
    # auth=(username, password) kwarg (see the call above) constructs a
    # byte-identical "Basic <base64>" header, so the credential never has to
    # be manually base64-encoded/stored as an opaque blob.
    headers = {
        "X-Correlation-Id": str(uuid.uuid4()),
        "X-Date": datetime.datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT"),
        "Content-Type": "application/json",
        # `requests` defaults to "python-requests/x.x" otherwise -- a manual
        # `curl` test against this same endpoint (same server, same Kong
        # gateway visible in its response headers) got a clean 200, while
        # the app's actual send got RemoteDisconnected with zero HTTP
        # response at all. That pattern -- connection reset before any
        # response, only from the library call -- points at a WAF/bot rule
        # on Airtel's Kong gateway that's specifically rejecting the
        # generic python-requests User-Agent. Overriding it here is the
        # cheapest thing to try before assuming something deeper (TLS
        # fingerprinting, rate limiting, etc.) is going on.
        "User-Agent": "TempleDonationSystem/1.0 (+https://givetokrishna.com)",
    }
    # See the module docstring's ⚠️ note #2 before setting this in
    # production -- only included at all if explicitly configured.
    cookie = cfg.get("WHATSAPP_AIRTEL_COOKIE")
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _to_e164(phone):
    """Normalizes a stored phone number into the digits-only international
    format the API expects (e.g. "919876543210"). Two shapes come out of
    utils.normalize_phone(): a plain 10-digit Indian mobile number (no
    country code stored -- prefixed with "91" here), or a foreign number
    already stored as "+<country code><number>" (e.g. "+14155552671") --
    for that shape, stripping the digits out is already exactly the E.164
    format Airtel wants, no prefixing needed."""
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) == 10:
        return f"91{digits}"
    return digits
