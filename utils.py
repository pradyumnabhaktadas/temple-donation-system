import datetime
import hashlib
import hmac
import re

# CSV/formula injection (OWASP CSV Injection, QA report REG-059): a cell
# whose text begins with one of these is read as a formula, not literal
# text, the moment the file is opened in Excel or Google Sheets -- and
# every admin CSV export in this app writes donor-controlled fields (name,
# address, remarks, ...) straight into cells an admin later opens for a
# government filing (Form 10BD) or a reconciliation. A donor whose name was
# literally "=CMD(...)" would have that executed, not printed, by whoever
# opened the export. Tab and CR are included because some spreadsheet
# versions also parse a leading tab as a formula trigger.
_CSV_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value):
    """Neutralizes one cell for csv.writer: if it's a string starting with
    a formula trigger, prefix it with a single quote so it opens as plain
    text -- Excel/Sheets both strip a leading `'` from what's displayed, so
    nothing about how the value reads to a human changes. Non-strings
    (amounts, None, dates already formatted elsewhere) pass through
    untouched."""
    if isinstance(value, str) and value.startswith(_CSV_FORMULA_TRIGGERS):
        return "'" + value
    return value


def csv_safe_row(values):
    """Apply csv_safe() to every cell in a row about to go to
    csv.writer.writerow() -- see csv_safe()'s docstring for why."""
    return [csv_safe(v) for v in values]


def receipt_access_token(donation_id, secret_key):
    """An unguessable per-donation token gating the /receipt/<id> download.

    Receipt PDFs carry the donor's full name, address, PAN, email and
    phone. Donation ids are sequential integers, so without this the route
    could be walked -- /receipt/1, /receipt/2, ... -- to harvest every
    donor's personal details, PAN included, from an unauthenticated
    endpoint. It can't simply be put behind a login: WhatsApp receipt
    delivery works by handing Airtel a public URL to fetch the PDF from
    (see whatsapp_utils), and donors need the link right after paying,
    before any account exists.

    A signed token solves both: it travels in the URL, so anything holding
    a legitimate link (the success page, a WhatsApp message) keeps
    working, while an id alone gets you nothing.

    Derived from SECRET_KEY rather than stored, so this needed no schema
    change and no migration -- the same donation always produces the same
    token, and rotating SECRET_KEY invalidates every old link at once
    (which also already invalidates every session, so it's not a new
    consideration). Truncated to 32 hex chars: 128 bits, far past
    brute-forcing, and short enough to stay readable in a WhatsApp message.
    """
    message = f"receipt:{donation_id}".encode()
    return hmac.new(secret_key.encode(), message, hashlib.sha256).hexdigest()[:32]


def format_inr(amount, decimals=0):
    """Format a number with Indian-style digit grouping (lakh/crore), e.g.
    1234567 -> '12,34,567' instead of the Western '1,234,567'.

    Grouping rule: the last 3 digits are grouped together, then every
    2 digits after that.
    """
    amount = float(amount)
    sign = "-" if amount < 0 else ""
    amount = abs(amount)

    if decimals:
        whole_str = f"{amount:.{decimals}f}"
        whole_str, _, frac = whole_str.partition(".")
    else:
        whole_str = str(int(round(amount)))
        frac = ""

    if len(whole_str) <= 3:
        grouped = whole_str
    else:
        last_three = whole_str[-3:]
        rest = whole_str[:-3]
        # Group the remainder in pairs of 2, from the right.
        pairs = []
        while len(rest) > 2:
            pairs.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            pairs.insert(0, rest)
        grouped = ",".join(pairs) + "," + last_three

    return sign + grouped + (f".{frac}" if frac else "")


PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")


def is_valid_pan(pan):
    """PAN is optional on the donation form, but if someone provides one
    (e.g. to claim 80G), it must match India's PAN structure: 5 letters,
    4 digits, 1 letter. Catches typos before they end up in the Form 10BD
    filing data."""
    if not pan:
        return True
    return bool(PAN_RE.match(pan.strip().upper()))


def normalize_phone(raw):
    """Normalizes any phone number a donor/staff member might type -- with
    or without a '+91'/'91' country code, with spaces/dashes/parens, or
    with a leading trunk '0' -- down to the plain 10-digit local number
    this codebase stores and matches against everywhere (Donor.phone,
    donor OTP login, WhatsApp/SMS sends -- see whatsapp_utils._to_e164 and
    sms_utils.py, which already assume this same plain-10-digit
    convention on the way *out*; this is the matching normalization on
    the way *in*).

    Without this, "+91 88020 81265", "918802081265", "08802081265", and
    "8802081265" would all get stored/compared as four different strings
    -- silently splitting one donor into duplicate records, and breaking
    donor login (exact phone match) if they log in with a different
    format than the one their donation was originally recorded with.

    Returns the input stripped-but-otherwise-unchanged if it doesn't look
    like a recognisable Indian mobile number (e.g. a landline, a foreign
    number, or garbage input) -- this only ever narrows a *recognised*
    format down to the canonical one, it never guesses at something
    genuinely ambiguous.
    """
    raw = (raw or "").strip()
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits if len(digits) == 10 else raw


PHONE_RE = re.compile(r"^[6-9]\d{9}$")


def is_valid_phone(raw):
    """True if `raw` normalizes (see normalize_phone above) down to a
    plausible 10-digit Indian mobile number. Catches the two mistakes
    normalize_phone can't fix on its own because it can't tell a typo from
    a genuinely unusual number: wrong digit count (a stray extra digit, a
    digit dropped, a landline/foreign number that isn't 10 digits after
    stripping) and a non-mobile leading digit (Indian mobile numbers are
    only ever allotted starting 6/7/8/9 -- TRAI hasn't issued 0/1-5
    prefixes for mobiles).

    Blank input is treated as *valid* here, same as is_valid_pan --
    both fields are optional in most places this is called; the caller
    decides separately whether blank is acceptable for that particular
    form. Call this after normalize_phone (or on the same raw value --
    it normalizes internally too) whenever a phone number is accepted
    from a donor or admin, to catch a mistyped number before it's stored
    and silently breaks OTP login / WhatsApp / SMS delivery later.
    """
    if not (raw or "").strip():
        return True
    return bool(PHONE_RE.match(normalize_phone(raw)))


IST_OFFSET = datetime.timedelta(hours=5, minutes=30)


def to_ist(dt):
    """Converts a naive UTC datetime -- the only kind this codebase stores;
    every timestamp column defaults to datetime.datetime.utcnow(), and the
    host server's own clock (e.g. Render's) is UTC too -- to naive IST for
    display or date-boundary math. India is a fixed UTC+5:30 offset with no
    DST, so this is just a flat addition, no timezone/tzdata dependency
    needed.

    Returns None if given None, so this can be chained directly on an
    optional column (e.g. donation.cancelled_at) without a separate guard:
    `{{ (d.cancelled_at | to_ist).strftime(...) if d.cancelled_at else '-' }}`.

    Without this, every timestamp shown to a temple office in Delhi -- a
    donation's "time" in the Donations Log, "today"'s collections on the
    Dashboard, upcoming birthdays, the financial-year a near-midnight
    donation gets filed under -- is off by 5 hours 30 minutes from what
    actually happened in India, and "today" itself is the wrong calendar
    date for the ~5.5 hours a day (roughly 12:00 AM-5:30 AM IST) where UTC
    hasn't rolled over to the same day yet.
    """
    if dt is None:
        return None
    return dt + IST_OFFSET


def now_ist():
    """Current moment in India, as a naive datetime. Use this instead of
    datetime.datetime.now()/datetime.date.today() (which read the server's
    own clock -- UTC on Render and most hosts) anywhere "today"/"now" needs
    to mean the actual India-local moment: dashboard "today" stats,
    birthday/anniversary windows, lapsed-donor calculations, and financial-
    year determination all depend on this being right."""
    return to_ist(datetime.datetime.utcnow())


def get_financial_year(date=None):
    """India FY runs Apr 1 - Mar 31. Returns e.g. '2026-27'.

    `date` should already be IST if it's derived from a stored UTC
    timestamp (see to_ist()/now_ist()) -- passing a raw UTC datetime can
    misattribute a donation made just after midnight IST to the previous
    financial year, since UTC is still on the prior calendar date then."""
    if date is None:
        date = now_ist().date()
    if date.month >= 4:
        start = date.year
    else:
        start = date.year - 1
    return f"{start}-{str(start + 1)[-2:]}"


_ONES = [
    "", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
    "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
    "Seventeen", "Eighteen", "Nineteen",
]
_TENS = [
    "", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety",
]


def _two_digits(n):
    if n < 20:
        return _ONES[n]
    return (_TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else "")).strip()


def _three_digits(n):
    if n >= 100:
        return (_ONES[n // 100] + " Hundred" + (" " + _two_digits(n % 100) if n % 100 else "")).strip()
    return _two_digits(n)


def amount_to_words_inr(amount):
    """Convert a rupee amount (int or float) to Indian-style words, e.g.
    123456 -> 'One Lakh Twenty Three Thousand Four Hundred Fifty Six Rupees Only'
    """
    amount = int(round(float(amount)))
    if amount == 0:
        return "Zero Rupees Only"
    if amount == 1:
        return "One Rupee Only"

    crore = amount // 10000000
    amount %= 10000000
    lakh = amount // 100000
    amount %= 100000
    thousand = amount // 1000
    amount %= 1000
    hundred = amount

    parts = []
    if crore:
        parts.append(_three_digits(crore) + " Crore")
    if lakh:
        parts.append(_three_digits(lakh) + " Lakh")
    if thousand:
        parts.append(_three_digits(thousand) + " Thousand")
    if hundred:
        parts.append(_three_digits(hundred))

    return " ".join(parts) + " Rupees Only"
