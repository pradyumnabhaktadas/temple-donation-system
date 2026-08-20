# QA Report Triage — 2026-08-20

Triage of the third-party QA report (`report.html`, 61 findings, R1 2026-08-08 + R2 2026-08-20 sweep) against the current codebase. Every Critical and High, and most Mediums, were checked one of two ways: (1) reading the actual code path, or (2) actually running it — a live pytest probe against `/receipt/<id>` and `/donate/success/<id>` with a real donation, not just reading the source. Where I ran code, that's noted explicitly. Nothing below was fixed yet — this is triage only.

## The two that need a decision right now

**REG-039 / REG-055 — Donor portal OTP disclosed in the HTTP response (auth bypass)**
Confirmed in code: `sms_utils.send_otp()` always returns `False` (no SMS provider is wired up), and `donor_portal.py`'s `send_otp_route()` reacts by flashing `f"DEMO MODE (no SMS provider configured): your OTP is {otp}"` straight into the page. The report used this to log into a real donor's account on **production** (`givetokrishna.com`), not just a sandbox. Anyone who knows a donor's phone number can read their donation history, address, and PAN this way right now. Fix: configure a real SMS provider, or at minimum stop rendering the OTP in the response and show a generic "check your phone" message instead.

**REG-056 (supersedes REG-026) — `/donate/success/<id>` leaks the receipt token unauthenticated**
I ran this, didn't just read it. `/receipt/<id>` with no token: **404** (correctly gated — that part of REG-026 is genuinely fixed and, per the report's own live re-test, already deployed). But `/donate/success/<id>` has no auth check at all, shows the donation amount, and embeds a working `/receipt/<id>?t=<token>` link in the page HTML — I confirmed that embedded token works. Since donation ids are sequential, anyone can page through ids and harvest a real download link for each one; the linked PDF has name, PAN, address, phone, email. This is the report's current top-severity finding, matching exactly what R2 found live on production.

My proposed fix for REG-056 (not yet built, wanted your go-ahead before touching payment code):
- Gate `/donate/success/<id>` the same way `/receipt/<id>` already is (token / admin / donor session). Without one of those, show a generic "thank you, check your email/SMS receipt" page — no amount, no download link.
- The two places that can legitimately hand a token to the *paying donor's own browser* without asking them to log in again: `payment_callback()` (already has a verified Razorpay signature before it redirects) and `/api/verify-payment`'s success response (same). Both get `t=` appended server-side.
- The pure-webhook-confirmed case (rare — browser fast path never fired) degrades to the generic page. The donor already gets the PDF by email/WhatsApp regardless, so nothing is actually lost, just less convenient in that one edge case.

## Everything else, Critical and High

| ID | Finding | Verdict |
|---|---|---|
| REG-001 | 80G opt-out still submits PAN to the server | **Open, reproducible.** Toggling to "No" only hides the fields via CSS; the inputs stay in the `<form>` and `FormData` still serializes them. |
| REG-020 | No form field sitewide has a linked `<label for=...>` | **Open, reproducible.** Grepped every donor-facing template; labels have no `for`, most inputs have no `id`. |
| REG-026 | Raw `/receipt/<id>` with zero token | **Fixed and confirmed deployed.** Ran it: 404 with no token, 404 with a wrong token. Superseded by REG-056 above. |
| REG-051 | Live donation table dropped from ~10,730 rows to ~103 | **Can't verify from this repo — production database state only you can check.** Flagging with urgency: if this is real, it's either a mass delete or someone pointed production at the wrong database. Worth checking Render's Postgres backups/point-in-time-recovery and any recent deploy/migration logs from around when this happened. |
| REG-002 | Almost no visible focus indicators | **Plausible, partially confirmed.** `static/style.css` only styles `:focus` for `.form-control`/`.form-select` and one admin dropdown — buttons and links have no visible focus state. |
| REG-003 | BACE form doesn't require email, but a receipt gets emailed | Not independently verified this session — plausible from the code structure (email is optional on donor records generally); would need a direct form check. |
| REG-004 | PAN case only normalized visually, not on the submitted value | **Not reproducible against current code.** Every server-side path that stores a PAN (`find_or_create_donor`, admin donor edit, all three CSV/xlsx importers) calls `.upper()` before it hits the database. If the live site still shows this, it's likely running an older deploy. |
| REG-005 | No Privacy Policy / Terms / Refund policy anywhere | **Open, reproducible.** No template or route matching any of those exists. |
| REG-021 | Mobile pages take 10-11s to become usable (image weight) | Not independently verified — no browser available in this session to measure. Plausible; the homepage gallery is known to carry several MB of unoptimized images (see REG-006). |
| REG-022 | Donation submission fails silently if Razorpay's script is blocked | **Already fixed, per the report's own "fixed-verified" status** — matches the `waitForRazorpay()` rewrite in `donation-payment.js` I did earlier this session. |
| REG-028 | Historical/imported donations show a high exact-duplicate rate | Marked **unverifiable** by the report itself; not something I can settle from code. |
| REG-031 / REG-057 | The "sandbox" shares the live production database and uses a live (not test) Razorpay key | **Architecturally plausible, can't confirm or deny from this repo.** `render.yaml` only defines one web service and one database — if a sandbox exists, it was set up separately on Render's dashboard, reusing the same env vars. This is worth checking directly in your Render account: is there a second service, and does it point at different `DATABASE_URL`/`RAZORPAY_KEY_*` values? |
| REG-059 | CSV/formula injection in admin exports | **Open, reproducible.** Every export writer in `admin.py` (BACE, donors, donations log, 10BD, camps — checked several, the pattern repeats) writes donor-controlled fields (name, address, remarks) straight into the CSV with no leading-`=`/`+`/`-`/`@` neutralization. A donor whose name is `=CMD(...)` would have that land unescaped in a CSV an admin later opens in Excel. |

## Medium (24) and Low (20)

I spot-checked the ones with the clearest security/compliance angle rather than re-verifying all 44:

- **REG-036** (80G accepted with no PAN) — **open, reproducible**: `create_order()`'s only PAN requirement is the Rs. 49,000 high-value rule; nothing blocks a below-threshold donation from being marked 80G with no PAN on file.
- **REG-040** (phone-number enumeration via differing login responses) — **open, reproducible**: `send_otp_route()` flashes "No donor account found with that phone number" only when the phone doesn't exist, a different message than the success path.
- **REG-041** (no rate limiting on OTP-send) — **open for IP-level throttling**: `send_otp_route()` has no `@limiter` decorator at all. Worth noting there IS a separate per-phone hourly cap already in the function body, so this isn't wide open, just missing the IP-level layer.
- **REG-019** (admin login linked in every page's footer) — **open, reproducible**, one line in `base.html`.
- **REG-029** (donor list shows PAN in full, unmasked) — **open, reproducible**, `admin/donors.html` prints `d.pan` raw.
- **REG-013** (no CSP header) — code says this should be **on**: `CONTENT_SECURITY_POLICY_ENABLED` defaults to `"true"` in `config.py`, and Flask-Talisman is a real, non-optional production dependency (not the "isn't installed" dev warning, which only fires in this sandbox). If the live site genuinely has no CSP header, check Render's env vars for an explicit override, or whether Talisman is actually installed in the deployed build.
- **REG-024** (no `<main>` landmark) — **open, reproducible**, no `<main>` tag anywhere in `base.html` or `base_admin.html`.
- The remaining perf/visual/a11y items (image optimization, color contrast, viewport overflow, dark mode, heading order, etc.) I did not re-verify — they need a rendered browser to confirm, which this session doesn't have loaded. Nothing about the report's methodology on the items I *did* check gave me reason to doubt the rest; I'd treat them as reliable unless you want me to spin up Chrome and re-check specific ones.
- A few are already resolved per the report's own verdict and consistent with recent commits: **REG-007** (cache headers), **REG-011** (`receipt_type` default), **REG-017** (favicon/robots/sitemap), **REG-050** (the admin roles system the report says now exists — it does, `admin_role_required` + Admin Users management, both already shipped).

## Recommended order, if you want me to start fixing

1. **REG-039/055** (OTP disclosure) and **REG-056** (success-page token leak) — both confirmed live, both are direct PAN/PII exposure. These are the ones I'd do first, today.
2. **REG-001** (80G opt-out PAN leak), **REG-059** (CSV formula injection), **REG-036** (80G with no PAN), **REG-040/041** (portal enumeration/rate limiting) — smaller, contained fixes, still real security/compliance gaps.
3. **REG-020/024/002** (labels, `<main>`, focus states) — mechanical, low-risk, one shared template pattern to fix once.
4. **REG-005** (missing Privacy/Terms/Refund pages) — content work, not code; happy to draft these once you tell me what your actual refund/cancellation policy is, since I can't invent one.
5. Performance/cosmetic items — lowest priority, worth a dedicated pass with a real browser rather than guessing from code.

Two things only you can act on: **REG-051** (the row-count drop — check Render/Postgres directly) and confirming whether a second Render service exists for **REG-031/057**.
