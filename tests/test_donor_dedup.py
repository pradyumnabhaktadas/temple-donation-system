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
