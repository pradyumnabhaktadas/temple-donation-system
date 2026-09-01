# Temple Donation & Collection Management System

A donation portal that replaces the multi-form / multi-spreadsheet Zoho
setup with a single donation form, a unified donor database, automated
receipts, a live collection dashboard, and admin tooling to manage it all.

## What this covers

- **One donation form**, not one per campaign. Donors pick a purpose
  (80G-eligible or not) from a dropdown; every submission lands in the same
  database.
- **Single donor database** deduplicated on PAN, then phone, then email, so
  a repeat donor never creates a second record. PAN format is validated
  (10-character India PAN structure) before it's accepted.
- **Per-purpose 80G eligibility (Live To Give)** — only six donation
  purposes are actually 80G-eligible: Food for Life, Charity, Donation,
  Life Membership, Construction, and Annadan. Every other purpose is
  strictly Non-80G. This is set per purpose (`LiveToGivePurpose.is_80g`)
  from **Admin → Live To Give Purposes**, not left to the donor to choose
  freely — picking a non-eligible purpose on the donation form hides the
  80G receipt option entirely, and `Donation.effective_is_80g` enforces
  this server-side regardless of what a request claims. **One-time step
  after deploying this**: existing purposes default to Non-80G (a fresh
  column backfills to `False`), so go to Admin → Live To Give Purposes and
  click "Mark 80G Eligible" on whichever of your existing purposes match
  the six categories above.
- **High-value PAN/address enforcement** — for any donation over Rs. 49,000,
  PAN and address become mandatory (not optional), in line with Income Tax
  Rule 114B's PAN-quoting requirement for transactions near the Rs. 50,000
  mark. Enforced server-side (`high_value_pan_address_error()` in
  `public.py`, shared by the online form, manual admin entry, and bulk CSV
  import) with matching client-side field toggling on the donation forms.
  Deliberately **not** applied to the legacy-donor-history importer, since
  those rows represent receipts already issued externally under the old
  system.
- **Razorpay integration** for online payments (order creation + signature
  verification), with a **demo mode** that simulates payment success when no
  live keys are configured, so you can test the whole flow first.
- **Automatic PDF receipts**, sequentially numbered with the temple's own
  scheme: one running counter shared by every donation, starting at
  `032511/ISK500000` and counting up by 1 (`032511/ISK500001`, ...)
  regardless of 80G status or financial year. Emailed automatically, and
  sent over **WhatsApp** automatically once configured (see "Sending
  receipts via WhatsApp" below) -- both are additive on top of the
  always-available download link, never a replacement for it.
- **Admin panel**: dashboard (today/month/year, campaign and payment-mode
  breakdowns, 6-month trend, campaign progress bars, a failed/abandoned
  online-donation alert for proactive donor follow-up), donor search &
  history, donation log, manual entry for offline (cash/cheque/bank
  transfer) donations, campaign management, CSV exports, a lapsed-donor
  report, multi-account admin/staff user management, and an activity log
  auditing who made which change (donor edits/merges, donation cancel/
  restore, campaign changes, account management).
- **Donor tools**: edit a donor's details, and merge a duplicate donor
  record into the correct one (moves their donations across, then removes
  the duplicate).
- **Donor self-service login** (`/my-donations`) secured with **mobile OTP**
  (not just a lookup by phone/email) — donors verify a one-time code sent
  to their phone before seeing anything. Once logged in they can view their
  full donation history, download receipts, edit their own contact/address/
  PAN details, and download a **consolidated annual statement** (one PDF of
  everything they gave in a chosen financial year, with an 80G/non-80G
  split and grand total) — also available to staff from a donor's admin
  page.
- **Security**: CSRF protection on every form, login lockout after repeated
  failed attempts, forced password change on first login, role-based access
  (see below), and hardened session cookies for production.
- **Historical data import** (`import_legacy_data.py`) for bringing your
  old Excel/Zoho Forms data into the unified database.
- **Automated tests** (`tests/`) covering donor dedup, receipt numbering,
  financial-year math, PAN validation, and key routes.
- **Branded 404/500 error pages** and basic **SEO/sharing polish** (meta
  description, Open Graph/Twitter Card tags, favicon, `robots.txt`,
  `sitemap.xml`) on every public page.

## Admin roles

Two roles exist on `AdminUser.role`:

- **admin** — everything, including creating/editing/deleting campaigns and
  merging duplicate donor records.
- **staff** — day-to-day work: logging offline donations, viewing donors,
  donations, dashboard, and the lapsed-donor report. Can't touch campaigns
  or merge donors.

Create additional admin accounts directly in the database for now (there's
no "invite a user" screen yet — see the Known Gaps section).

## Important compliance note (80G)

The PDF this app generates the moment a donation succeeds is an **instant
donation receipt**, not the official Section 80G tax certificate. Under
current Income Tax rules, your trust must file **Form 10BD** annually
listing every 80G donation, and the donor's actual certificate
(**Form 10BE**, with an IT-Department-issued ARN) is generated by the IT
portal after that filing. Use **Admin → Dashboard → "Download Form 10BD
data (CSV)"** to get the data in the columns you'll need for that annual
filing — the dashboard also shows a reminder banner every April/May as the
31 May filing deadline approaches. Don't represent the app-generated PDF to
donors as their final 80G certificate — the receipt text already says
this, but your office should know it too.

The donation form also asks donors to consent to their data being stored
for receipting and Form 10BD reporting purposes (a lightweight nod to
India's DPDP Act — see Known Gaps for what's not covered).

**Receipt number guarantees** (this data ultimately feeds the Form 10BD
filing, so these are enforced, not just conventions):
- **Unique and sequential** — numbered `032511/ISK500000`, `032511/ISK500001`,
  ... from one running counter shared by every donation (no split by 80G
  status or financial year — that's `Donation.financial_year`, tracked
  separately for annual statements and the 80G-only Form 10BD export),
  assigned by `ReceiptCounter.next_receipt_number()`. The counter row is
  locked (`with_for_update()`) when read, and `Donation.receipt_number` also
  has a database-level unique constraint as a hard backstop.
- **Only issued on a successful payment** — online donations start as
  `status="pending"` with no receipt number; a number is only assigned once
  Razorpay's signature is verified (or, in demo mode, once the simulated
  payment completes). A failed/cancelled payment gets `status="failed"` and
  never gets a receipt. Manual (cash/cheque/bank transfer) entries logged by
  staff are recorded as already-received, so they get a receipt immediately
  on entry.
- **Never issued twice for the same donation** — `_finalize_success()` is
  idempotent: if a donation is somehow finalized twice (double-click,
  browser retry), it returns the existing receipt instead of burning a
  second serial number.

## Setup

```bash
cd temple-donation-system
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env             # then edit .env -- see below
python seed.py                   # creates sample campaigns + admin login
python app.py                    # runs on http://localhost:5000 (or 5001, see note below)
```

Default admin login after seeding: **admin / ChangeMe123!** — you'll be
**required to set a new password** the first time you log in.

macOS note: port 5000 is often taken by AirPlay Receiver. Run
`PORT=5001 python app.py` to use a different port, or turn off AirPlay
Receiver in System Settings.

## Environment variables (`.env`)

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Flask session/CSRF signing key. `.env` ships with a random one already generated. |
| `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` | Leave blank for demo mode; fill in to go live (see below). |
| `RAZORPAY_WEBHOOK_SECRET` | Optional but recommended once live: verifies the `/webhooks/razorpay` server-to-server payment confirmation (see below). Separate secret from `RAZORPAY_KEY_SECRET`. |
| `ORG_NAME`, `ORG_ADDRESS`, `ORG_PAN`, `ORG_80G_REG_NO` | Shown on receipts and the site header. `ORG_ADDRESS` is the local branch address, shown in the address box on the receipt. |
| `ORG_PARENT_NAME` | The main receipt heading (e.g. "International Society for Krishna Consciousness (ISKCON)"). |
| `ORG_FOUNDER_LINE` | Founder-Acharya line under the main heading. |
| `ORG_BRANCH_TYPE` | Heading of the address box (e.g. "Extension Centre"). |
| `ORG_BRANCH_SHORT_NAME` | Short branch identifier shown next to the logo (e.g. "Dwarka Colony, New Delhi"). |
| `ORG_PHONE`, `ORG_EMAIL` | Branch-level contact, shown inside the address box. |
| `ORG_LOGO_PATH` | Path to the temple's logo image (defaults to `static/branding/iskcon_logo.png`), embedded top-right of the receipt. Falls back to a plain vector placeholder if the file isn't found. |
| `ORG_HO_ADDRESS`, `ORG_HO_PHONE`, `ORG_HO_EMAIL` | Registered/head office contact, shown only in the footer's "Registered Office: ..." line -- separate from the branch contact above, matching the real receipt. |
| `ORG_REG_INFO` | One-line trust registration note (e.g. "Registered under Bombay Public Trust Act, 1950 vide Regn. No.: F-2179 (Bom)") shown in the footer alongside the 80G number. |
| `ORG_CLOSING_MESSAGE` | Closing line at the bottom of every receipt, below "Thank you for your generous support." Defaults to the Hare Krishna maha-mantra; set to `""` to omit, or override for a different tradition. Use ` / ` to split into two centred lines. |
| `DATABASE_URL` | Defaults to local SQLite. Set to a Postgres URL if you outgrow SQLite. |
| `FLASK_ENV` | Set to `production` when deployed behind HTTPS — marks session/CSRF cookies `Secure`. Leave unset for local `http://localhost` dev, or cookies will be silently dropped and login will appear broken. |
| `LOGIN_MAX_ATTEMPTS` / `LOGIN_LOCKOUT_MINUTES` | Admin login lockout thresholds (default: 5 attempts, 15 minute lockout). |
| `CONSENT_VERSION` | Tag stored on `Donation.consent_version` for every online donation, identifying which wording of the consent checkbox they agreed to. Bump this whenever that text materially changes. |
| `SENTRY_DSN` | Leave blank to run without error monitoring (default). Set to a Sentry DSN to start reporting unhandled exceptions -- see "Known gaps" below. |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USERNAME` / `SMTP_PASSWORD` / `SMTP_USE_TLS` | Leave `SMTP_HOST` blank to run without emailing receipts (default). Fill these in to email the receipt PDF to the donor automatically -- see "Emailing receipts" below. |
| `MAIL_FROM_ADDRESS` / `MAIL_FROM_NAME` | "From" address/name on receipt emails. `MAIL_FROM_ADDRESS` falls back to `SMTP_USERNAME` if left blank. |
| `WHATSAPP_AIRTEL_USERNAME` / `WHATSAPP_AIRTEL_PASSWORD` | Leave blank to run without sending receipts over WhatsApp (default). Fill in with the HTTP Basic auth credentials Airtel issued for the account, to send the receipt PDF over WhatsApp automatically -- see "Sending receipts via WhatsApp" below. Secrets -- set via `.env`/host Environment tab only, never commit real values. |
| `WHATSAPP_FROM_NUMBER` / `WHATSAPP_TEMPLATE_ID` | The temple's registered WhatsApp Business number and the approved template's ID from Airtel's WhatsApp Manager. Not secret -- already defaulted in `config.py` to the temple's real values. |
| `WHATSAPP_AIRTEL_BASE_URL` | Airtel's send endpoint. Has a working default in `whatsapp_utils.py` -- only set this if Airtel ever changes it. |
| `WHATSAPP_AIRTEL_COOKIE` | Optional. See the ⚠️ note in `whatsapp_utils.py` before setting this -- looked like a session-scoped value in the example this was built from, not a stable credential; confirm with Airtel/your team whether it's actually required before relying on it. |
| `PUBLIC_BASE_URL` | The site's own public URL (`https://givetokrishna.com`). Used to build the receipt link Airtel's servers fetch the PDF from -- see "Sending receipts via WhatsApp" below. |
| `WHATSAPP_REPORT_TEMPLATE_ID` | The 4 AM daily collection report's approved WhatsApp template ID -- see "Daily collection report" below. Not secret -- already defaulted in `config.py`. A separate template from `WHATSAPP_TEMPLATE_ID` above (different variables). |

## Going live with Razorpay

You said you already have a Razorpay account for collections — good, that's
the one thing here you can't test without your own credentials. To go live:

1. In your Razorpay Dashboard, grab your **Key ID** and **Key Secret**
   (Settings → API Keys). Start with **test mode** keys first.
2. Put them in `.env`:
   ```
   RAZORPAY_KEY_ID=rzp_test_xxxxxxxx
   RAZORPAY_KEY_SECRET=xxxxxxxxxxxxxxxx
   ```
3. Restart the app. The donation page will automatically switch from the
   demo "Simulate Payment" button to the real Razorpay checkout popup.
4. Test with Razorpay's [test card/UPI numbers](https://razorpay.com/docs/payments/payments/test-card-upi-details/),
   confirm a receipt PDF is generated end to end, then swap in your **live**
   keys when ready.
5. **Do not commit your `.env` file** — it's already in `.gitignore`.

### Webhook (recommended once live)

The donation page confirms payment by calling `/api/verify-payment` from the
browser right after Razorpay checkout closes. That works for the vast
majority of donations, but it depends on the donor's tab staying open long
enough to make that call — if they close it, lose signal, or something else
interrupts the JS at exactly the wrong moment, the payment succeeded on
Razorpay's side but the app never finds out, and no receipt gets generated
or emailed.

A webhook fixes that: Razorpay calls your server directly, independent of
the browser, whenever a payment actually captures. Both API keys above and
the webhook secret below are needed — the keys create the order and launch
checkout, the webhook is a separate, reliable confirmation channel on top of
that (not a replacement for the keys).

1. In Razorpay Dashboard → **Settings → Webhooks → Add New Webhook**:
   - **Webhook URL**: `https://yourdomain.com/webhooks/razorpay`
   - **Active events**: `payment.captured` (and optionally `order.paid`) is
     the minimum needed for the core donation flow. Also recommended:
     `payment.failed` (marks a donation failed immediately instead of
     waiting for the Dashboard's time-based "abandoned donation" alert to
     notice) and the `payment.dispute.*` events -- `created`,
     `under_review`, `action_required`, `won`, `lost`, `closed` (surfaces
     a chargeback against an already-captured/receipted donation on the
     admin Dashboard, since Razorpay won't otherwise tell you inside this
     app). Any other event type you subscribe to is safely acknowledged
     and ignored -- no harm in checking more than you need.
   - Razorpay shows you a **Secret** when you save it — copy that.
2. Put it in `.env`:
   ```
   RAZORPAY_WEBHOOK_SECRET=whsec_xxxxxxxxxxxxxxxx
   ```
3. Restart the app. That's it — no code changes needed. The webhook and the
   browser-side `/api/verify-payment` call are both safe to fire for the
   same donation (finalizing is idempotent: whichever arrives first issues
   the receipt number and emails it; the other is a no-op).
4. Your server needs to be reachable at a public HTTPS URL for Razorpay to
   reach it — during local development, a tool like `ngrok` can expose
   `localhost` temporarily so you can test the webhook before deploying.

**What the webhook captures.** Beyond just confirming payment, the webhook
pulls the full `payment.captured` payload and stores it on the donation:
payment method (UPI/card/netbanking/wallet), a human-readable reference
(UPI VPA, masked card, bank, or wallet name), Razorpay's fee, and the email/
phone entered at checkout — plus the entire raw payload as JSON, so nothing
is lost even if you need a field not explicitly pulled out. View it from
Admin → Donor → **Payment details** link next to any online donation. This
only ever gets populated by the webhook, so a donation confirmed only via
the browser callback (webhook not set up) shows just the payment ID.

**Disputes/chargebacks.** If you subscribed to the `payment.dispute.*`
events, a dispute against an already-captured donation shows up as a red
"Disputed / Charged-Back Donations" panel on the admin Dashboard, and in
the same donation's Payment details modal (dispute status, reason, and
when it was raised). Nothing about the donation's own `status` changes --
the receipt/80G record stays exactly as issued; a dispute is Razorpay's
own separate process layered on top, and this app just mirrors whatever
status Razorpay reports (`created`/`under_review`/`action_required`/
`won`/`lost`/`closed`) so staff know to look into it and can cross-
reference Razorpay's own dashboard for next steps.

⚠️ This adds new columns to the `donations` table
(`razorpay_method`/`razorpay_reference`/`razorpay_fee`/`razorpay_email`/
`razorpay_contact`/`razorpay_raw_payload`). If you already have a live
database, run the migration after pulling this change:
```bash
flask db migrate -m "add razorpay payment detail fields"
flask db upgrade
```
(A brand-new install via `db.create_all()` picks these up automatically —
only existing databases need the migration above.)

## Importing your historical Excel/Zoho data

`import_legacy_data.py` brings your old donor/donation history into the
unified database, using the exact same donor-matching logic as the live
form (so it won't create duplicates of donors who've already given through
the new system). Read the docstring at the top of that file for the exact
CSV format it expects — you'll need to consolidate your old per-campaign
spreadsheets into one CSV first. Then:

```bash
python import_legacy_data.py path/to/consolidated_history.csv --dry-run   # validate first
python import_legacy_data.py path/to/consolidated_history.csv            # then actually import
```

Campaigns referenced in the CSV must already exist (Admin → Campaigns) —
the script will tell you which rows it skipped and why.

## Donor login (mobile OTP) — going live with SMS

Donors log in with their phone number and a 6-digit OTP. **No SMS provider
is wired up yet** — `sms_utils.py` runs in demo mode: the OTP is shown
directly on the verify page (clearly marked "DEMO MODE") instead of being
texted, so you can test the whole login → account → statement flow today
without an SMS account.

To go live:

1. Pick a provider — MSG91, Fast2SMS, and Twilio are common choices for
   India; typically a few paise to a few rupees per SMS.
2. Open `sms_utils.py` and fill in the `send_otp()` function with that
   provider's API call (there's an example MSG91 request shape commented
   in there already). Have it return `True` on success.
3. Nothing else needs to change — the routes, OTP hashing/expiry/rate
   limiting, and templates already just call this function and adapt based
   on whether it actually sent something.

Other OTP behavior, configurable via `.env`:

| Variable | Purpose |
|---|---|
| `OTP_EXPIRY_MINUTES` | How long a code is valid (default: 10). |
| `OTP_MAX_VERIFY_ATTEMPTS` | Wrong guesses allowed before the code is invalidated (default: 5). |
| `OTP_MAX_REQUESTS_PER_HOUR` | Per-phone-number request throttle (default: 5) — matters once real SMS has a per-message cost. |

Only phone numbers with an existing donor record can log in (there's
nothing to see otherwise) — donors need to have made at least one donation
first, whether online or logged by staff.

## Emailing receipts

Every successful donation (online via Razorpay/simulate-payment, and manual
cash/cheque/bank-transfer entries logged by staff) automatically emails the
receipt PDF to the donor, if they have an email on file. **No SMTP server is
configured yet** — `email_utils.py` runs in demo mode: the PDF is still
generated and downloadable from the admin panel / donor portal as always,
but nothing is emailed until `SMTP_HOST` is set.

To go live, add these to `.env` (Gmail example — use an **App Password**,
not your normal Gmail password: Google Account → Security → 2-Step
Verification → App passwords):

```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=you@gmail.com
SMTP_PASSWORD=xxxx xxxx xxxx xxxx
MAIL_FROM_ADDRESS=you@gmail.com
MAIL_FROM_NAME=ISKCON Dwarka Extension Centre
```

No new pip package is required — this uses only Python's built-in
`smtplib`/`email` modules, so it works even if you can't `pip install` right
now. A failed or unconfigured send never blocks the donation or receipt PDF
itself (the error is logged, not raised) — email is strictly additive on
top of the always-available PDF download.

## Sending receipts via WhatsApp

Every successful donation can also send the receipt PDF to the donor over
WhatsApp, to `Donor.whatsapp_or_phone` (their dedicated WhatsApp number if
they gave one, otherwise their regular phone). **Live** once
`WHATSAPP_AIRTEL_USERNAME`/`WHATSAPP_AIRTEL_PASSWORD` are set (see below) — until then
`whatsapp_utils.py` runs in demo mode; nothing breaks either way, the PDF is
still generated, emailed, and downloadable as always.

This uses the **Airtel IQ WhatsApp Business API**
(iqwhatsapp.airtel.in) — the account this temple actually has. Unlike
Meta's own Cloud API (which needs the PDF uploaded as "media" first, then
referenced by ID in a separate call), Airtel's endpoint just wants a
publicly-fetchable URL to the file and downloads it themselves — which is
exactly what the existing `/receipt/<id>` route already serves (same route
donors' own "Download receipt" links use). `PUBLIC_BASE_URL` plus that
route path is all it takes; no separate upload step. If you ever switch
providers again, only `whatsapp_utils.py` needs to change (swap its
implementation for the new provider's API call, same as swapping
`sms_utils.send_otp()`'s provider) — nothing else in the app does.

Required env vars:
```
WHATSAPP_AIRTEL_USERNAME=...            # secret -- HTTP Basic auth username from Airtel
WHATSAPP_AIRTEL_PASSWORD=...            # secret -- HTTP Basic auth password from Airtel
WHATSAPP_FROM_NUMBER=918178798462       # already defaulted in config.py
WHATSAPP_TEMPLATE_ID=01kzdy128ke65be98yhg9fjazx   # already defaulted in config.py
PUBLIC_BASE_URL=https://givetokrishna.com          # already defaulted in config.py
```
The username/password are the only ones that actually need setting per
environment (local `.env` / the host's Environment tab) — the other three
already match the temple's real account and domain as defaults in
`config.py`, so they're only worth overriding if any of them change later.
`requests`' own `auth=(username, password)` builds the "Basic ..." header
from these two at send time, so there's no base64 token to generate or
store separately.

The approved message template (already set up, ID above) is a **Utility**
category template with a **Document** header (for the PDF) and exactly 3
body placeholders in this order — donor name, amount, org name:
> Dear {{1}}, thank you for your generous donation of Rs. {{2}} to {{3}}.
> Your receipt is attached as a PDF. This is a computer-generated message
> and does not require a signature.
>
> Hare Krishna!

⚠️ Two things worth confirming with Airtel/whoever manages that account,
since they couldn't be verified against Airtel's own docs from here — see
the fuller note in `whatsapp_utils.py`:
- The exact format Airtel expects for the `X-Date` request header (this
  guesses the standard HTTP-date format) — worth checking first if sends
  start failing with an auth/date-looking error.
- Whether a `Cookie` header is genuinely required for production API calls,
  or was just an artifact of a browser/Postman testing session — this
  integration doesn't send one unless `WHATSAPP_AIRTEL_COOKIE` is
  explicitly set.

**Cost:** Airtel bills per message sent, in the same ballpark as Meta's own
direct utility-category rate (roughly Rs. 0.13–0.15 in India) — check your
account's actual rate card for the exact figure.

Same failure policy as email: a bad token, unreachable receipt URL, or
network hiccup is logged and swallowed, never raised into the donation flow
— the receipt PDF is already saved to the database and downloadable
regardless of whether either send succeeds. Donors with no phone/WhatsApp
number on file are silently skipped, same as donors with no email.

## Receipt storage

Receipt PDFs are generated once, at the moment a donation succeeds, and
stored as bytes on `Donation.receipt_pdf` — not written to local disk. This
matters for two reasons: it survives redeploys/restarts on hosts with no
persistent filesystem (Render's free tier, most serverless platforms), and
it rides along with your regular database backups instead of needing a
separate backup story for a folder of PDFs. The PDF is never regenerated on
demand — what's stored is byte-for-byte what the donor actually got, so it
stays accurate even if you change the org's address, logo, or the receipt
template's code later. Downloads (`/receipt/<id>`) and the email attachment
both read straight from that column.

One exception to "never regenerated": if a donation succeeded and has a
receipt number but its stored PDF is missing (PDF generation is
best-effort at finalization, so a failure there can never cost a donor an
already-issued receipt number), the download route builds it on demand and
saves it. That's a repair path for a receipt that would otherwise be
unreachable, not a routine regeneration.

### Receipt download access

`/receipt/<id>` requires a signed token — `/receipt/42?t=…` — because the
PDF carries the donor's full name, address, PAN, email and phone, and
donation ids are sequential. Without it the route could be walked to
harvest every donor's personal details from an unauthenticated endpoint.

The token is an HMAC of the donation id under `SECRET_KEY`
(`utils.receipt_access_token`), so it needs no schema change and no
migration, and the same donation always produces the same token. Templates
build links with the `receipt_token()` Jinja global; `whatsapp_utils`
appends it so Airtel can fetch the PDF server-side. Logged-in admins and a
donor viewing their own donations in the donor portal are allowed through
without a token.

Rotating `SECRET_KEY` invalidates every previously issued receipt link at
once (it already invalidates all sessions, so this isn't a new
consideration) — donors can still reach their receipts through the donor
portal, and staff through the admin area.

**Size, if you're wondering:** each receipt is about 250 KB. At 500-2,000
donations/year for a single branch, that's roughly 120-490 MB/year —
genuinely small, and it grows with your existing Postgres storage, which
you can resize anytime without downtime (see "Deploying to Render" above).
The bulk of that 250 KB is the hologram sticker image; it was originally
~900 KB/receipt until the source image (622x624px, but only ever drawn at
24x27 points on the page) was downsampled to a still-generous 240x240px —
worth knowing if you ever swap in a different hologram/logo image and
receipt sizes creep back up: keep source images sized close to their actual
print size, not "as high-res as I happened to have."

⚠️ **Migration note for existing installs:** this adds a new
`Donation.receipt_pdf` column. If you already have a live database (e.g.
your first Render deploy is already running), pull this change and run:
```bash
flask db migrate -m "add receipt_pdf column"
flask db upgrade
```
Any receipts already issued before this change keep working — the
`/receipt/<id>` download route falls back to the old on-disk location
(`instance/receipts/`) if `receipt_pdf` is empty on that donation, so
nothing 404s. Only *new* donations after the migration get the DB-stored
PDF; there's no automatic backfill of historical receipts from disk into
the database (write a one-off script if you want that, using
`receipt_pdf_path()` in `pdf_utils.py` to locate each old file).

## Backups

SQLite is a single file (`instance/temple.db`) — back it up. `backup_db.py`
copies it to `instance/backups/` with a timestamp and prunes old copies:

```bash
python backup_db.py                # keeps the last 30 backups
```

Set this up as a daily cron job (or your host's scheduled task feature) —
see the comment at the top of the script for a crontab example. If you
move to Postgres instead, use your host's managed backups or `pg_dump`
rather than this script.

### Full data backup (portable, works on SQLite or Postgres)

`backup_utils.py` builds a ZIP of one CSV per table (donors, donations,
campaigns, and every admin-editable lookup list) via the SQLAlchemy ORM,
so it works identically on SQLite or Postgres. There are three ways to
run it:

- **Admin -> Settings -> Data Backup -> Download Backup Now** — streams a
  fresh ZIP straight to your browser, nothing saved on the server.
- **Admin -> Settings -> Data Backup -> Run Backup Now** — runs the full
  routine on demand: saves a copy to `instance/backups/` on that web
  service, prunes old copies beyond `BACKUP_RETENTION_COUNT`, and emails
  it if SMTP + `BACKUP_EMAIL`/`ORG_CONTACT_EMAIL` are configured.
- **`python backup_data.py`** — the same routine, meant to run
  automatically on a schedule (see the `temple-weekly-backup` Cron Job in
  `render.yaml`, Sundays 2 AM UTC). `--dest` overrides the save directory,
  `--no-email` skips the email step.

To restore from one of these ZIPs — e.g. after data loss, or seeding a
new host — use `restore_backup.py`:

```bash
python restore_backup.py path/to/temple_data_backup_20260101_020000.zip --dry-run   # preview first
python restore_backup.py path/to/temple_data_backup_20260101_020000.zip             # upsert by id
python restore_backup.py path/to/temple_data_backup_20260101_020000.zip --wipe      # fresh/empty DB
```

By default it upserts (matches existing rows by id, inserts anything
missing, deletes nothing) — safer when the current database already has
data. `--wipe` deletes every row of every backed-up table first, for a
"make the database look exactly like this backup" restore into a fresh
database. Always take a fresh backup of the *current* database before
running this against production. Admin login credentials and receipt PDFs
are deliberately excluded from every backup (see `backup_utils.py`'s
module docstring) and are therefore untouched by a restore — recreate
admin accounts via Admin -> Manage Users, and receipt PDFs regenerate
automatically the next time they're downloaded or emailed.

## Daily collection report

`daily_report_utils.py` computes yesterday's / this week's (calendar week
to date) / this month's (calendar month to date) collection totals, plus a
campaign-wise breakdown of each, and delivers it to whoever is listed under
**Admin -> Settings -> Daily Report Recipients** — no code change or
redeploy needed to add/remove a recipient.

- **`python daily_report.py`** — triggers the report for yesterday (IST).
  Meant to run automatically at 4:00 AM IST every day — see the
  `temple-daily-report` Cron Job in `render.yaml` (schedule `30 22 * * *`,
  which is UTC — 10:30 PM UTC is 4:00 AM IST the *next* calendar day).
  `--date YYYY-MM-DD` re-runs it for a specific date; `--force` re-sends
  even if that date's report already went out.
  This script is a thin HTTP client, not the sender — it POSTs to the
  already-running web app's own `/internal/daily-report/send`
  (authenticated with `INTERNAL_TASK_TOKEN`, shared between the web
  service and this Cron Job), and the web app does the actual computing
  and emailing/WhatsApping. It moved out of the Cron Job's own process
  because every automatic run failed both channels while manual re-runs
  and every donor-facing receipt this same web app sends, all day, every
  day, succeeded — pointing at that separate container's own outbound
  networking rather than a bug in the send functions. See
  `config.py`'s `INTERNAL_TASK_TOKEN` for the full story and
  `public.internal_daily_report_send` for the endpoint itself.
- **Email** works automatically once `SMTP_HOST` etc. are configured (see
  above) — no extra setup.
- **WhatsApp** uses its own approved template, separate from the receipt
  one (WhatsApp Business templates are approved for one fixed set of
  variables, and the receipt template's 3 donor-facing variables don't fit
  a 5-number internal report) — already approved and defaulted in
  `config.py` as `WHATSAPP_REPORT_TEMPLATE_ID`. See the "DAILY REPORT
  TEMPLATE" section of `whatsapp_utils.py`'s module docstring for the
  exact variable order it was approved with, if it ever needs replacing.
  If `WHATSAPP_AIRTEL_USERNAME`/`PASSWORD` aren't set, this still no-ops
  (same demo-mode pattern as everywhere else) — email delivery is
  unaffected either way.

## Running the tests

```bash
pip install -r requirements.txt   # includes pytest
pytest
```

Covers: donor de-duplication logic (including the WhatsApp-number field),
consent capture on online donations, receipt numbering (sequential, one
running counter shared across 80G/non-80G and financial years), financial-year date math,
Indian-style number formatting, PAN validation, the demo-mode donation
flow end-to-end, admin login lockout, role enforcement on campaign
management, the full donor OTP login flow (request/verify/expiry/
rate-limiting/wrong-attempts, account access control, profile updates),
receipt emailing (demo mode when unconfigured, message construction/
attachment/SMTP delivery via a mocked `smtplib.SMTP`, and that a broken mail
server never raises into the donation flow), receipt PDF storage (bytes
land on Donation.receipt_pdf for both the online and manual-entry flows,
downloads are served from the database, and the legacy on-disk fallback
still works for receipts issued before this change), and the Razorpay webhook
(signature verification accepts/rejects correctly, unconfigured secret is
refused, unrelated event types and unknown order IDs are acknowledged
without side effects, a duplicate delivery of the same event doesn't burn a
second receipt number, and UPI/card payloads are parsed into the right
method-specific reference).

## Deploying to production

1. Set `FLASK_ENV=production` in your production `.env` (hardens cookies).
2. Don't use `python app.py` (Flask's dev server) in production — use
   gunicorn:
   ```bash
   gunicorn -c gunicorn_config.py app:app
   ```
   or, if your host reads a `Procfile` (Render/Railway/Heroku-style), one's
   already included.
3. Put a reverse proxy (nginx, or your host's built-in one) in front for
   HTTPS/SSL termination.
4. Set up `backup_db.py` on a schedule (see above) — or move to a managed
   Postgres database with its own backups if you expect meaningful
   concurrent traffic or plan to add more temples/projects.

### Deploying to Render

Render is the easiest fit for this app as-is (no code changes needed) — it
runs `Procfile`/`gunicorn_config.py` directly as an always-on server, unlike
serverless platforms (Vercel, etc.), which don't support this app's local
SQLite file or on-disk receipt PDFs without a larger rearchitecture.

**Quickest path — Blueprint (recommended):**

1. Push this repo to GitHub.
2. In the Render dashboard: **New → Blueprint**, point it at the repo.
   Render reads `render.yaml` (already in this repo) and sets up the web
   service, a Postgres database, and a 1 GB persistent disk (for the SQLite
   fallback and/or generated receipt PDFs) automatically.
3. Once created, open the service → **Environment** tab and fill in the
   values `render.yaml` left blank: `ORG_NAME`, `ORG_PAN`, `ORG_80G_REG_NO`
   (and the rest of the `ORG_*` variables from the env var table above),
   `RAZORPAY_KEY_ID`/`SECRET`/`WEBHOOK_SECRET`, `SMTP_*`, `SENTRY_DSN` — same
   values as your local `.env`, just pasted into Render instead of committed
   to git.
4. First deploy will run, but the database is still empty. Open the
   service's **Shell** tab and run:
   ```bash
   flask db init && flask db migrate -m "initial schema" && flask db upgrade
   python seed.py   # creates sample campaigns + the default admin login
   ```
5. Your app is live at `https://<service-name>.onrender.com`. Update
   `RAZORPAY_WEBHOOK_SECRET`'s webhook URL in the Razorpay Dashboard to
   `https://<service-name>.onrender.com/webhooks/razorpay` (see "Webhook"
   above), and log in once as `admin` to set a real password.
6. Add a custom domain under the service's **Settings** tab, if you have one
   (see "Custom domain" below).

#### Custom domain

This app is live at **givetokrishna.com** (Cloudflare-managed DNS). Steps
taken, for reference if you ever need to redo this (new domain, domain
transfer, disaster recovery, etc.):

1. Render dashboard → the web service → **Settings → Custom Domains → Add
   Custom Domain** → add both the apex (`givetokrishna.com`) and `www`
   (`www.givetokrishna.com`).
2. In Cloudflare → the domain → **DNS → Records**, add:

   | Type | Name | Target | Proxy status |
   |---|---|---|---|
   | CNAME | `@` (root) | `<service-name>.onrender.com` | DNS only (grey cloud) |
   | CNAME | `www` | `<service-name>.onrender.com` | DNS only (grey cloud) |

   A CNAME at the root works because Cloudflare flattens it automatically.
   Keep both records **DNS only** (not proxied) while Render issues the TLS
   certificate — the orange-cloud proxy can interfere with Let's Encrypt
   validation. Safe to switch to proxied afterward, once Render shows both
   domains as **Verified** with **Certificate Issued**.
3. In Render, pick one domain as primary and let the other redirect to it —
   `www.givetokrishna.com` redirects to `givetokrishna.com` here.
4. Update the Razorpay webhook URL (Dashboard → Settings → Webhooks) from
   the `onrender.com` URL to `https://givetokrishna.com/webhooks/razorpay`.
5. No code changes are needed for a domain swap — nothing in this app
   hardcodes a hostname (no `SERVER_NAME`, no hardcoded `onrender.com`
   links); Flask, Talisman, and CSRF all validate against whatever host the
   request actually came in on.

**Manual path (no Blueprint):** New → Web Service → connect the repo → set
Build Command to `pip install -r requirements.txt` and Start Command to
`gunicorn -c gunicorn_config.py app:app` → add a New → PostgreSQL database
separately and copy its Internal Database URL into `DATABASE_URL` → add a
Disk under the web service's Settings, mounted at
`/opt/render/project/src/instance` → set the same env vars as step 3 above.

**Plan choice matters here:** the Free tier has no persistent disk and
spins the service down after 15 minutes idle (cold-start delay, and any
receipt PDFs not yet in Postgres/moved off-disk are lost on every restart).
Fine for kicking the tyres; use at least the **Starter** plan for real
donation traffic issuing legal 80G receipts.

## Known gaps (be upfront about these with whoever runs this long-term)

- **Donor OTP isn't actually texted yet** — see "Donor login (mobile OTP)"
  above. The login flow itself is real (hashed codes, expiry, attempt
  limits, rate limiting); only the SMS delivery is a demo-mode stand-in
  until you wire up a provider.
- **No automated lapsed-donor follow-ups** — the lapsed-donor report
  identifies who to contact, but nothing texts or WhatsApps them
  automatically yet — that's still a manual step for staff. (Receipts
  themselves are now automated over both email and WhatsApp — see
  "Resolved" below.)
- **No "invite an admin user" screen** — new staff/admin accounts need to
  be created directly in the database for now.
- **Single-server SQLite by default** — fine for one temple's traffic; see
  the Deploying section above for when to move to Postgres.

**Resolved since the note above was first written:**
- **Consent is now actually recorded**, not just gate-checked. Every
  online donation stores `Donation.consent_given`, `consent_at` (when),
  and `consent_version` (which wording of the consent checkbox they saw —
  bump `CONSENT_VERSION` in `.env`/`config.py` whenever that text changes
  materially). Manual (cash/cheque/bank transfer) entries logged by staff
  still default to `consent_given=False`, since there's no digital
  checkbox in that flow — treat that as a known limitation, not a bug.
- **WhatsApp number**: `Donor.whatsapp_number` is now a field of its own,
  separate from `phone` (the donation form, admin manual-entry form, donor
  self-service edit page, and Admin → Donor → Edit all have it). It only
  shows up as its own line on the printed receipt when it's actually
  different from the donor's phone number — otherwise the receipt looks
  exactly as before. `Donor.whatsapp_or_phone` gives you "whichever number
  to actually use" in application code.
- **Database migrations (Alembic via Flask-Migrate)** are wired up in
  `app.py` (guarded so this still runs even before you've installed it).
  After `pip install -r requirements.txt`, run once:
  ```bash
  flask db init      # creates migrations/ -- one-time, first setup only
  flask db migrate -m "initial schema"
  flask db upgrade
  ```
  From then on, whenever you change a model, run `flask db migrate -m "..."`
  then `flask db upgrade` instead of relying on `db.create_all()` to
  hand-evolve a live database. On Render, `render.yaml`'s web service now
  runs `flask db upgrade` automatically as a `preDeployCommand` before
  each deploy goes live, so a committed migration is applied for you —
  you shouldn't normally need to run it by hand anymore. (If you do run
  it manually and hit `DuplicateTable`, that means `app.py`'s own
  `db.create_all()` already created the table on a prior boot before the
  migration ran; run `flask db stamp <revision>` instead of `upgrade` to
  just sync Alembic's version marker, no data is at risk.)
- **Error monitoring (Sentry)** is wired up in `app.py`, off by default.
  Set `SENTRY_DSN` in `.env` (from Sentry → Settings → your project →
  Client Keys) to start reporting unhandled exceptions; leave it blank to
  run without it, same as today.
- **Receipts are now emailed to donors automatically** on every successful
  donation (online and manual), off by default until `SMTP_HOST` is set —
  see "Emailing receipts" above.
- **Receipts are now sent over WhatsApp automatically** too, on every
  successful donation (online and manual), off by default until
  `WHATSAPP_AIRTEL_USERNAME`/`WHATSAPP_AIRTEL_PASSWORD` are set — see "Sending receipts via WhatsApp"
  above. Uses the Airtel IQ WhatsApp Business API.
- **The public donation API is now rate-limited** (`/api/create-order`,
  `/api/verify-payment`, `/api/simulate-payment` — 30 requests/hour per IP),
  via Flask-Limiter, guarded the same way as Flask-Migrate/Sentry so the app
  still runs before you `pip install`, just without throttling. Behind a
  reverse proxy (Render, etc.) this needs to see the real visitor IP rather
  than the proxy's — `app.py` now wraps the app in `ProxyFix` when
  `FLASK_ENV=production`, trusting one proxy hop. The Razorpay webhook is
  deliberately NOT rate-limited (its own signature check is its
  authentication, and Razorpay's own servers are the caller).
- **Security headers (HSTS, clickjacking protection, MIME-sniffing
  protection, Referrer-Policy, and Content-Security-Policy) are now applied
  in production** via Flask-Talisman, guarded like Sentry/Flask-Migrate so
  local dev is unaffected. The CSP allow-lists exactly the external
  resources this app's templates load (Bootstrap/Chart.js from jsdelivr,
  Google Fonts, Razorpay's checkout script/iframe/API) — nothing was
  guessed, it was checked against the actual template files. ⚠️ **Test your
  donation form + a real Razorpay checkout once after your first deploy
  with this live** — I couldn't verify CSP against a real browser from
  where this was built. If anything looks blocked (check the browser
  console for CSP violation messages), set `CONTENT_SECURITY_POLICY_ENABLED=false`
  in your env vars and redeploy to disable just the CSP header (HSTS/
  clickjacking/etc. stay on), then let me know what broke so the allow-list
  can be fixed properly.
- **Receipt PDFs are now stored in the database** instead of local disk —
  see "Receipt storage" above for the full explanation, size numbers, and
  the migration step existing installs need to run.
- **Malformed API requests now fail gracefully.** A missing/non-numeric
  `campaign_id`, `donation_id`, or `amount` used to raise an unhandled
  `ValueError` and show the donor a generic server error page; these now
  return a clean `400` with a JSON error message instead.
- **Razorpay payments are now confirmed server-to-server via a webhook**
  (`/webhooks/razorpay`), not just by trusting the browser's callback after
  checkout closes — off by default until `RAZORPAY_WEBHOOK_SECRET` is set,
  see "Webhook (recommended once live)" above. This does not replace
  `RAZORPAY_KEY_ID`/`RAZORPAY_KEY_SECRET`, which are still required to
  create orders and launch checkout — the webhook is an additional,
  independent confirmation channel with its own secret.

## Project structure

```
temple-donation-system/
├── app.py                    # app factory, blueprint registration, CSRF/error handling
├── config.py                  # settings (reads from .env)
├── extensions.py                # db, login_manager, csrf singletons
├── models.py                     # Donor, Campaign, Donation, ReceiptCounter, AdminUser
├── public.py                      # donation form + Razorpay order/verify + receipt download
├── admin.py                        # admin login, dashboard, donors, campaigns, exports
├── donor_portal.py                  # donor OTP login, account page, statements
├── sms_utils.py                       # OTP generation + SMS delivery (demo mode)
├── email_utils.py                       # receipt emailing (SMTP, demo mode)
├── whatsapp_utils.py                      # receipt delivery over WhatsApp (Meta Cloud API, demo mode)
├── pdf_utils.py                       # receipt PDF generation
├── utils.py                            # financial-year calc, amount-in-words, INR formatting, PAN validation
├── seed.py                              # sample campaigns + default admin user
├── import_legacy_data.py                 # historical CSV importer
├── backup_db.py                           # SQLite backup script
├── gunicorn_config.py / Procfile           # production deployment
├── tests/                                   # pytest suite
├── templates/                                # Jinja2 templates (Bootstrap 5 + Chart.js via CDN)
└── static/style.css                            # temple theme (maroon/saffron/gold)
```
# temple-donation-system
