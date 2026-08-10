import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from extensions import db
from models import Donor, ReceiptCounter


class TestDonorDedup:
    """Covers the core problem this whole system was built to fix: the same
    donor should never end up with two records just because they gave to a
    different campaign or typed their details slightly differently.

    The `app` fixture (conftest.py) keeps a single app context open for the
    whole test, so no need to open app_context() again here.
    """

    def test_second_donation_same_phone_reuses_donor(self, app):
        from public import find_or_create_donor

        d1 = find_or_create_donor({"full_name": "Radha Devi", "phone": "9876543210", "email": "radha@example.com"})
        db.session.commit()

        d2 = find_or_create_donor({"full_name": "Radha Devi", "phone": "9876543210", "email": ""})
        db.session.commit()

        assert d1.id == d2.id
        assert Donor.query.count() == 1

    def test_matches_by_pan_even_if_phone_differs(self, app):
        from public import find_or_create_donor

        d1 = find_or_create_donor({"full_name": "Govind Das", "phone": "9111111111", "pan": "ABCDE1234F"})
        db.session.commit()

        # Same PAN, different (e.g. new) phone number -- should still match.
        d2 = find_or_create_donor({"full_name": "Govind Das", "phone": "9222222222", "pan": "abcde1234f"})
        db.session.commit()

        assert d1.id == d2.id
        assert Donor.query.count() == 1

    def test_backfills_missing_fields_on_existing_donor(self, app):
        from public import find_or_create_donor

        d1 = find_or_create_donor({"full_name": "Krishna Prasad", "phone": "9333333333"})
        db.session.commit()
        assert d1.email is None

        d2 = find_or_create_donor({
            "full_name": "Krishna Prasad", "phone": "9333333333", "email": "krishna@example.com",
        })
        db.session.commit()

        assert d1.id == d2.id
        assert d2.email == "krishna@example.com"

    def test_later_donation_updates_details_for_same_person_shared_contact(self, app):
        """A shared phone/PAN/email is common in Indian households -- spouse,
        parents, or grown children all donating under one family contact.
        If the SAME person (name matches, allowing for case/whitespace
        differences) donates again under that shared contact, their latest
        address/city etc. should update the existing record rather than
        being ignored."""
        from public import find_or_create_donor

        d1 = find_or_create_donor({
            "full_name": "Ramesh Kumar", "phone": "9123456789", "address": "12 MG Road", "city": "Delhi",
        })
        db.session.commit()
        assert d1.full_name == "Ramesh Kumar"

        d2 = find_or_create_donor({
            "full_name": "  ramesh   kumar ", "phone": "9123456789", "address": "45 Nehru Place", "city": "Delhi",
        })
        db.session.commit()

        assert d1.id == d2.id
        assert d2.address == "45 Nehru Place"

    def test_shared_phone_different_name_creates_separate_donor(self, app):
        """The core fix: a phone number shared by more than one real person
        (spouse, parents, grown children all donating through one family
        contact) must NOT collapse into a single donor record just because
        the phone matches -- only a matching name (or PAN) means "same
        person". A different name under the same phone gets its own donor
        record, and the original donor's details are left completely
        untouched."""
        from public import find_or_create_donor

        d1 = find_or_create_donor({
            "full_name": "Ramesh Kumar", "phone": "9123456789", "address": "12 MG Road", "city": "Delhi",
        })
        db.session.commit()

        d2 = find_or_create_donor({
            "full_name": "Sita Devi", "phone": "9123456789", "address": "45 Nehru Place", "city": "Delhi",
        })
        db.session.commit()

        assert d1.id != d2.id
        assert Donor.query.count() == 2
        assert d1.full_name == "Ramesh Kumar" and d1.address == "12 MG Road"
        assert d2.full_name == "Sita Devi" and d2.address == "45 Nehru Place"

    def test_shared_phone_different_name_and_pan_does_not_overwrite_original(self, app):
        """Reported flaw #1: person A donates and their PAN is saved. Later,
        a different family member donates through the same phone with their
        OWN, different PAN. That must create a separate donor -- it must
        never overwrite person A's record with person B's name/PAN."""
        from public import find_or_create_donor

        d1 = find_or_create_donor({"full_name": "Ramesh Kumar", "phone": "9123450001", "pan": "AAAAA1111A"})
        db.session.commit()

        d2 = find_or_create_donor({"full_name": "Sita Devi", "phone": "9123450001", "pan": "BBBBB2222B"})
        db.session.commit()

        assert d1.id != d2.id
        assert d1.full_name == "Ramesh Kumar" and d1.pan == "AAAAA1111A"
        assert d2.full_name == "Sita Devi" and d2.pan == "BBBBB2222B"

    def test_shared_phone_different_name_blank_pan_does_not_inherit_pan(self, app):
        """Reported flaw #2: a second family member donates through the same
        shared phone but doesn't fill in a PAN. The new donor record must
        NOT inherit the first person's PAN -- that would attach a stranger's
        tax ID to the wrong name on their receipt."""
        from public import find_or_create_donor

        d1 = find_or_create_donor({"full_name": "Ramesh Kumar", "phone": "9123450002", "pan": "AAAAA1111A"})
        db.session.commit()

        d2 = find_or_create_donor({"full_name": "Amit Sharma", "phone": "9123450002"})
        db.session.commit()

        assert d1.id != d2.id
        assert d2.pan is None
        assert d1.pan == "AAAAA1111A"  # original donor's PAN is untouched too

    def test_blank_field_on_later_donation_does_not_erase_saved_value(self, app):
        """The flip side of the above -- if this donation's form just didn't
        ask for/collect a field (e.g. address left blank), the previously
        saved value should survive rather than being wiped to blank."""
        from public import find_or_create_donor

        d1 = find_or_create_donor({
            "full_name": "Gopal Das", "phone": "9234567890", "email": "gopal@example.com", "city": "Mumbai",
        })
        db.session.commit()

        d2 = find_or_create_donor({"full_name": "Gopal Das", "phone": "9234567890", "email": "", "city": ""})
        db.session.commit()

        assert d1.id == d2.id
        assert d2.email == "gopal@example.com"
        assert d2.city == "Mumbai"

    def test_different_donors_stay_separate(self, app):
        from public import find_or_create_donor

        d1 = find_or_create_donor({"full_name": "Person A", "phone": "9000000001"})
        d2 = find_or_create_donor({"full_name": "Person B", "phone": "9000000002"})
        db.session.commit()

        assert d1.id != d2.id
        assert Donor.query.count() == 2

    def test_whatsapp_number_stored_separately_from_phone(self, app):
        from public import find_or_create_donor

        d1 = find_or_create_donor({
            "full_name": "Nitai Das", "phone": "9444444444", "whatsapp_number": "9555555555",
        })
        db.session.commit()

        assert d1.phone == "9444444444"
        assert d1.whatsapp_number == "9555555555"
        assert d1.whatsapp_or_phone == "9555555555"

    def test_whatsapp_or_phone_falls_back_to_phone(self, app):
        from public import find_or_create_donor

        d1 = find_or_create_donor({"full_name": "Gauranga Das", "phone": "9666666666"})
        db.session.commit()

        assert d1.whatsapp_number is None
        assert d1.whatsapp_or_phone == "9666666666"

    def test_second_donation_same_phone_different_format_reuses_donor(self, app):
        """The exact bug report this test guards against: a donor typing
        their number as "+91 88020 81265" on one donation and plain
        "8802081265" on another must still be recognised as the same
        person, not silently split into two donor records (and, outside
        this test, not silently fail donor OTP login either -- see
        donor_portal.py, which normalizes the same way)."""
        from public import find_or_create_donor

        d1 = find_or_create_donor({"full_name": "Meera Bai", "phone": "8802081265"})
        db.session.commit()

        d2 = find_or_create_donor({"full_name": "Meera Bai", "phone": "+91 88020 81265"})
        db.session.commit()

        assert d1.id == d2.id
        assert Donor.query.count() == 1
        assert d2.phone == "8802081265"

    def test_second_donation_same_phone_space_grouped_no_country_code_reuses_donor(self, app):
        """Covers the other common way this number gets typed/pasted --
        space-grouped as "88020 81265" with no +91/91 prefix at all (e.g.
        copied straight off a business card or WhatsApp profile). Digits
        get stripped regardless of position, so this needs no separate
        country-code handling in normalize_phone() -- just confirming it
        actually behaves that way."""
        from public import find_or_create_donor

        d1 = find_or_create_donor({"full_name": "Meera Bai", "phone": "8802081265"})
        db.session.commit()

        d2 = find_or_create_donor({"full_name": "Meera Bai", "phone": "88020 81265"})
        db.session.commit()

        assert d1.id == d2.id
        assert Donor.query.count() == 1
        assert d2.phone == "8802081265"

    def test_whatsapp_number_normalized_regardless_of_format(self, app):
        from public import find_or_create_donor

        d1 = find_or_create_donor({
            "full_name": "Shyam Sundar", "phone": "9777777777", "whatsapp_number": "+91 98888 88888",
        })
        db.session.commit()

        assert d1.whatsapp_number == "9888888888"


class TestReceiptNumbering:
    def test_sequential_global_counter(self, app):
        import datetime

        date = datetime.date(2026, 7, 28)
        num1, fy1 = ReceiptCounter.next_receipt_number(is_80g=True, date=date)
        num2, fy2 = ReceiptCounter.next_receipt_number(is_80g=True, date=date)
        db.session.commit()

        assert fy1 == fy2 == "2026-27"
        assert num1 == "032511/ISK500000"
        assert num2 == "032511/ISK500001"

    def test_80g_and_non80g_share_one_sequence(self, app):
        import datetime

        date = datetime.date(2026, 7, 28)
        num_80g, _ = ReceiptCounter.next_receipt_number(is_80g=True, date=date)
        num_gen, _ = ReceiptCounter.next_receipt_number(is_80g=False, date=date)
        db.session.commit()

        # Same running sequence regardless of 80G status -- the temple's
        # numbering scheme doesn't split by series.
        assert num_80g == "032511/ISK500000"
        assert num_gen == "032511/ISK500001"

    def test_numbering_does_not_reset_across_financial_years(self, app):
        import datetime

        march = datetime.date(2026, 3, 31)   # FY 2025-26
        april = datetime.date(2026, 4, 1)     # FY 2026-27

        num1, fy1 = ReceiptCounter.next_receipt_number(is_80g=True, date=march)
        num2, fy2 = ReceiptCounter.next_receipt_number(is_80g=True, date=april)
        db.session.commit()

        # financial_year is still tracked per-donation (for annual
        # statements / Form 10BD), but the receipt number itself keeps
        # counting up across the FY boundary rather than resetting.
        assert fy1 == "2025-26"
        assert fy2 == "2026-27"
        assert num1 == "032511/ISK500000"
        assert num2 == "032511/ISK500001"


class TestConsent:
    """Consent used to be gate-checked at submission time and then thrown
    away -- nothing was ever persisted. Now every online donation records
    that consent was given, when, and which wording of the checkbox."""

    def test_online_donation_records_consent(self, app, client):
        from models import Campaign, Donation

        campaign = Campaign.query.filter_by(name="Annadan").first()
        order_resp = client.post(
            "/api/create-order",
            json={
                "campaign_id": campaign.id,
                "amount": 251,
                "full_name": "Consent Test Donor",
                "phone": "9777777777",
                "consent": "on",
            },
        )
        donation_id = order_resp.get_json()["donation_id"]
        donation = Donation.query.get(donation_id)

        assert donation.consent_given is True
        assert donation.consent_at is not None
        assert donation.consent_version == app.config["CONSENT_VERSION"]

    def test_missing_consent_rejected_before_any_donation_is_created(self, app, client):
        from models import Campaign, Donation

        campaign = Campaign.query.filter_by(name="Annadan").first()
        count_before = Donation.query.count()

        resp = client.post(
            "/api/create-order",
            json={
                "campaign_id": campaign.id,
                "amount": 251,
                "full_name": "No Consent Donor",
                "phone": "9888888888",
            },
        )

        assert resp.status_code == 400
        assert Donation.query.count() == count_before
