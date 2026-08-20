"""REG-034 (QA report, 2026-08-20): a PAN on file for a donor -- stored
because one earlier donation actually required it -- used to print on
*every* subsequent receipt for that donor, including a small non-80G one
with no legal reason to show it. generate_receipt_pdf() now only
includes the PAN field when *that specific donation* is 80G-eligible or
crosses the same high-value threshold that requires PAN to be collected
in the first place (utils.HIGH_VALUE_PAN_THRESHOLD) -- mirroring the
rule already enforced at collection time
(public.high_value_pan_address_error).

Goes through the real /api/create-order + /api/simulate-payment flow
(not a direct call into generate_receipt_pdf) so this exercises the
actual code path a donor's browser drives, then extracts the stored
receipt PDF's text with pdfplumber to check what a human reading the
printed receipt would actually see.
"""
import io

import pdfplumber


def _receipt_text(pdf_bytes):
    """Extracts the receipt's real printed text, excluding the repeating
    diagonal "ISKCON" watermark that tiles the whole page.

    The watermark sits at the same z-order/position as ordinary field
    text (see pdf_utils.py's _box/watermark drawing), so pdfplumber's
    default word-joining interleaves its characters with genuine field
    values that happen to fall in the same region -- e.g. a PAN value
    like "ABCDE1234F" would extract as a garbled mix with "ISKCON". The
    watermark is drawn in a distinctly light colour (near-white) while
    all real printed text is dark ink, so filtering characters by
    fill-colour brightness cleanly separates the two.
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        parts = []
        for page in pdf.pages:
            ink_chars = [
                c for c in page.chars
                if isinstance(c.get("non_stroking_color"), (tuple, list))
                and max(c["non_stroking_color"][:3]) < 0.6
            ]
            parts.append(_words_to_text(ink_chars))
        return "\n".join(parts)


def _words_to_text(chars):
    """Groups already-filtered characters back into a readable string,
    line by line (grouped by rounded vertical position) and left to
    right within each line -- enough to substring-search for a label or
    value without needing pdfplumber's full word-segmentation machinery
    (which assumes a clean, non-overlapping character stream)."""
    if not chars:
        return ""
    lines = {}
    for c in chars:
        key = round(c["top"], 0)
        lines.setdefault(key, []).append(c)
    out_lines = []
    for key in sorted(lines):
        row = sorted(lines[key], key=lambda c: c["x0"])
        line_str = ""
        prev_x1 = None
        for c in row:
            if prev_x1 is not None and c["x0"] - prev_x1 > 1.5:
                line_str += " "
            line_str += c["text"]
            prev_x1 = c["x1"]
        out_lines.append(line_str)
    return "\n".join(out_lines)


def _donate_and_pay(client, **overrides):
    data = {
        "amount": 501, "full_name": "Receipt Field Test Donor", "phone": "9177001122",
        "consent": "on",
    }
    data.update(overrides)
    order_resp = client.post("/api/create-order", json=data)
    assert order_resp.status_code == 200, order_resp.get_json()
    donation_id = order_resp.get_json()["donation_id"]
    pay_resp = client.post("/api/simulate-payment", json={"donation_id": donation_id})
    assert pay_resp.status_code == 200, pay_resp.get_json()
    return donation_id


class TestReceiptPanGating:
    def test_80g_donation_receipt_shows_pan(self, app, client):
        """Annadan is 80G-eligible (see conftest) -- a PAN supplied for
        it is legally required to be on the receipt."""
        from models import Campaign, Donation
        campaign = Campaign.query.filter_by(name="Annadan").first()

        donation_id = _donate_and_pay(client, campaign_id=campaign.id, pan="ABCDE1234F")

        donation = Donation.query.get(donation_id)
        text = _receipt_text(donation.receipt_pdf)
        assert "ABCDE1234F" in text
        assert "PAN" in text

    def test_non_80g_low_value_receipt_hides_pan_even_if_on_file(self, app, client):
        """Same donor as above (same phone + name), so the PAN stays on
        their donor record -- but *this* donation is against BACE
        Contribution (not 80G-eligible) and well under the high-value
        threshold, so the receipt for it must not print PAN at all."""
        from models import Campaign, Donation, Donor

        # First, an 80G donation to get a PAN on file for this donor.
        annadan = Campaign.query.filter_by(name="Annadan").first()
        _donate_and_pay(client, campaign_id=annadan.id, pan="ABCDE1234F", phone="9177002233")

        bace = Campaign.query.filter_by(name="BACE Contribution").first()
        donation_id = _donate_and_pay(client, campaign_id=bace.id, phone="9177002233", amount=501)

        donation = Donation.query.get(donation_id)
        donor = Donor.query.get(donation.donor_id)
        assert donor.pan == "ABCDE1234F"  # confirms the PAN really is on file for this donor

        text = _receipt_text(donation.receipt_pdf)
        assert "ABCDE1234F" not in text
        # The receipt's fixed Terms & Conditions text always includes a
        # bullet explaining *when* PAN is required ("PAN is compulsory
        # for all donation of Rs. 50,000/- or more") -- that's generic
        # boilerplate, not this donor's PAN, and stays on every receipt
        # regardless of this gate. What must NOT appear is the donor
        # box's own "PAN <value>" field row, which (unlike that sentence)
        # is never followed by the word "is".
        assert not any(
            line.strip().startswith("PAN") and not line.strip().startswith("PAN is")
            for line in text.splitlines()
        )

    def test_non_80g_high_value_receipt_still_shows_pan(self, app, client):
        """BACE Contribution is not 80G, but a large enough amount still
        legally requires PAN to be quoted (Income Tax Rule 114B) -- the
        receipt for it must keep showing PAN regardless of 80G status."""
        from models import Campaign, Donation
        bace = Campaign.query.filter_by(name="BACE Contribution").first()

        donation_id = _donate_and_pay(
            client, campaign_id=bace.id, phone="9177003344", amount=50001,
            pan="PQRSX5678K", address="123 Test Street, Test City",
        )
        donation = Donation.query.get(donation_id)
        text = _receipt_text(donation.receipt_pdf)
        assert "PQRSX5678K" in text
        assert "PAN" in text
