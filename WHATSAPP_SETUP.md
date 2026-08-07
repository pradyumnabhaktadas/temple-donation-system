# Setting up WhatsApp receipt delivery — for whoever manages our Meta/Facebook account

**What this is for:** the donation website now supports automatically sending each donor their receipt PDF over WhatsApp (in addition to email). To turn it on, we need four values from Meta's WhatsApp Business platform. This doc has everything needed to get them.

No coding needed on your end — just account setup and one form (the message template) inside Meta's own dashboard.

---

## What you'll need before starting

- A Facebook account with admin access to (or the ability to create) a **Meta Business Account**.
- A phone number to register for WhatsApp Business — this can be a free test number Meta gives you to start, or the temple's real WhatsApp number later.

---

## Step 1 — Meta Business Account

1. Go to **business.facebook.com** and create a Business Account if we don't already have one (temple name, your email, etc.).

## Step 2 — Create a Meta App with WhatsApp

1. Go to **developers.facebook.com** → **My Apps** → **Create App**.
2. Choose the **Business** app type, link it to the Business Account from Step 1.
3. Once the app is created, add the **WhatsApp** product to it (from the app dashboard, "Add Product" → WhatsApp → Set Up).

## Step 3 — Get the Phone Number ID

1. Inside the app, go to **WhatsApp → API Setup**.
2. Meta gives you a **free test phone number** here automatically — good enough to get started (it can only message a short list of verified test recipients until the number is upgraded, but that's fine for our initial test).
3. On that page, copy the **Phone Number ID** (a long numeric ID, *not* the phone number itself). This is `WHATSAPP_PHONE_NUMBER_ID`.
4. Later, when ready to go fully live, add our real WhatsApp Business number instead (Meta walks you through verifying it via SMS/call) — the Phone Number ID will change to the new number's ID at that point.

## Step 4 — Get an Access Token

Two options:

- **Quick test token (expires in 24 hours):** shown right on the API Setup page from Step 3. Fine for the very first test send.
- **Permanent token (use this for the real site):** WhatsApp → **Configuration** → **System Users** → create a new System User → assign it the WhatsApp app with **WhatsApp Business Messaging** permission → generate a token with no expiry.

This value is `WHATSAPP_ACCESS_TOKEN`. Treat it like a password — don't post it anywhere public.

## Step 5 — Create and submit the message template

This is the one-time "form" that has to be filled in and approved by Meta before any receipt can be sent (WhatsApp requires pre-approved templates for any message a business sends first, since the donor isn't messaging us first).

Go to **WhatsApp Manager → Account Tools → Message Templates → Create Template**, and fill in exactly this:

| Field | Value |
|---|---|
| **Template name** | `donation_receipt` |
| **Category** | **Utility** (not Marketing — this is a transactional receipt, and Utility is both cheaper and doesn't need marketing opt-in) |
| **Language** | English (or whichever language code you pick — note it down, e.g. `en_US`) |
| **Header type** | **Document** (this is what lets us attach the receipt PDF) |

**Body text** — copy this exactly, including the `{{1}}`, `{{2}}`, `{{3}}` placeholders:

```
Dear {{1}}, thank you for your generous donation of Rs. {{2}} to {{3}}. Your receipt is attached as a PDF. This is a computer-generated message and does not require a signature.

Hare Krishna!
```

Meta will ask you to give **sample values** for each placeholder when submitting — use something like:
- `{{1}}` → `Rajesh Sharma`
- `{{2}}` → `1,001.00`
- `{{3}}` → `Sri Sri Rukmini Dwarkadhish Temple`

Submit for review. Utility-category templates are usually approved within a few minutes to a day — you'll get a notification either way.

---

## What to send back once done

Four values, all from the steps above:

| Value | From step | Example |
|---|---|---|
| `WHATSAPP_ACCESS_TOKEN` | Step 4 | `EAAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx` |
| `WHATSAPP_PHONE_NUMBER_ID` | Step 3 | `109876543210987` |
| `WHATSAPP_RECEIPT_TEMPLATE_NAME` | Step 5 | `donation_receipt` |
| `WHATSAPP_RECEIPT_TEMPLATE_LANG` | Step 5 | `en_US` |

Please send these back somewhere private (not a public channel) — the access token in particular can send messages on our behalf if it leaks.

---

## Cost

Meta charges per message sent (not a monthly subscription, since we're going direct rather than through a paid middleman platform). Utility-category messages currently run roughly **Rs. 0.13–0.15 per message** in India — cheaper than most SMS, and we're only charged when a receipt is actually sent.

## Notes

- The test phone number from Step 3 can only message phone numbers you've explicitly added as verified test recipients in the dashboard — fine for us to test with our own numbers, but donors' real numbers won't receive anything until we register our real WhatsApp Business number (Step 3, "later" note).
- Once the four values above are added to the site, WhatsApp receipt delivery turns on automatically for every future donation — no other changes needed.
