"""Tests for check_payment_duplicates.

This script's answer decides whether a unique constraint gets added to a
production table during a deploy. A false "clean" means the migration
fails partway through a release; a false alarm means someone goes looking
through tax documents for a problem that isn't there. Both are worth
testing for.

The cross-column case is the one that has actually caught us out:
pay_TUoekY3BLSiZXp was entered by hand, which records the gateway id in
bank_transaction_id, and an earlier version of the reconciliation report
only looked at razorpay_payment_id and declared the money missing.
"""
import os
import sys
import types

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from check_payment_duplicates import find_duplicates


def _d(id, razorpay=None, bank=None):
    """A donation-shaped stand-in. find_duplicates reads attributes, so a
    namespace is enough and keeps these tests off the database."""
    return types.SimpleNamespace(
        id=id, razorpay_payment_id=razorpay, bank_transaction_id=bank,
        amount=100, receipt_number=f"R{id}", status="success",
        payment_mode="online", donation_date=None,
    )


class TestFindingRealDuplicates:
    def test_the_same_gateway_id_on_two_donations_is_found(self):
        dupes, _ = find_duplicates([_d(1, razorpay="pay_X"), _d(2, razorpay="pay_X")])
        assert list(dupes) == ["pay_X"]
        assert [d.id for d in dupes["pay_X"]] == [1, 2]

    def test_a_hand_entered_backfill_colliding_with_a_gateway_row_is_found(self):
        """The cross-column case. An offline entry records the gateway id
        in bank_transaction_id, so checking one column at a time misses
        exactly the duplicate that has already happened here."""
        dupes, _ = find_duplicates([
            _d(1, razorpay="pay_TUoekY3BLSiZXp"),
            _d(2, bank="pay_TUoekY3BLSiZXp"),
        ])
        assert list(dupes) == ["pay_TUoekY3BLSiZXp"]

    def test_two_offline_entries_sharing_a_reference_are_found(self):
        dupes, _ = find_duplicates([_d(1, bank="UTR12345"), _d(2, bank="UTR12345")])
        assert list(dupes) == ["UTR12345"]

    def test_three_donations_on_one_payment_are_grouped_together(self):
        dupes, _ = find_duplicates([_d(i, razorpay="pay_X") for i in (1, 2, 3)])
        assert len(dupes["pay_X"]) == 3

    def test_several_separate_duplicates_are_all_reported(self):
        dupes, _ = find_duplicates([
            _d(1, razorpay="pay_A"), _d(2, razorpay="pay_A"),
            _d(3, razorpay="pay_B"), _d(4, bank="pay_B"),
            _d(5, razorpay="pay_C"),
        ])
        assert sorted(dupes) == ["pay_A", "pay_B"]


class TestNotCryingWolf:
    """A false alarm sends someone auditing tax documents for nothing."""

    def test_distinct_payments_are_clean(self):
        dupes, _ = find_duplicates([_d(1, razorpay="pay_A"), _d(2, razorpay="pay_B")])
        assert dupes == {}

    def test_one_donation_carrying_the_id_in_both_columns_is_not_a_duplicate(self):
        """A single donation with the gateway id mirrored into the
        reference field is one payment, recorded once."""
        dupes, _ = find_duplicates([_d(1, razorpay="pay_X", bank="pay_X")])
        assert dupes == {}

    def test_empty_and_missing_ids_never_collide(self):
        """Most offline donations have neither id. NULLs are distinct in
        both Postgres and SQLite, and the check must agree."""
        dupes, _ = find_duplicates([
            _d(1), _d(2), _d(3, razorpay=""), _d(4, bank=""), _d(5, razorpay=None),
        ])
        assert dupes == {}

    def test_whitespace_only_values_are_treated_as_absent(self):
        dupes, _ = find_duplicates([_d(1, bank="   "), _d(2, bank="  ")])
        assert dupes == {}

    def test_surrounding_whitespace_does_not_hide_a_duplicate(self):
        """A hand-typed reference with a trailing space is the same
        payment, and the constraint would see it as different -- so the
        check must flag it rather than let the migration decide."""
        dupes, _ = find_duplicates([_d(1, razorpay="pay_X"), _d(2, bank=" pay_X ")])
        assert list(dupes) == ["pay_X"]

    def test_an_empty_database_is_clean(self):
        assert find_duplicates([]) == ({}, 0)


class TestTheSimulatedPlaceholder:
    """/donate/simulate sets this literal on every demo donation, so it is
    legitimately repeated. A plain unique index would have failed on it --
    which is why the constraint excludes it, and why this check must too."""

    def test_repeated_simulated_rows_are_not_duplicates(self):
        dupes, count = find_duplicates([_d(i, razorpay="SIMULATED") for i in (1, 2, 3)])
        assert dupes == {}
        assert count == 3

    def test_they_are_counted_so_they_can_be_mentioned(self):
        _dupes, count = find_duplicates([_d(1, razorpay="SIMULATED"), _d(2, razorpay="pay_A")])
        assert count == 1

    def test_a_real_duplicate_alongside_simulated_rows_is_still_found(self):
        dupes, count = find_duplicates([
            _d(1, razorpay="SIMULATED"), _d(2, razorpay="SIMULATED"),
            _d(3, razorpay="pay_X"), _d(4, razorpay="pay_X"),
        ])
        assert list(dupes) == ["pay_X"] and count == 2


class TestAgainstTheRealTable:
    """The pure function above is where the logic lives, but it reads
    attributes off whatever it's given -- so one pass over real Donation
    rows confirms the column names haven't drifted."""

    def test_it_reads_the_columns_that_actually_exist(self, client, app):
        from extensions import db
        from models import Campaign, Donation, Donor

        donor = Donor(full_name="Test Donor", phone="9000000001")
        db.session.add(donor)
        db.session.flush()
        campaign_id = Campaign.query.first().id
        db.session.add(Donation(donor_id=donor.id, campaign_id=campaign_id, amount=100,
                                status="success", payment_mode="online",
                                razorpay_payment_id="pay_Dup1"))
        db.session.add(Donation(donor_id=donor.id, campaign_id=campaign_id, amount=100,
                                status="success", payment_mode="offline",
                                bank_transaction_id="pay_Dup1"))
        db.session.add(Donation(donor_id=donor.id, campaign_id=campaign_id, amount=100,
                                status="success", payment_mode="online",
                                razorpay_payment_id="pay_Unique1"))
        db.session.commit()

        dupes, _ = find_duplicates(Donation.query.order_by(Donation.id).all())

        assert list(dupes) == ["pay_Dup1"]
        assert len(dupes["pay_Dup1"]) == 2

    def test_a_clean_table_reports_clean(self, client, app):
        from extensions import db
        from models import Campaign, Donation, Donor

        donor = Donor(full_name="Test Donor", phone="9000000002")
        db.session.add(donor)
        db.session.flush()
        db.session.add(Donation(donor_id=donor.id, campaign_id=Campaign.query.first().id,
                                amount=100, status="success", payment_mode="online",
                                razorpay_payment_id="pay_Only1"))
        db.session.commit()

        assert find_duplicates(Donation.query.all())[0] == {}
