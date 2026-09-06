"""Tests for zoho_sync -- entries in, donations out.

Every entry fixture here is modelled on a real one from this account, as
sent through in the project history: the "last, first" name rendering, the
"Txn ID : pay_X Order ID : order_Y" transaction string, "Payment Amount"
vs "Total Amount" differing between forms, and Zoho's own Payment Status
being wrong in both directions.

The rules that earlier failures put in place are asserted directly, since
the point of the rewrite was to keep them:

  * Razorpay decides whether money arrived, not Zoho's status field.
  * A receipt needs a genuine pay_ id AND a confirmed capture.
  * Idempotency covers both id columns, so a hand-entered backfill isn't
    re-imported as a duplicate.
  * One bad entry never strands the others.
  * Nothing is dropped silently.

And the thing the rewrite exists to fix: two entries from one donor for
the same amount produce two receipts, where the old phone-and-amount
matching could only refuse and ask a human.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import zoho_sync


def entry(**overrides):
    """A realistic entry, in the shape this account's forms produce."""
    base = {
        "Name": "raj, shivam",
        "Phone": "+919319880507",
        "Gender": "Male",
        "Age": "27",
        "Mode of Payment": "Online (UPI)",
        "Payment Amount": "100.00",
        "Payment Status": "Completed",
        "Payment Currency": "INR",
        "Added Time": "04-Sep-2026 21:55:22",
        "Payment Merchant": "Razor Pay",
        "Payment Transaction ID": "Txn ID : pay_TY1ckLpy6lUDMr Order ID : order_TY1bS1x9eSe5tb",
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def _captured(payment_id):
    return True, None


def _never_captured(payment_id):
    return False, None


def _unreachable(payment_id):
    return False, "connection reset"


class _FakeDonation:
    def __init__(self, payment_id):
        self.id = abs(hash(payment_id)) % 10000
        self.receipt_number = f"032511/ISK{self.id:06d}"
        self.razorpay_payment_id = payment_id


def _creator(recorder):
    def create(payload, campaign, payment_id, order_id):
        recorder.append({
            "payload": payload, "campaign": campaign,
            "payment_id": payment_id, "order_id": order_id,
        })
        return _FakeDonation(payment_id), None
    return create


class TestFieldExtraction:
    def test_it_finds_the_transaction_id_inside_zohos_combined_string(self):
        """Production renders this as "Txn ID : pay_X Order ID : order_Y"
        in some places and a bare id in others."""
        pid, oid = zoho_sync.payment_ids(entry())
        assert pid == "pay_TY1ckLpy6lUDMr"
        assert oid == "order_TY1bS1x9eSe5tb"

    def test_it_finds_a_bare_transaction_id_too(self):
        pid, oid = zoho_sync.payment_ids(entry(**{"Payment Transaction ID": "pay_Bare1"}))
        assert pid == "pay_Bare1" and oid is None

    def test_it_finds_the_id_even_in_a_differently_named_field(self):
        """Scanning every value rather than one named field means a form
        that relabels this can't silently import nothing."""
        e = {"Some Odd Label": "ref pay_Relabelled9 here", "Phone": "9876543210"}
        assert zoho_sync.payment_ids(e)[0] == "pay_Relabelled9"

    def test_no_transaction_id_is_reported_as_absent_not_invented(self):
        assert zoho_sync.payment_ids(entry(**{"Payment Transaction ID": ""})) == (None, None)

    @pytest.mark.parametrize("raw,expected", [
        # Every one of these is a real entry from this account.
        ("shivam, raj", "Shivam Raj"),
        ("Jatin, saini", "Jatin Saini"),
        ("Neeraj, Kumar", "Neeraj Kumar"),
        ("Anurag, Awasthi", "Anurag Awasthi"),
        ("Pinki, Bisht", "Pinki Bisht"),
    ])
    def test_zohos_composite_name_reads_first_then_last(self, raw, expected):
        """Confirmed against this account's own entries. The natural
        assumption is "Last, First" -- as most software renders a sorted
        name -- and that reading would put "Saini Jatin" on a receipt. It
        was very nearly written that way, and an earlier version of this
        test hedged with an `or` that let the wrong order pass."""
        assert zoho_sync.donor_name({"Name": raw}) == expected

    def test_a_plain_name_is_left_alone(self):
        assert zoho_sync.donor_name(entry(**{"Name": "Ruby Kumari"})) == "Ruby Kumari"

    def test_capitalisation_is_only_applied_where_it_is_missing(self):
        """Donors type "shivam raj" as often as "Shivam Raj", but names
        that are already cased must survive intact."""
        assert zoho_sync.donor_name({"Name": "Ravi McDonald"}) == "Ravi McDonald"
        assert zoho_sync.donor_name({"Name": "A K Sharma"}) == "A K Sharma"

    def test_payment_amount_wins_over_a_generic_amount_field(self):
        """Some forms carry both "Total Amount" and "Payment Amount", and
        they can differ -- the one actually charged is what matters."""
        e = entry(**{"Total Amount": "500", "Payment Amount": "100.00"})
        assert zoho_sync.field(e, "amount") == "100.00"

    def test_a_form_using_only_total_amount_still_works(self):
        e = entry(**{"Payment Amount": None, "Total Amount": "250"})
        assert zoho_sync.field(e, "amount") == "250"

    def test_field_names_match_regardless_of_spelling(self):
        for label in ("Phone", "phone_number", "Phone No.", "Mobile"):
            assert zoho_sync.field({label: "9811100011"}, "phone") == "9811100011"

    def test_a_missing_optional_field_is_empty_not_an_error(self):
        assert zoho_sync.field(entry(), "pan") == ""

    def test_zoho_timestamps_parse(self):
        assert zoho_sync.parse_added_time(entry()).year == 2026

    def test_an_unparseable_timestamp_returns_none_rather_than_guessing(self):
        assert zoho_sync.parse_added_time(entry(**{"Added Time": "whenever"})) is None


class TestTheCaseTheOldDesignCouldNotHandle:
    def test_two_entries_from_one_donor_produce_two_receipts(self, app):
        """The Jatin case. Two submissions, same phone, same amount -- the
        old phone-and-amount matching had one submission chasing two
        payments and could only refuse and ask a human. Each entry now
        carries its own transaction id, so the join is exact."""
        with app.app_context():
            made = []
            summary = zoho_sync.sync_entries(
                app.config,
                [
                    entry(**{"Name": "saini, jatin", "Phone": "+919650150283",
                             "Payment Transaction ID": "pay_TYkDaxITUonK1O"}),
                    entry(**{"Name": "saini, jatin", "Phone": "+919650150283",
                             "Payment Transaction ID": "pay_TYkwQc4pqV9HNu"}),
                ],
                campaign=object(), form_name="EBG",
                verify_captured=_captured, create_donation=_creator(made),
            )

        assert len(summary["created"]) == 2
        assert {m["payment_id"] for m in made} == {"pay_TYkDaxITUonK1O", "pay_TYkwQc4pqV9HNu"}


class TestRazorpayRemainsTheAuthority:
    def test_zohos_completed_status_alone_does_not_produce_a_receipt(self, app):
        """Production had entries reading "Completed" against payments
        Razorpay had not captured."""
        with app.app_context():
            made = []
            summary = zoho_sync.sync_entries(
                app.config, [entry()], campaign=object(), form_name="EBG",
                verify_captured=_never_captured, create_donation=_creator(made),
            )
        assert summary["created"] == [] and made == []
        assert len(summary["not_captured"]) == 1

    def test_an_entry_zoho_calls_processing_is_still_receipted_when_captured(self, app):
        """And the reverse: Zoho's status is advisory, so a stale
        "processing" must not block a payment Razorpay confirms."""
        with app.app_context():
            made = []
            summary = zoho_sync.sync_entries(
                app.config, [entry(**{"Payment Status": "processing"})],
                campaign=object(), form_name="EBG",
                verify_captured=_captured, create_donation=_creator(made),
            )
        assert len(summary["created"]) == 1

    def test_an_unreachable_razorpay_is_reported_not_treated_as_no_payment(self, app):
        with app.app_context():
            summary = zoho_sync.sync_entries(
                app.config, [entry()], campaign=object(), form_name="EBG",
                verify_captured=_unreachable, create_donation=_creator([]),
            )
        assert summary["created"] == []
        assert "connection reset" in summary["not_captured"][0]["reason"]

    def test_an_entry_with_no_payment_is_an_ordinary_skip(self, app):
        with app.app_context():
            summary = zoho_sync.sync_entries(
                app.config, [entry(**{"Payment Transaction ID": ""})],
                campaign=object(), form_name="EBG",
                verify_captured=_captured, create_donation=_creator([]),
            )
        assert summary["skipped_no_payment"] == 1 and summary["failed"] == []


class TestIdempotency:
    def _record_existing(self, app, **kwargs):
        from extensions import db
        from models import Campaign, Donation, Donor
        donor = Donor(full_name="Existing", phone="9319880507")
        db.session.add(donor)
        db.session.flush()
        db.session.add(Donation(
            donor_id=donor.id, campaign_id=Campaign.query.first().id, amount=100,
            status="success", payment_mode="online", **kwargs,
        ))
        db.session.commit()

    def test_a_payment_already_recorded_is_not_imported_again(self, app):
        with app.app_context():
            self._record_existing(app, razorpay_payment_id="pay_TY1ckLpy6lUDMr")
            summary = zoho_sync.sync_entries(
                app.config, [entry()], campaign=object(), form_name="EBG",
                verify_captured=_captured, create_donation=_creator([]),
            )
        assert summary["already_recorded"] == 1 and summary["created"] == []

    def test_a_hand_entered_backfill_is_not_imported_again(self, app):
        """Staff backfilling put the reference in bank_transaction_id.
        Checking only razorpay_payment_id would re-import all 21 of
        September's backfilled donations as duplicate receipts."""
        with app.app_context():
            self._record_existing(app, bank_transaction_id="pay_TY1ckLpy6lUDMr")
            summary = zoho_sync.sync_entries(
                app.config, [entry()], campaign=object(), form_name="EBG",
                verify_captured=_captured, create_donation=_creator([]),
            )
        assert summary["already_recorded"] == 1 and summary["created"] == []


class TestResilience:
    def test_one_bad_entry_does_not_strand_the_others(self, app):
        def flaky(payload, campaign, payment_id, order_id):
            if payment_id == "pay_Bad1":
                raise RuntimeError("something unexpected")
            return _FakeDonation(payment_id), None

        with app.app_context():
            summary = zoho_sync.sync_entries(
                app.config,
                [
                    entry(**{"Payment Transaction ID": "pay_Bad1"}),
                    entry(**{"Payment Transaction ID": "pay_Good1", "Phone": "9811100022"}),
                ],
                campaign=object(), form_name="EBG",
                verify_captured=_captured, create_donation=flaky,
            )

        assert len(summary["created"]) == 1
        assert summary["created"][0].razorpay_payment_id == "pay_Good1"
        assert summary["failed"][0]["entry"] == "pay_Bad1"

    def test_a_validation_failure_is_reported_with_its_reason(self, app):
        def refuses(payload, campaign, payment_id, order_id):
            return None, ({"error": "A PAN is required to issue an 80G tax receipt"}, 400)

        with app.app_context():
            summary = zoho_sync.sync_entries(
                app.config, [entry()], campaign=object(), form_name="EBG",
                verify_captured=_captured, create_donation=refuses,
            )
        assert summary["created"] == []
        assert "PAN" in summary["failed"][0]["error"]

    def test_work_per_run_is_bounded(self, app):
        """Runs inside an HTTP request under gunicorn's worker timeout --
        the failure this codebase has already hit twice."""
        entries = [
            entry(**{"Payment Transaction ID": f"pay_Bulk{i}", "Phone": f"98111000{i:02d}"})
            for i in range(10)
        ]
        with app.app_context():
            summary = zoho_sync.sync_entries(
                app.config, entries, campaign=object(), form_name="EBG",
                verify_captured=_captured, create_donation=_creator([]),
                max_per_run=3,
            )
        assert len(summary["created"]) == 3
        assert "next run" in summary["failed"][0]["error"]

    def test_every_entry_lands_in_exactly_one_bucket(self, app):
        """A count that doesn't add up means an entry went nowhere, which
        is the silent loss this work exists to remove."""
        entries = [
            entry(),                                                    # created
            entry(**{"Payment Transaction ID": ""}),                    # no payment
            entry(**{"Payment Transaction ID": "pay_NotCaptured1"}),    # not captured
        ]

        def selective(payment_id):
            return (payment_id != "pay_NotCaptured1"), None

        with app.app_context():
            summary = zoho_sync.sync_entries(
                app.config, entries, campaign=object(), form_name="EBG",
                verify_captured=selective, create_donation=_creator([]),
            )

        accounted = (
            len(summary["created"]) + summary["skipped_no_payment"]
            + summary["already_recorded"] + len(summary["not_captured"])
            + len(summary["failed"])
        )
        assert accounted == len(entries)
