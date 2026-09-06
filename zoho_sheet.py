"""Reads the Google Sheet that Zoho Forms writes each submission into,
and uses it to put a name on a payment Razorpay has already confirmed.

WHY THIS SHAPE
--------------
Three sources, none of them complete on its own:

    Razorpay      payment id, amount, phone, timestamp   -- no name
    Google Sheet  name, phone, amount, timestamp         -- no payment id
    Zoho webhook  everything, when it bothers to arrive  -- often doesn't

The instinct is to treat the Sheet as the record of donations and hunt
for its payments. That was tried and it is the wrong way round: it makes
the Sheet decide *whether* a receipt is owed, which it cannot do -- it
has no payment id, and its own "Payment Status" column has been wrong in
both directions.

Turning it around removes the difficulty. Razorpay decides what happened:
it knows, definitively, which payments were captured and which of those
this app has no donation for. That list is already produced, hourly, by
public.unreconciled_razorpay_payments(). The only thing missing from it
is whose name goes on the receipt, and that is all the Sheet is asked
for.

WHY THE AMBIGUITY THAT BROKE THE OLD DESIGN DOESN'T APPLY
---------------------------------------------------------
Matching a payment to a submission by phone and amount was unsafe because
two submissions from one donor for the same amount are indistinguishable
from one submission paid for twice -- and those need opposite handling
(two receipts vs. one receipt and a refund). Getting it wrong meant a
wrong receipt.

Here the question is only "what is this payer called". Razorpay has
already established that this specific payment happened, for this amount,
from this phone. When several Sheet rows match, they are by construction
the same phone -- and in every real case seen on this account, the same
person: Harshit Prajapati appears three times in one evening, Divyansh
Narang twice. So they agree on the name, and picking between them is not
a decision at all. When they genuinely disagree, that is reported rather
than guessed -- see resolve_name().

PRIVACY
-------
This sheet holds donor names and phone numbers. If it is exposed by
"publish to web", that link is public to anyone who has it. A service
account with read access to a private sheet is the safer arrangement, and
the reason both are supported here rather than only the easy one.
"""
import csv
import io
import re

import requests


def _norm_phone(raw):
    """Last ten digits, which is how this codebase compares numbers
    everywhere (see utils.normalize_phone). Zoho writes "919220830216"
    and Razorpay returns "+919220830216" for the same person."""
    digits = re.sub(r"\D", "", str(raw or ""))
    return digits[-10:] if len(digits) >= 10 else ""


def _norm_key(key):
    return re.sub(r"[^a-z0-9]", "", str(key or "").lower())


def _value(row, *aliases):
    normalised = {_norm_key(k): v for k, v in row.items()}
    for alias in aliases:
        if alias in normalised:
            return str(normalised[alias] or "").strip()
    for alias in aliases:
        for key, value in normalised.items():
            if alias in key:
                return str(value or "").strip()
    return ""


def fetch_rows(config):
    """Every row of the sheet, as dicts. Returns (rows, error).

    Two ways in, because they trade convenience against exposure:

      ZOHO_SHEET_CSV_URL   a "publish to web" CSV link -- no credentials,
                           but the link is readable by anyone who has it,
                           and it holds donor names and phone numbers.
      (service account)    not implemented here; if this is adopted
                           long-term it belongs behind a private sheet and
                           a read-only service account.

    An error is returned rather than an empty list, deliberately. "No
    rows" and "couldn't read the sheet" must not look alike -- treating a
    failed fetch as "no submissions" is exactly the silent-nothing failure
    that let donations go missing for weeks."""
    url = (config.get("ZOHO_SHEET_CSV_URL") or "").strip()
    if not url:
        return [], None  # not configured is not a failure

    try:
        resp = requests.get(url, timeout=60)
    except requests.RequestException as exc:
        return [], f"Couldn't fetch the submissions sheet: {exc}"

    if resp.status_code >= 400:
        return [], f"The submissions sheet returned {resp.status_code}"

    text = resp.text
    if text.lstrip().startswith("<"):
        # A login or error page rather than CSV -- the usual sign that the
        # sheet isn't actually published, or the link points at the editor
        # rather than the CSV export.
        return [], (
            "The submissions sheet returned a web page, not CSV. Check the link is the "
            "'publish to web' CSV export and not the normal sheet URL."
        )

    try:
        rows = list(csv.DictReader(io.StringIO(text)))
    except Exception as exc:
        return [], f"Couldn't parse the submissions sheet: {exc}"

    return rows, None


def index_by_phone(rows):
    """Sheet rows grouped by normalised phone, which is the only field
    Razorpay and the sheet reliably share."""
    index = {}
    for row in rows:
        phone = _norm_phone(_value(row, "phone", "mobile", "contact"))
        if phone:
            index.setdefault(phone, []).append(row)
    return index


def resolve_name(index, contact, amount=None):
    """The donor's name for a payment, or (None, reason).

    Matched on phone, then narrowed by amount when the sheet has one --
    a donor who registered for a Rs. 100 seminar and separately gave
    Rs. 1,100 should not have the two confused.

    Several matching rows are fine as long as they agree on the name,
    which is the normal case: this account's own data has the same person
    submitting three times in one evening. Rows that disagree are refused
    with a reason rather than resolved by picking the first, because a
    receipt carrying the wrong donor's name is worse than one that waits
    for a person to look."""
    phone = _norm_phone(contact)
    if not phone:
        return None, "the payment has no usable phone number"

    candidates = index.get(phone, [])
    if not candidates:
        return None, "no submission in the sheet has this phone number"

    if amount is not None:
        narrowed = []
        for row in candidates:
            raw = _value(row, "paymentamount", "totalamount", "amount")
            try:
                if abs(float(raw) - float(amount)) < 0.01:
                    narrowed.append(row)
            except (TypeError, ValueError):
                continue
        if narrowed:
            candidates = narrowed

    names = {n for n in (donor_name(row) for row in candidates) if n}
    if not names:
        return None, "the matching submission has no name on it"
    if len(names) > 1:
        return None, (
            "this phone number appears under different names in the sheet ("
            + ", ".join(sorted(names)) + ") -- resolve by hand"
        )

    return names.pop(), None


def donor_name(row):
    """Zoho's composite Name control renders "First, Last" -- confirmed
    against this account: "shivam, raj" is Shivam Raj, "Jatin, saini" is
    Jatin Saini. Not "Last, First", which is the natural assumption and
    would put "Saini Jatin" on a receipt."""
    raw = _value(row, "name", "donorname", "fullname")
    if not raw:
        return ""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    raw = " ".join(parts) if parts else raw
    return " ".join(w.capitalize() if w.islower() else w for w in raw.split())


def extras(row):
    """Anything else worth putting on the donation, when the sheet has
    it. Everything here is optional -- a missing email or PAN is normal
    and must never block a receipt."""
    out = {}
    email = _value(row, "email", "emailid")
    if email and "@" in email:
        out["email"] = email
    pan = _value(row, "pan")
    if pan:
        out["pan"] = pan
    return out


def row_for(index, contact, amount=None):
    """The single best-matching row, for pulling extras from. Returns None
    when the match isn't unambiguous, on the same terms as resolve_name."""
    phone = _norm_phone(contact)
    candidates = index.get(phone, [])
    if not candidates:
        return None
    if amount is not None:
        narrowed = [
            row for row in candidates
            if _matches_amount(row, amount)
        ]
        if narrowed:
            candidates = narrowed
    names = {n for n in (donor_name(row) for row in candidates) if n}
    return candidates[0] if len(names) == 1 else None


def _matches_amount(row, amount):
    raw = _value(row, "paymentamount", "totalamount", "amount")
    try:
        return abs(float(raw) - float(amount)) < 0.01
    except (TypeError, ValueError):
        return False
