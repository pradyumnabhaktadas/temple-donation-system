import os
from datetime import timedelta

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'instance', 'temple.db')}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Render's managed Postgres silently drops idle connections after a
    # while; without this, SQLAlchemy can hand out a dead connection from
    # its pool and the query fails with a raw driver error (e.g. "SSL
    # error: decryption failed or bad record mac") instead of transparently
    # reconnecting. pool_pre_ping issues a cheap "is this still alive"
    # check before every checkout (auto-reconnects if not); pool_recycle
    # forces connections older than 4 minutes to be recycled preemptively,
    # comfortably under typical cloud idle-connection timeouts. No effect
    # on SQLite (used for local/demo mode).
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True, "pool_recycle": 240}

    # Without this, Flask doesn't send a Cache-Control max-age on files
    # served from /static (logo, gallery photos, style.css) -- browsers
    # end up re-validating every one of them on every single page view
    # instead of just reusing what they already downloaded. 7 days is a
    # reasonable middle ground for a site with no cache-busting/content-
    # hashed filenames set up: long enough to meaningfully speed up
    # repeat visits within the same week (a donor browsing several pages,
    # or admin staff who are here constantly), short enough that a real
    # asset update (a new gallery photo, a style.css tweak) shows up for
    # everyone within a week even without a hard cache-clear.
    SEND_FILE_MAX_AGE_DEFAULT = timedelta(days=7)

    # --- Razorpay ---
    # Leave these blank to run the app in DEMO MODE: the donation form will show
    # a "Simulate Payment Success" button instead of the real Razorpay checkout,
    # so you can test the entire flow (donor dedup, receipts, dashboard) without
    # a live payment. Fill these in with your real Razorpay keys to go live.
    RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
    RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
    # Separate secret for the Razorpay webhook (Dashboard -> Settings ->
    # Webhooks), NOT the same as RAZORPAY_KEY_SECRET above. This lets
    # Razorpay call the app directly to confirm a payment succeeded, which
    # is more reliable than only trusting the browser to call back after
    # checkout closes (donor could close the tab, lose connectivity, etc.
    # right after paying). API keys above are still required to create
    # orders and launch checkout -- the webhook is an additional, reliable
    # confirmation channel, not a replacement for them. Leave blank to run
    # without it (the existing client-side verify-payment call is still
    # the primary confirmation path).
    RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
    # NOTE: computed as a plain boolean in app.py (create_app), not as a
    # @property here -- Flask's config.from_object() reads class attributes
    # via getattr(Config, key), which would return the property descriptor
    # itself (always truthy) rather than evaluating it.

    # --- Temple / Org details (used on receipts & 80G text) ---
    # Values below match the temple's actual issued receipt (ISKCON Dwarka
    # Extension Centre, New Delhi).
    ORG_NAME = os.environ.get("ORG_NAME", "Sri Sri Rukmini Dwarkadhish Temple")
    # The umbrella organisation line shown as the main receipt heading.
    ORG_PARENT_NAME = os.environ.get(
        "ORG_PARENT_NAME", "International Society for Krishna Consciousness (ISKCON)"
    )
    ORG_FOUNDER_LINE = os.environ.get(
        "ORG_FOUNDER_LINE", "Founder-Acharya: His Divine Grace A. C. Bhaktivedanta Swami Prabhupada"
    )
    # Short branch identifier shown next to the logo (e.g. "Dwarka Colony, New Delhi").
    # Baked into the logo image itself now, kept here only as a fallback for
    # the vector placeholder used when ORG_LOGO_PATH isn't found.
    ORG_BRANCH_SHORT_NAME = os.environ.get("ORG_BRANCH_SHORT_NAME", "Dwarka Colony, New Delhi")
    # What kind of branch this is, prefixing the address box on the receipt
    # (e.g. "Extension Centre: ISKCON Land").
    ORG_BRANCH_TYPE = os.environ.get("ORG_BRANCH_TYPE", "Extension Centre")
    # Local branch address, one line per line-break -- matches the exact line
    # breaks used in the address box on the temple's actual receipt template.
    # The first line is appended to ORG_BRANCH_TYPE on one line; the rest are
    # shown on their own lines below it.
    ORG_ADDRESS = os.environ.get(
        "ORG_ADDRESS",
        "ISKCON Land\nSector 13, Behind Redisson Blue Hotel\nDwarka, New Delhi - 110075",
    )
    ORG_PAN = os.environ.get("ORG_PAN", "AAATI0017P")
    ORG_80G_REG_NO = os.environ.get("ORG_80G_REG_NO", "AAATI0017PF20219")
    ORG_LOGO_TEXT = os.environ.get("ORG_LOGO_TEXT", "🕉")
    # Path (relative to the project root) to the temple's logo image, used on
    # the receipt PDF. Leave blank to fall back to a plain vector placeholder.
    ORG_LOGO_PATH = os.environ.get(
        "ORG_LOGO_PATH", os.path.join(BASE_DIR, "static", "branding", "iskcon_logo_v2.png")
    )
    # Branch-level contact (shown in the address box on the receipt).
    ORG_PHONE = os.environ.get("ORG_PHONE", "8527405353")
    ORG_EMAIL = os.environ.get("ORG_EMAIL", "acct.iskcondwarka@gmail.com")
    # Head office / registered office details -- shown only in the small-print
    # footer, separate from the branch contact above (the real receipt uses a
    # different phone/email for the registered office vs. the local branch).
    ORG_HO_ADDRESS = os.environ.get("ORG_HO_ADDRESS", "Hare Krishna Land, Juhu, Mumbai - 400 049")
    ORG_HO_PHONE = os.environ.get("ORG_HO_PHONE", "72088 46210")
    ORG_HO_EMAIL = os.environ.get("ORG_HO_EMAIL", "info@iskconindia.org")
    # Single line shown in the small print on receipts, e.g. "Registered under
    # the Indian Trusts Act, 1882 | Regn. No. XXXX". Leave blank to omit.
    ORG_REG_INFO = os.environ.get(
        "ORG_REG_INFO", "Registered under Maharashtra Public Trust Act 1950, vide Regn. No.: F-2179 (Bom)"
    )
    # Closing lines on the Terms & Conditions page, matching the temple's
    # actual receipt backside verbatim. Split on " / " for separate lines.
    ORG_CLOSING_MESSAGE = os.environ.get(
        "ORG_CLOSING_MESSAGE",
        "Please chant / "
        "HARE KRISHNA HARE KRISHNA KRISHNA KRISHNA HARE HARE / "
        "HARE RAMA HARE RAMA RAMA RAMA HARE HARE / "
        "and be happy.",
    )

    # --- Public site footer: About Us / Contact / Location ---
    # Deliberately separate from ORG_ADDRESS/ORG_EMAIL above -- those feed
    # the legal 80G receipt text and shouldn't change casually. These are
    # just what's shown to visitors in the website footer.
    ORG_ABOUT_TEXT = os.environ.get(
        "ORG_ABOUT_TEXT",
        "ISKCON Dwarka Delhi Temple is an initiative of ISKCON Youth Forum (IYF), Dwarka Delhi, "
        "dedicated to spreading Krishna consciousness through devotional service, festivals, and "
        "community seva.",
    )
    ORG_CONTACT_ADDRESS = os.environ.get(
        "ORG_CONTACT_ADDRESS",
        "Plot No.-4, Sub-City Level, Dwarka Sector-13, Behind Radisson Blue Hotel, Delhi-110075",
    )
    ORG_CONTACT_EMAIL = os.environ.get("ORG_CONTACT_EMAIL", "livetogive.dwarka@gmail.com")
    # Shown in the website footer/About page as a click-to-call and
    # click-to-WhatsApp number. Stored plain (10 digits, optionally with a
    # leading +91) -- normalize_phone() cleans it up before use.
    ORG_CONTACT_PHONE = os.environ.get("ORG_CONTACT_PHONE", "+91 93156 22933")

    # Applies to both admin sessions (Flask-Login) and donor OTP-login
    # sessions -- Flask's session lifetime is app-wide, not per-login-type.
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)

    # --- Security ---
    # Set FLASK_ENV=production (or ENV=production) when you deploy behind
    # HTTPS so session/CSRF cookies are marked Secure. Leave unset for local
    # http://localhost development, or the browser will silently drop the
    # cookies and login will appear to "not work".
    IS_PRODUCTION = os.environ.get("FLASK_ENV") == "production" or os.environ.get("ENV") == "production"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = IS_PRODUCTION
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SECURE = IS_PRODUCTION

    # No expiry on CSRF tokens -- a donor filling out the donation form
    # shouldn't hit a stale-token error just because they took a while.
    WTF_CSRF_TIME_LIMIT = None

    # Admin login lockout
    LOGIN_MAX_ATTEMPTS = int(os.environ.get("LOGIN_MAX_ATTEMPTS", 5))
    LOGIN_LOCKOUT_MINUTES = int(os.environ.get("LOGIN_LOCKOUT_MINUTES", 15))

    # --- Consent (DPDP Act) ---
    # Bump this whenever the consent checkbox's wording on the donation
    # form materially changes, so `Donation.consent_version` records which
    # exact wording a given donor actually agreed to, not just that some
    # version of it existed.
    CONSENT_VERSION = os.environ.get("CONSENT_VERSION", "2026-07")

    # --- Security headers (Flask-Talisman) ---
    # HSTS, clickjacking (X-Frame-Options), MIME-sniffing (X-Content-Type-
    # Options), and Referrer-Policy are always applied in production (see
    # IS_PRODUCTION above). Content-Security-Policy is also on by default,
    # allow-listing exactly the external resources this app actually loads
    # (Bootstrap/Chart.js from jsdelivr, Google Fonts, Razorpay checkout) --
    # see app.py's create_app(). If a future template change ever needs a
    # new external resource and the CSP starts blocking something
    # unexpectedly, set this to false to disable just the CSP header
    # without touching HSTS/clickjacking/etc., then update the allow-list
    # in app.py at your leisure.
    CONTENT_SECURITY_POLICY_ENABLED = os.environ.get(
        "CONTENT_SECURITY_POLICY_ENABLED", "true"
    ).strip().lower() not in ("false", "0", "no")

    # --- Error monitoring (Sentry) ---
    # Leave blank to run without error monitoring (the default). Set to a
    # real Sentry DSN (Settings -> Projects -> <project> -> Client Keys) to
    # start reporting unhandled exceptions -- see README "Error monitoring"
    # for setup.
    SENTRY_DSN = os.environ.get("SENTRY_DSN", "")

    # --- Receipt emailing (SMTP) ---
    # Leave SMTP_HOST blank to run in DEMO MODE: receipts are still generated
    # as PDFs and downloadable from the admin panel / donor portal, but no
    # email is sent. Fill these in (Gmail works with an App Password -- see
    # README "Emailing receipts") to email the receipt PDF to the donor
    # automatically whenever a donation succeeds.
    SMTP_HOST = os.environ.get("SMTP_HOST", "")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
    SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "true").strip().lower() not in ("false", "0", "no")
    # Shown as the "From" address/name on receipt emails. Falls back to
    # SMTP_USERNAME if left blank.
    MAIL_FROM_ADDRESS = os.environ.get("MAIL_FROM_ADDRESS", "")
    MAIL_FROM_NAME = os.environ.get("MAIL_FROM_NAME", "")

    # --- Weekly data backup (backup_data.py) ---
    # Where the weekly backup ZIP (donors/donations/lookup lists as CSV --
    # see backup_utils.py) gets emailed, in addition to being saved under
    # instance/backups/. Falls back to ORG_CONTACT_EMAIL if left blank, since
    # that inbox is already checked regularly.
    BACKUP_EMAIL = os.environ.get("BACKUP_EMAIL", "")
    # How many backups to keep on disk before pruning the oldest -- default
    # 12 covers ~3 months of weekly backups.
    BACKUP_RETENTION_COUNT = int(os.environ.get("BACKUP_RETENTION_COUNT", 12))

    # --- Receipt delivery over WhatsApp (Airtel IQ WhatsApp Business API) ---
    # Leave WHATSAPP_AIRTEL_USERNAME/PASSWORD blank to run in DEMO MODE:
    # receipts are still generated and emailed/downloadable as before, just
    # not sent over WhatsApp. See README "Sending receipts via WhatsApp" and
    # whatsapp_utils.py for the full explanation.
    #
    # Secrets -- never hardcode real values here (this file is committed to
    # git); set them in .env locally / the host's Environment tab in
    # production, same as RAZORPAY_KEY_SECRET etc. requests' own
    # auth=(username, password) builds the "Basic ..." header from these --
    # no base64 encoding to do by hand.
    WHATSAPP_AIRTEL_USERNAME = os.environ.get("WHATSAPP_AIRTEL_USERNAME", "")
    WHATSAPP_AIRTEL_PASSWORD = os.environ.get("WHATSAPP_AIRTEL_PASSWORD", "")
    # Not secret (a WhatsApp Business number is donor-facing by design, and
    # a template ID just names an approved message) -- defaults match the
    # temple's actual Airtel account so a fresh checkout of this repo Just
    # Works once WHATSAPP_AIRTEL_USERNAME/PASSWORD above are filled in.
    WHATSAPP_FROM_NUMBER = os.environ.get("WHATSAPP_FROM_NUMBER", "918178798462")
    WHATSAPP_TEMPLATE_ID = os.environ.get("WHATSAPP_TEMPLATE_ID", "01kzdy128ke65be98yhg9fjazx")
    WHATSAPP_AIRTEL_BASE_URL = os.environ.get("WHATSAPP_AIRTEL_BASE_URL", "")  # has a working default in whatsapp_utils.py
    # See the ⚠️ note in whatsapp_utils.py before setting this -- likely a
    # session-scoped value, not a stable API credential. Leave blank unless
    # Airtel/your team confirms it's actually required.
    WHATSAPP_AIRTEL_COOKIE = os.environ.get("WHATSAPP_AIRTEL_COOKIE", "")
    # The site's own public URL, used to build the receipt link Airtel's
    # servers fetch the PDF from (see whatsapp_utils.py) -- also handy as a
    # single place to change if the domain ever changes.
    PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://givetokrishna.com")

    # --- Donor OTP login ---
    # No SMS provider is wired up yet (see sms_utils.py) -- OTPs are shown
    # directly on the verify page instead of texted, clearly marked "Demo
    # Mode", so you can test the whole login flow before picking a vendor.
    OTP_LENGTH = 6
    OTP_EXPIRY_MINUTES = int(os.environ.get("OTP_EXPIRY_MINUTES", 10))
    OTP_MAX_VERIFY_ATTEMPTS = int(os.environ.get("OTP_MAX_VERIFY_ATTEMPTS", 5))
    OTP_MAX_REQUESTS_PER_HOUR = int(os.environ.get("OTP_MAX_REQUESTS_PER_HOUR", 5))
