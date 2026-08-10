import datetime
import re


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


def get_financial_year(date=None):
    """India FY runs Apr 1 - Mar 31. Returns e.g. '2026-27'."""
    if date is None:
        date = datetime.date.today()
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
