import datetime
import io
import os
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from utils import HIGH_VALUE_PAN_THRESHOLD, amount_to_words_inr, format_inr

RECEIPTS_DIR = os.path.join(os.path.dirname(__file__), "instance", "receipts")
FONTS_DIR = os.path.join(os.path.dirname(__file__), "static", "fonts")

# --- Fonts -------------------------------------------------------------
# The temple's actual receipt uses a serif face throughout, and its "Rs."
# figure is shown with a real Rupee-sign glyph (U+20B9) -- not part of any
# base-14 PDF font's WinAnsi encoding. DejaVu Serif has that glyph, so we
# bundle it (static/fonts/) and register it; if the files are ever missing
# (e.g. stripped from a deploy), fall back to the base-14 Times family so
# the receipt still renders, just without the Rupee glyph.
try:
    pdfmetrics.registerFont(TTFont("Receipt-Serif", os.path.join(FONTS_DIR, "DejaVuSerif.ttf")))
    pdfmetrics.registerFont(TTFont("Receipt-Serif-Bold", os.path.join(FONTS_DIR, "DejaVuSerif-Bold.ttf")))
    pdfmetrics.registerFont(TTFont("Receipt-Serif-Italic", os.path.join(FONTS_DIR, "DejaVuSerif-Italic.ttf")))
    SERIF = "Receipt-Serif"
    SERIF_BOLD = "Receipt-Serif-Bold"
    SERIF_ITALIC = "Receipt-Serif-Italic"
    RUPEE = "₹"
except Exception:
    SERIF = "Times-Roman"
    SERIF_BOLD = "Times-Bold"
    SERIF_ITALIC = "Times-Italic"
    RUPEE = "Rs."

# --- Colours -- sampled directly from the temple's actual receipt PDF --
NAVY = colors.HexColor("#3e4095")          # main title
PINK = colors.HexColor("#ec268f")          # box borders + field labels
YELLOW = colors.HexColor("#fff8af")        # address / registered-office boxes
DONOR_BADGE = colors.HexColor("#f49cc2")   # "DONOR'S COPY" badge fill
LAVENDER = colors.HexColor("#edecf6")      # field/box interiors
INK = colors.HexColor("#2a2a2a")
GREY = colors.HexColor("#6b6b6b")
WATERMARK_GREEN = colors.HexColor("#d9ecd4")

# Legacy names kept for generate_annual_statement_pdf, which still uses the
# app's site theme (now Krishna blue/gold) rather than the receipt's exact
# palette. Values match static/style.css's --maroon/--maroon-dark/--saffron.
MAROON = colors.HexColor("#1d3b6d")
MAROON_DARK = colors.HexColor("#0f2444")
SAFFRON = colors.HexColor("#e2a33d")


def _ensure_dir():
    os.makedirs(RECEIPTS_DIR, exist_ok=True)


def receipt_pdf_path(receipt_number):
    _ensure_dir()
    safe_name = receipt_number.replace("/", "_")
    return os.path.join(RECEIPTS_DIR, f"{safe_name}.pdf")


def _wrap_to_width(c, text, font_name, font_size, max_width):
    """Word-wraps `text` so each line fits within max_width (points), using
    actual font metrics rather than a guessed character count.

    A single "word" that's wider than max_width on its own (a long email
    address or URL with no spaces, which plain word-wrap can't break) is
    fallen back to a character-level split instead of being left to
    silently overflow the box it's drawn in."""
    words = text.split()
    lines = []
    current = ""
    for w in words:
        if c.stringWidth(w, font_name, font_size) > max_width:
            if current:
                lines.append(current)
                current = ""
            chunk = ""
            for ch in w:
                if c.stringWidth(chunk + ch, font_name, font_size) <= max_width:
                    chunk += ch
                else:
                    if chunk:
                        lines.append(chunk)
                    chunk = ch
            current = chunk
            continue
        candidate = (current + " " + w).strip()
        if c.stringWidth(candidate, font_name, font_size) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = w
    if current:
        lines.append(current)
    return lines


def _draw_watermark(c, width, height, text="ISKCON"):
    """Tight, upright, tiled background text -- matches the real receipt's
    security-paper texture, which is packed rows of "ISKCON" repeated with
    almost no gap (not rotated, as an earlier version of this had it)."""
    c.saveState()
    try:
        c.setFillAlpha(0.55)
    except AttributeError:
        pass
    c.setFont(SERIF, 10)
    c.setFillColor(WATERMARK_GREEN)
    tw = c.stringWidth(text + " ", SERIF, 10)
    step_y = 15
    row = 0
    y = -step_y
    while y < height + step_y:
        offset = (row % 2) * (tw / 2)
        x = -tw + offset
        while x < width + tw:
            c.drawString(x, y, text)
            x += tw
        y += step_y
        row += 1
    c.restoreState()


def _box(c, rect, fill=None, stroke=PINK, line_width=1.1, radius=4):
    """A rounded rectangle in the style used throughout the real receipt:
    thin pink/magenta border, optional fill."""
    x0, y0, x1, y1 = rect
    c.saveState()
    if fill is not None:
        c.setFillColor(fill)
    if stroke is not None:
        c.setStrokeColor(stroke)
    c.setLineWidth(line_width)
    c.roundRect(
        x0, y0, x1 - x0, y1 - y0, radius,
        stroke=1 if stroke is not None else 0,
        fill=1 if fill is not None else 0,
    )
    c.restoreState()


def _box_label_above(c, rect, text, font=SERIF, size=10.5, color=PINK, pad_x=10):
    """A label straddling a box's top border, like "Date" or "Mode of
    Payment (...)" on the real receipt -- text centred on the border line,
    with a small white backing rectangle so it visually breaks the line."""
    x0, y0, x1, y1 = rect
    cx = (x0 + x1) / 2
    tw = c.stringWidth(text, font, size)
    c.saveState()
    c.setFillColor(colors.white)
    c.rect(cx - tw / 2 - pad_x / 2, y1 - size * 0.35, tw + pad_x, size * 1.1, stroke=0, fill=1)
    c.setFont(font, size)
    c.setFillColor(color)
    c.drawCentredString(cx, y1 - size * 0.32, text)
    c.restoreState()


def _box_label_below(c, rect, text, font=SERIF, size=7.5, color=NAVY, max_width=None):
    """Label centred just below a box -- shrinks to fit so two adjacent
    narrow boxes (e.g. the two signature lines) never bleed into each
    other."""
    x0, y0, x1, y1 = rect
    box_w = max_width if max_width is not None else (x1 - x0) - 4
    while size > 5.5 and c.stringWidth(text, font, size) > box_w:
        size -= 0.5
    c.setFont(font, size)
    c.setFillColor(color)
    c.drawCentredString((x0 + x1) / 2, y0 - size * 1.1, text)


def _value_in_box(c, rect, value, font=SERIF_BOLD, size=10, color=INK, pad=6, max_lines=2):
    """Centres a (possibly wrapped) value inside a box -- used for the
    Mode of Payment / Payment Details / Purpose of Donation boxes."""
    x0, y0, x1, y1 = rect
    text = str(value) if value else "-"
    lines = _wrap_to_width(c, text, font, size, (x1 - x0) - 2 * pad)[:max_lines] or ["-"]
    line_h = size * 1.25
    total_h = line_h * len(lines)
    top = (y0 + y1) / 2 + total_h / 2 - size * 0.8
    c.setFont(font, size)
    c.setFillColor(color)
    ty = top
    for line in lines:
        c.drawCentredString((x0 + x1) / 2, ty, line)
        ty -= line_h


def _draw_phone_icon(c, cx, cy, r=4.2, color=INK):
    """Small circular phone/WhatsApp-style icon -- no standard PDF font has
    this glyph, so it's a tiny vector approximation of the monochrome icon
    printed before "Mobile" throughout the real receipt (address box,
    donor details, and footer)."""
    c.saveState()
    c.setStrokeColor(color)
    c.setLineWidth(0.55)
    c.circle(cx, cy, r, stroke=1, fill=0)
    p = c.beginPath()
    p.moveTo(cx - r * 0.42, cy - r * 0.38)
    p.curveTo(cx - r * 0.05, cy - r * 0.05, cx - r * 0.05, cy + r * 0.15, cx + r * 0.12, cy + r * 0.4)
    p.curveTo(cx + r * 0.28, cy + r * 0.62, cx + r * 0.48, cy + r * 0.42, cx + r * 0.32, cy + r * 0.2)
    c.setLineWidth(0.8)
    c.drawPath(p, stroke=1, fill=0)
    c.restoreState()


def _bits_with_icon_width(c, bits, font, size, icon_prefix="Mobile"):
    """Total rendered width of `bits` as drawn by
    `_draw_centered_bits_with_icon` -- split out so callers can shrink-to-fit
    before drawing."""
    space_w = c.stringWidth(" ", font, size)
    icon_r = size * 0.42
    icon_gap = 2.2
    total = 0.0
    for b in bits:
        w = c.stringWidth(b, font, size)
        if b.startswith(icon_prefix):
            w += icon_r * 2 + icon_gap
        total += w
    return total + space_w * (len(bits) - 1)


def _draw_centered_bits_with_icon(c, cx, y, bits, font, size, color, icon_prefix="Mobile"):
    """Draws a sequence of text segments as one centred line (mirroring the
    old ' '.join(...) + drawCentredString behaviour), except the segment
    starting with `icon_prefix` gets the small phone icon drawn just before
    it -- used for the footer's "Mobile: ..." bit."""
    space_w = c.stringWidth(" ", font, size)
    icon_r = size * 0.42
    icon_gap = 2.2
    total = _bits_with_icon_width(c, bits, font, size, icon_prefix)
    x = cx - total / 2
    c.setFont(font, size)
    c.setFillColor(color)
    for b in bits:
        if b.startswith(icon_prefix):
            _draw_phone_icon(c, x + icon_r, y + size * 0.32, icon_r, color)
            x += icon_r * 2 + icon_gap
        c.drawString(x, y, b)
        x += c.stringWidth(b, font, size) + space_w


HOLOGRAM_PATH = os.path.join(os.path.dirname(__file__), "static", "branding", "hologram_circle_small.png")
# ^ Downsampled from the original 622x624 source (still kept on disk,
# unused) to 240x240 -- the hologram is only ever drawn into a 24x27pt box
# on the receipt (see _draw_hologram call below), so the original was ~25x
# oversized in each dimension and was, by far, the single biggest
# contributor to each generated receipt's file size (embedding raw bitmap data
# for a sticker nobody ever sees larger than a thumbnail). 240x240 is still
# an 8-9x supersample over the box it's drawn into, comfortable headroom
# for print quality with a fraction of the embedded data.


def _draw_hologram(c, x0, y0, x1, y1):
    """The real ISKCON hologram-sticker artwork (a circular holographic
    foil sticker with repeating rainbow "HARE KRISHNA" text and an
    embossed lotus/ISKCON mark) if the asset is present, else falls back
    to a plain corner-bracket placeholder."""
    if os.path.isfile(HOLOGRAM_PATH):
        from reportlab.lib.utils import ImageReader
        img = ImageReader(HOLOGRAM_PATH)
        size = min(x1 - x0, y1 - y0)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        c.drawImage(
            img, cx - size / 2, cy - size / 2, width=size, height=size,
            preserveAspectRatio=True, mask="auto",
        )
    else:
        _draw_hologram_placeholder(c, x0, y0, x1, y1)


def _draw_hologram_placeholder(c, x0, y0, x1, y1):
    """Corner-bracket placeholder with "holo / gram" text -- fallback used
    only if the real hologram artwork asset is missing."""
    c.saveState()
    c.setStrokeColor(GREY)
    c.setLineWidth(0.6)
    arm = 4
    for cx, cy, dx, dy in [
        (x0, y1, 1, -1), (x1, y1, -1, -1), (x0, y0, 1, 1), (x1, y0, -1, 1),
    ]:
        c.line(cx, cy, cx + arm * dx, cy)
        c.line(cx, cy, cx, cy + arm * dy)
    c.setFont(SERIF, 7)
    c.setFillColor(GREY)
    c.drawCentredString((x0 + x1) / 2, (y0 + y1) / 2 + 3, "holo")
    c.drawCentredString((x0 + x1) / 2, (y0 + y1) / 2 - 5, "gram")
    c.restoreState()


def generate_receipt_pdf(donation, donor, campaign, org_cfg):
    """Generates the donation receipt PDF and returns the raw PDF bytes.

    Built entirely in memory (no file written to disk) -- the caller is
    responsible for persisting it, normally to Donation.receipt_pdf, so it
    survives exactly as issued regardless of what host/filesystem this is
    running on. Same in-memory approach as generate_annual_statement_pdf
    below.

    This mirrors the temple's actual issued receipt (a landscape,
    two-page document: page 1 is the filled receipt, page 2 the Terms &
    Conditions / closing chant that's printed on the back of the physical
    form) field-for-field: exact colours, exact box layout, exact wording,
    down to the Rupee-sign icon and the "holo / gram" hologram placeholder.

    IMPORTANT (for whoever maintains this -- not printed on the receipt,
    matching the temple's actual template, which doesn't carry this text
    either): for 80G-eligible donations this is still only the instant
    donation receipt. Page 2 already explains that Form 10BE (the real
    80G certificate) follows separately, generally by 31 May of the next
    financial year -- see README for how Form 10BD export ties into this.
    """
    buffer = io.BytesIO()

    # Single page now: the receipt itself (exact size of the temple's own
    # receipt PDF) with the Terms & Conditions / closing chant -- previously
    # a separate page -- appended in a strip below it, rather than on a
    # second page.
    width, height = 612, 396
    TC_H = 260
    c = canvas.Canvas(buffer, pagesize=(width, height + TC_H))

    # ---------------------------------------------------------------
    # RECEIPT -- drawn translated up by TC_H so all of its existing
    # (height=396-relative) coordinates land unchanged in the top portion
    # of the taller page.
    # ---------------------------------------------------------------
    c.saveState()
    c.translate(0, TC_H)
    _draw_watermark(c, width, height)

    # Logo (top-left) -- the bundled image already includes the "ISKCON" /
    # branch-name lockup exactly as printed on the real receipt.
    logo_path = org_cfg.get("ORG_LOGO_PATH")
    if logo_path and os.path.isfile(logo_path):
        from reportlab.lib.utils import ImageReader
        img = ImageReader(logo_path)
        iw, ih = img.getSize()
        logo_h = 104
        logo_w = logo_h * iw / ih
        c.drawImage(
            img, 13, height - 121, width=logo_w, height=logo_h,
            preserveAspectRatio=True, mask="auto",
        )

    # Title block -- centred in the gap between the logo (left) and the
    # hologram placeholder (right), shrinking to fit so long org names
    # never run under the hologram box.
    title_cx = (140 + 572) / 2
    title_max_w = 572 - 140
    title_text = org_cfg.get("ORG_PARENT_NAME") or org_cfg["ORG_NAME"]
    title_size = 18
    while title_size > 10 and c.stringWidth(title_text, SERIF_BOLD, title_size) > title_max_w:
        title_size -= 0.5
    c.setFont(SERIF_BOLD, title_size)
    c.setFillColor(NAVY)
    c.drawCentredString(title_cx, height - 28, title_text)

    if org_cfg.get("ORG_FOUNDER_LINE"):
        founder_text = org_cfg["ORG_FOUNDER_LINE"]
        founder_size = 11.5
        while founder_size > 8 and c.stringWidth(founder_text, SERIF, founder_size) > title_max_w:
            founder_size -= 0.5
        c.setFont(SERIF, founder_size)
        c.drawCentredString(title_cx, height - 45, founder_text)

    # Hologram sticker (top-right corner)
    _draw_hologram(c, 574, 354.6, 598, 381.6)

    # Address box (yellow)
    addr_box = (136.4, 274.7, 378.0, 346.7)
    _box(c, addr_box, fill=YELLOW, stroke=PINK)
    addr_lines = (org_cfg.get("ORG_ADDRESS") or "").split("\n")
    branch_type = org_cfg.get("ORG_BRANCH_TYPE") or "Branch"
    display_lines = [f"{branch_type}: {addr_lines[0]}"] + addr_lines[1:] if addr_lines and addr_lines[0] else [branch_type]
    contact_bits = []
    if org_cfg.get("ORG_PHONE"):
        contact_bits.append(f"Mobile:  {org_cfg['ORG_PHONE']}")
    if org_cfg.get("ORG_EMAIL"):
        contact_bits.append(f"E-mail: {org_cfg['ORG_EMAIL']}")
    display_lines += contact_bits
    cx = (addr_box[0] + addr_box[2]) / 2
    ay = addr_box[3] - 14
    for line in display_lines:
        c.setFont(SERIF, 10)
        c.setFillColor(INK)
        if line.startswith("Mobile:"):
            icon_r = 4.2
            gap = 3
            tw = c.stringWidth(line, SERIF, 10)
            start_x = cx - (tw + icon_r * 2 + gap) / 2
            _draw_phone_icon(c, start_x + icon_r, ay + 3.2, icon_r)
            c.drawString(start_x + icon_r * 2 + gap, ay, line)
        else:
            c.drawCentredString(cx, ay, line)
        ay -= 12.6

    # Donation Receipt No. + Date + Donor's Copy badge
    c.setFont(SERIF_BOLD, 11)
    c.setFillColor(PINK)
    c.drawString(391, height - 55, "Donation")
    c.drawString(391, height - 71, "Receipt No.")
    # Shrink-to-fit: our receipt numbers ("GEN/2026-27/00010") run longer
    # than the short codes on the physical template, and at a fixed 17pt
    # they were running off the right edge of the page.
    # The real template sets this in a plain sans-serif face (unlike every
    # other value on the receipt, which is serif) -- matched here rather
    # than left as serif.
    receipt_text = donation.receipt_number
    receipt_size = 17
    receipt_max_w = 600 - 466
    while receipt_size > 9 and c.stringWidth(receipt_text, "Helvetica-Bold", receipt_size) > receipt_max_w:
        receipt_size -= 0.5
    c.setFont("Helvetica-Bold", receipt_size)
    c.setFillColor(INK)
    c.drawString(466, height - 66, receipt_text)

    date_box = (466.2, 270.7, 597.2, 306.4)
    _box(c, date_box, fill=LAVENDER, stroke=PINK)
    _box_label_above(c, date_box, "Date")
    c.setFont(SERIF, 10)
    c.setFillColor(INK)
    c.drawCentredString((date_box[0] + date_box[2]) / 2, (date_box[1] + date_box[3]) / 2 - 3,
                         donation.donation_date.strftime("%d-%m-%Y"))

    donorcopy_box = (391.3, 270.4, 459.4, 304.2)
    _box(c, donorcopy_box, fill=DONOR_BADGE, stroke=None, line_width=0)
    c.setFont(SERIF_BOLD, 11)
    c.setFillColor(colors.white)
    c.drawCentredString((donorcopy_box[0] + donorcopy_box[2]) / 2, donorcopy_box[3] - 15, "DONOR'S")
    c.drawCentredString((donorcopy_box[0] + donorcopy_box[2]) / 2, donorcopy_box[1] + 9, "COPY")

    # Donation Amount in Rupees -- shown both in words (left) and numerals
    # (right, next to the Rupee icon), like a cheque.
    amount_box = (49.7, 228.2, 597.2, 262.8)
    _box(c, amount_box, fill=LAVENDER, stroke=PINK)
    _box_label_above(c, amount_box, "Donation Amount in Rupees")
    rupee_box = (465.8, 232.2, 488.5, 257.4)
    _box(c, rupee_box, fill=PINK, stroke=None, line_width=0, radius=2)
    c.setFont(SERIF_BOLD, 15)
    c.setFillColor(colors.white)
    c.drawCentredString((rupee_box[0] + rupee_box[2]) / 2, (rupee_box[1] + rupee_box[3]) / 2 - 5, RUPEE)

    amt = float(donation.amount)
    if abs(amt - round(amt)) < 0.005:
        amount_display = f"{format_inr(amt, decimals=0)}/-"
    else:
        amount_display = format_inr(amt, decimals=2)

    # Numeral now sits AFTER (to the right of) the Rupee icon, not before
    # it -- shrunk-to-fit to whatever room is left to the box's right edge.
    numeral_x = rupee_box[2] + 8
    numeral_max_w = amount_box[2] - 8 - numeral_x
    numeral_size = 15
    while numeral_size > 7 and c.stringWidth(amount_display, SERIF_BOLD, numeral_size) > numeral_max_w:
        numeral_size -= 0.5
    c.setFont(SERIF_BOLD, numeral_size)
    c.setFillColor(INK)
    c.drawString(numeral_x, (amount_box[1] + amount_box[3]) / 2 - numeral_size * 0.33, amount_display)

    words_text = amount_to_words_inr(donation.amount)
    words_max_w = rupee_box[0] - 12 - (amount_box[0] + 8)
    words_size = 15
    while words_size > 7 and c.stringWidth(words_text, SERIF, words_size) > words_max_w:
        words_size -= 0.5
    c.setFont(SERIF, words_size)
    c.setFillColor(INK)
    if c.stringWidth(words_text, SERIF, words_size) <= words_max_w:
        # Fits on one line -- vertically centred in the box, as usual.
        c.drawString(amount_box[0] + 8, (amount_box[1] + amount_box[3]) / 2 - words_size * 0.32, words_text)
    else:
        # Extremely large amount: even the smallest single-line size
        # doesn't fit next to the numeral -- wrap to 2 lines rather than
        # let it run under the numeral.
        w_lines = _wrap_to_width(c, words_text, SERIF, words_size, words_max_w)[:2]
        line_h = words_size * 1.15
        top = (amount_box[1] + amount_box[3]) / 2 + line_h / 2 - words_size * 0.32
        for i, line in enumerate(w_lines):
            c.drawString(amount_box[0] + 8, top - i * line_h, line)

    # Donor Details box
    donor_box = (49.7, 58.0, 341.3, 220.7)
    _box(c, donor_box, fill=LAVENDER, stroke=PINK)
    _box_label_above(c, donor_box, "Donor Details", size=10)

    donor_fields = [
        ("Name", donor.full_name, 2),
        ("Address", donor.address or "-", 5),
        ("PIN", donor.pincode or "-", 1),
    ]
    # QA report REG-034: a PAN on file (kept for one donation that needed
    # it, or entered once and reused by find_or_create_donor for a later
    # one) used to print on *every* receipt for that donor, even a small
    # non-80G one with no legal reason to show it. Only print PAN when
    # this specific donation is 80G-eligible or crosses the same
    # high-value threshold that requires PAN to be collected at all --
    # matching the rule already enforced at collection time.
    if donation.effective_is_80g or (donation.amount or 0) > HIGH_VALUE_PAN_THRESHOLD:
        donor_fields.append(("PAN", donor.pan or "-", 1))
    donor_fields.append(("Mobile", donor.phone or "-", 1))
    whatsapp = getattr(donor, "whatsapp_number", None)
    if whatsapp and whatsapp != donor.phone:
        # Only shown when it's actually a different number -- most donors
        # never fill this in separately, and the printed receipt shouldn't
        # show a redundant duplicate of Mobile.
        donor_fields.append(("WhatsApp", whatsapp, 1))
    donor_fields.append(("E-mail", donor.email or "-", 1))

    # Slightly tighter when the optional WhatsApp row is present, so 7
    # fields still fit the box as comfortably as 6 normally do.
    LINE_H = 11.8 if len(donor_fields) > 6 else 12.6
    fy = donor_box[3] - 18
    for label, value, max_lines in donor_fields:
        # Only the field label (e.g. "Name") is bold; the value itself
        # stays regular weight.
        c.setFont(SERIF_BOLD, 10.5)
        c.setFillColor(INK)
        label_x = donor_box[0] + 8
        if label in ("Mobile", "WhatsApp"):
            icon_r = 4.0
            _draw_phone_icon(c, label_x + icon_r, fy + 3, icon_r)
            label_x += icon_r * 2 + 2.5
        label_w = c.stringWidth(label + "  ", SERIF_BOLD, 10.5)
        c.drawString(label_x, fy, label)
        value_start_x = label_x + label_w
        value_max_w = (donor_box[2] - 8) - value_start_x

        # Try 10.5pt first; if a long value (e.g. Address) doesn't fit in
        # max_lines at that size, shrink the value's font down (not the
        # label) before ever falling back to an ellipsis, so long values
        # need genuinely extreme length to actually get cut off.
        value_size = 10.5
        all_value_lines = _wrap_to_width(c, str(value), SERIF, value_size, value_max_w)
        while len(all_value_lines) > max_lines and value_size > 8:
            value_size -= 0.5
            all_value_lines = _wrap_to_width(c, str(value), SERIF, value_size, value_max_w)

        value_lines = all_value_lines[:max_lines] or ["-"]
        if len(all_value_lines) > max_lines and value_lines:
            # Still doesn't fit even at the smallest size -- show it's cut
            # off rather than silently dropping characters with no visual
            # indication.
            last = value_lines[-1]
            while last and c.stringWidth(last + "…", SERIF, value_size) > value_max_w:
                last = last[:-1]
            value_lines[-1] = last + "…"
        c.setFont(SERIF, value_size)
        vy = fy
        for i, line in enumerate(value_lines):
            vx = value_start_x if i == 0 else donor_box[0] + 8
            c.drawString(vx, vy, line)
            c.setStrokeColor(colors.HexColor("#c9c6da"))
            c.setLineWidth(0.5)
            c.line(donor_box[0] + 8, vy - 3, donor_box[2] - 8, vy - 3)
            vy -= LINE_H
        fy = vy if len(value_lines) > 1 else fy - LINE_H

    # Mode of Payment / Payment Details / Purpose of Donation
    mode_box = (347.0, 185.8, 597.2, 221.0)
    payment_box = (347.0, 142.9, 597.2, 178.2)
    purpose_box = (347.0, 100.4, 597.2, 135.7)

    _box(c, mode_box, fill=LAVENDER, stroke=PINK)
    _box_label_above(c, mode_box, "Mode of Payment (Cheque / Online / UPI / Cash)", size=9.5)
    _value_in_box(c, mode_box, donation.payment_mode.replace("_", " ").title())

    _box(c, payment_box, fill=LAVENDER, stroke=PINK)
    _box_label_above(c, payment_box, "Payment Details (Cheque / Transaction Details)", size=9.5)
    # reference_display already picks the right field for whichever
    # payment_mode this donation actually used -- razorpay_payment_id for
    # online, "Cheque #.../ (Bank)" for cheque, the UTR/transaction ID for
    # bank transfer. This used to check razorpay_payment_id directly and
    # nothing else, so cheque and bank-transfer donations always fell
    # through to remarks (usually blank) and printed "-" here instead of
    # the reference the donor/office actually needs to reconcile a
    # cheque or bank transfer against.
    _value_in_box(c, payment_box, donation.reference_display or donation.remarks or "-")

    _box(c, purpose_box, fill=LAVENDER, stroke=PINK)
    _box_label_above(c, purpose_box, "Purpose of Donation (Corpus / General / Others)", size=9.5)
    if campaign.name == "BACE Contribution":
        # BACE Contribution receipts always read "BACE Contribution" here,
        # regardless of which property the payment was for -- the property
        # itself isn't shown on the receipt's Purpose line.
        purpose_text = "BACE Contribution"
    elif campaign.name == "Live To Give" and getattr(donation, "live_to_give_purpose", None):
        # Live To Give receipts show the specific purpose the donor picked
        # (e.g. "Cow Protection", "Temple Construction") in place of the
        # campaign name.
        purpose_text = donation.live_to_give_purpose.name
    else:
        # For Festival Seva, show which occasion (and seva tier) this
        # payment is for alongside the campaign name -- _value_in_box
        # already wraps to 2 lines, which fits these combinations for the
        # names in use.
        purpose_parts = [campaign.name]
        if getattr(donation, "festival", None):
            purpose_parts.append(donation.festival.name)
        if getattr(donation, "seva_type", None):
            purpose_parts.append(donation.seva_type.name)
        purpose_text = " -- ".join(purpose_parts)
    _value_in_box(c, purpose_box, purpose_text)

    # Signature boxes removed entirely, per request -- no boxes, no captions.

    # Footer: Registered Office (yellow box)
    footer_box = (49.3, 10.8, 583.9, 46.1)
    _box(c, footer_box, fill=YELLOW, stroke=PINK, radius=3)
    ro_bits = [f"Registered Office: {org_cfg.get('ORG_HO_ADDRESS', '')}."]
    if org_cfg.get("ORG_HO_PHONE"):
        ro_bits.append(f"Mobile: {org_cfg['ORG_HO_PHONE']}.")
    if org_cfg.get("ORG_HO_EMAIL"):
        ro_bits.append(f"E-mail: {org_cfg['ORG_HO_EMAIL']}")
    reg_bits = []
    if org_cfg.get("ORG_REG_INFO"):
        reg_bits.append(org_cfg["ORG_REG_INFO"] + ".")
    reg_bits.append(f"Unique Regn. No. (80G): {org_cfg['ORG_80G_REG_NO']}")

    footer_max_w = footer_box[2] - footer_box[0] - 20

    ro_size = 9
    while ro_size > 6 and _bits_with_icon_width(c, ro_bits, SERIF, ro_size) > footer_max_w:
        ro_size -= 0.25
    _draw_centered_bits_with_icon(c, width / 2, footer_box[3] - 14, ro_bits, SERIF, ro_size, INK)

    reg_text = " ".join(reg_bits)
    reg_size = 9
    while reg_size > 6 and c.stringWidth(reg_text, SERIF, reg_size) > footer_max_w:
        reg_size -= 0.25
    c.setFont(SERIF, reg_size)
    c.setFillColor(INK)
    c.drawCentredString(width / 2, footer_box[3] - 26, reg_text)
    c.restoreState()

    # ---------------------------------------------------------------
    # TERMS & CONDITIONS -- verbatim from the temple's own receipt
    # backside, plus the closing chant. Drawn in the TC_H-tall strip below
    # the receipt (y=0 to y=TC_H), in its own coordinate frame -- no
    # watermark here, matching the original backside.
    # ---------------------------------------------------------------
    margin2 = 44
    y = TC_H - 24
    c.setFont(SERIF_BOLD, 12)
    c.setFillColor(NAVY)
    c.drawString(margin2, y, "Please note Terms and Conditions (T&C):")
    y -= 19

    terms = [
        "This donation receipt is an acknowledgement only.",
        "For all type of donations, irrespective of amount and mode of payment, full legal name and address "
        "with PIN are required.",
        "PAN is compulsory for all donation of Rs. 50,000/- or more.",
        "In case of payment by cheque, this donation receipt is valid subject to clearance of the cheque.",
        "In case of any error/discrepancy in this receipt, including your Name, address, E-mail ID, WhatsApp "
        "number etc. Please contact the receipt issuing centre for correction.",
    ]
    c.setFont(SERIF, 9.5)
    for term in terms:
        lines = _wrap_to_width(c, term, SERIF, 9.5, width - 2 * margin2 - 16)
        c.setFillColor(NAVY)
        c.drawString(margin2, y, "•")
        for i, line in enumerate(lines):
            c.drawString(margin2 + 14, y, line)
            if i < len(lines) - 1:
                y -= 12
        y -= 13.5

    y -= 20
    c.setFont(SERIF, 10.5)
    c.setFillColor(NAVY)
    c.drawCentredString(width / 2, y, "Thank you for your support")
    y -= 18

    closing = (org_cfg.get("ORG_CLOSING_MESSAGE") or "").split(" / ")
    for line in closing:
        line = line.strip()
        if not line:
            continue
        if line.isupper() or "hare" in line.lower() and ("krishna" in line.lower() or "rama" in line.lower()):
            c.setFont(SERIF_BOLD, 12)
        else:
            c.setFont(SERIF, 10.5)
        c.setFillColor(NAVY)
        c.drawCentredString(width / 2, y, line)
        y -= 15

    # Rotated year stamp along the right edge, matching the real template's
    # own print-year marker in this spot.
    stamp_year = donation.donation_date.year if donation.donation_date else datetime.datetime.now().year
    c.saveState()
    c.setFillColor(NAVY)
    c.setFont(SERIF_BOLD, 9)
    c.translate(width - 12, TC_H * 0.33)
    c.rotate(90)
    c.drawString(0, 0, str(stamp_year))
    c.restoreState()

    # Sits right below the closing chant, using the running cursor (not a
    # hard-coded y) so it never overlaps "and be happy." above it -- this is
    # a computer-generated receipt, so no signature is required for it to
    # be valid.
    note_y = max(y - 14, 15)
    c.setFont(SERIF_ITALIC, 8.5)
    c.setFillColor(GREY)
    c.drawCentredString(width / 2, note_y, "This is a computer-generated receipt and does not need a signature.")

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer.getvalue()


def generate_annual_statement_pdf(donor, donations, fy, org_cfg):
    """Builds a single consolidated 'here's everything I gave this financial
    year' PDF for a donor, generated on demand and returned in-memory (no
    file written to disk -- so this works the same whether the app is on
    local storage or, later, a serverless host with no persistent disk).

    `donations` should already be filtered to this donor + fy + status success,
    ordered by donation_date.
    """
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    margin = 18 * mm

    col_date_x = margin
    col_receipt_x = margin + 22 * mm
    col_campaign_x = margin + 62 * mm
    col_type_x = margin + 122 * mm
    col_amount_x = width - margin

    def draw_header_block():
        y = height - margin
        c.setFont("Helvetica-Bold", 16)
        c.setFillColor(MAROON)
        c.drawCentredString(width / 2, y, org_cfg["ORG_NAME"])
        y -= 6 * mm
        c.setFont("Helvetica", 9)
        c.setFillColor(INK)
        c.drawCentredString(width / 2, y, (org_cfg["ORG_ADDRESS"] or "").replace("\n", ", "))
        y -= 5 * mm
        c.setFillColor(GREY)
        c.drawCentredString(width / 2, y, f"PAN: {org_cfg['ORG_PAN']}   |   80G Regn No: {org_cfg['ORG_80G_REG_NO']}")
        y -= 6 * mm
        c.setFillColor(SAFFRON)
        c.rect(margin, y - 1 * mm, width - 2 * margin, 1 * mm, stroke=0, fill=1)
        y -= 1 * mm
        c.setStrokeColor(MAROON)
        c.setLineWidth(0.5)
        c.line(margin, y - 0.6 * mm, width - margin, y - 0.6 * mm)
        y -= 8 * mm

        c.setFont("Helvetica-Bold", 13)
        c.setFillColor(MAROON_DARK)
        c.drawCentredString(width / 2, y, f"ANNUAL DONATION STATEMENT - FY {fy}")
        y -= 10 * mm

        c.setFont("Helvetica-Bold", 10)
        c.setFillColor(INK)
        c.drawString(margin, y, "Donor:")
        c.setFont("Helvetica", 10)
        c.drawString(margin + 20 * mm, y, donor.full_name)
        y -= 6 * mm

        c.setFont("Helvetica-Bold", 10)
        c.drawString(margin, y, "PAN:")
        c.setFont("Helvetica", 10)
        c.drawString(margin + 20 * mm, y, donor.pan or "-")
        y -= 6 * mm

        c.setFont("Helvetica-Bold", 10)
        c.drawString(margin, y, "Contact:")
        c.setFont("Helvetica", 10)
        contact = " / ".join(filter(None, [donor.phone, donor.email])) or "-"
        c.drawString(margin + 20 * mm, y, contact)
        y -= 9 * mm

        c.setFont("Helvetica-Bold", 9)
        c.setFillColor(MAROON_DARK)
        c.drawString(col_date_x, y, "Date")
        c.drawString(col_receipt_x, y, "Receipt No.")
        c.drawString(col_campaign_x, y, "Campaign")
        c.drawString(col_type_x, y, "Type")
        c.drawRightString(col_amount_x, y, "Amount (Rs.)")
        y -= 2 * mm
        c.setStrokeColor(colors.grey)
        c.line(margin, y, width - margin, y)
        y -= 5 * mm
        return y

    y = draw_header_block()
    c.setFont("Helvetica", 9)
    c.setFillColor(INK)

    total_80g = 0.0
    total_non_80g = 0.0

    for d in donations:
        if y < margin + 30 * mm:
            c.showPage()
            y = draw_header_block()
            c.setFont("Helvetica", 9)
            c.setFillColor(INK)

        amount = float(d.amount)
        is_80g = d.effective_is_80g
        if is_80g:
            total_80g += amount
        else:
            total_non_80g += amount

        c.setFillColor(INK)
        c.drawString(col_date_x, y, d.donation_date.strftime("%d-%b-%Y"))
        c.drawString(col_receipt_x, y, d.receipt_number or "-")
        c.drawString(col_campaign_x, y, (d.campaign.name or "")[:28])
        c.drawString(col_type_x, y, "80G" if is_80g else "Non-80G")
        c.drawRightString(col_amount_x, y, format_inr(amount, decimals=2))
        y -= 6 * mm

    if not donations:
        c.setFont("Helvetica-Oblique", 10)
        c.drawString(margin, y, "No donations recorded for this financial year.")
        y -= 8 * mm

    y -= 4 * mm
    c.setStrokeColor(colors.grey)
    c.line(margin, y, width - margin, y)
    y -= 8 * mm

    grand_total = total_80g + total_non_80g
    c.setFont("Helvetica-Bold", 10)
    c.setFillColor(INK)
    if total_80g:
        c.drawString(margin, y, "Total 80G-Eligible Donations:")
        c.drawRightString(col_amount_x, y, format_inr(total_80g, decimals=2))
        y -= 6 * mm
    if total_non_80g:
        c.drawString(margin, y, "Total Non-80G Collections:")
        c.drawRightString(col_amount_x, y, format_inr(total_non_80g, decimals=2))
        y -= 6 * mm
    c.setFont("Helvetica-Bold", 11)
    c.setFillColor(MAROON)
    c.drawString(margin, y, "Grand Total:")
    c.drawRightString(col_amount_x, y, format_inr(grand_total, decimals=2))
    y -= 6 * mm
    c.setFont("Helvetica", 9)
    c.setFillColor(INK)
    c.drawString(margin, y, amount_to_words_inr(grand_total))
    y -= 10 * mm

    if total_80g:
        note = (
            "The 80G-eligible portion above is covered by instant donation receipts, NOT the "
            "official Section 80G certificate. As per Income Tax rules, the official certificate "
            "(Form 10BE, bearing an Income Tax Department ARN) is issued after the trust files its "
            "annual Form 10BD statement of donations for this financial year."
        )
        c.setFont("Helvetica-Oblique", 8)
        c.setFillColor(GREY)
        text_obj = c.beginText(margin, y)
        text_obj.setFont("Helvetica-Oblique", 8)
        for line in _wrap_text(note, 100):
            text_obj.textLine(line)
        c.drawText(text_obj)

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer


def _wrap_text(text, width):
    words = text.split()
    lines = []
    current = ""
    for w in words:
        if len(current) + len(w) + 1 <= width:
            current = (current + " " + w).strip()
        else:
            lines.append(current)
            current = w
    if current:
        lines.append(current)
    return lines
