"""Tests for bace_tracker.py -- pure functions, no database, no app
context. Mirrors the spreadsheet's SUMIFS-per-cell logic this replaces:
Paid if the sum of that student's payments for that month is >= their
monthly amount, Partial if some but not enough, Pending if none -- unless
the month is before they joined (N/A) or hasn't arrived yet (Upcoming),
which take priority over all of that.
"""
import datetime
from decimal import Decimal

import pytest

import bace_tracker as bt


def _d(year, month, day=1):
    return datetime.date(year, month, day)


class _Student:
    def __init__(self, id, monthly_amount, joined_month):
        self.id = id
        self.monthly_amount = monthly_amount
        self.joined_month = joined_month


class _Payment:
    def __init__(self, student_id, for_month, amount_paid):
        self.student_id = student_id
        self.for_month = for_month
        self.amount_paid = amount_paid


class TestMonthMath:
    def test_month_start_drops_the_day(self):
        assert bt.month_start(_d(2026, 9, 17)) == _d(2026, 9, 1)

    def test_add_months_rolls_over_the_year(self):
        assert bt.add_months(_d(2026, 11, 1), 2) == _d(2027, 1, 1)
        assert bt.add_months(_d(2026, 1, 1), -1) == _d(2025, 12, 1)

    def test_months_between_is_inclusive_both_ends(self):
        assert bt.months_between(_d(2026, 1, 1), _d(2026, 3, 1)) == [
            _d(2026, 1, 1), _d(2026, 2, 1), _d(2026, 3, 1),
        ]

    def test_months_between_empty_when_start_after_end(self):
        assert bt.months_between(_d(2026, 3, 1), _d(2026, 1, 1)) == []


class TestStatusFor:
    def test_before_joining_is_na(self):
        assert bt.status_for(3000, _d(2026, 6, 1), _d(2026, 5, 1), 0, today=_d(2026, 9, 10)) == bt.STATUS_NA

    def test_after_this_month_is_upcoming(self):
        assert bt.status_for(3000, _d(2026, 1, 1), _d(2026, 12, 1), 0, today=_d(2026, 9, 10)) == bt.STATUS_UPCOMING

    def test_full_payment_is_paid(self):
        assert bt.status_for(3000, _d(2026, 1, 1), _d(2026, 9, 1), 3000, today=_d(2026, 9, 10)) == bt.STATUS_PAID

    def test_overpayment_is_still_paid(self):
        assert bt.status_for(3000, _d(2026, 1, 1), _d(2026, 9, 1), 3500, today=_d(2026, 9, 10)) == bt.STATUS_PAID

    def test_partial_payment_is_partial(self):
        assert bt.status_for(3000, _d(2026, 1, 1), _d(2026, 9, 1), 1000, today=_d(2026, 9, 10)) == bt.STATUS_PARTIAL

    def test_no_payment_is_pending(self):
        assert bt.status_for(3000, _d(2026, 1, 1), _d(2026, 9, 1), 0, today=_d(2026, 9, 10)) == bt.STATUS_PENDING

    def test_the_joining_month_itself_is_due_not_na(self):
        assert bt.status_for(3000, _d(2026, 9, 1), _d(2026, 9, 1), 0, today=_d(2026, 9, 10)) == bt.STATUS_PENDING

    def test_the_current_month_itself_is_due_not_upcoming(self):
        assert bt.status_for(3000, _d(2026, 1, 1), _d(2026, 9, 1), 0, today=_d(2026, 9, 10)) == bt.STATUS_PENDING


class TestBuildRentSummary:
    def test_a_student_fully_paid_this_month_owes_nothing(self):
        student = _Student(1, 3000, _d(2026, 9, 1))
        payments = [_Payment(1, _d(2026, 9, 1), 3000)]
        months = bt.months_between(_d(2026, 9, 1), _d(2026, 9, 1))

        [row] = bt.build_rent_summary([student], payments, months, today=_d(2026, 9, 10))

        assert row["this_month_status"] == bt.STATUS_PAID
        assert row["this_month_due"] == 0
        assert row["balance_due"] == 0
        assert row["months_behind"] == 0
        assert row["total_paid"] == 3000

    def test_two_months_never_paid_are_both_behind(self):
        """Joined Aug, nothing paid, today is in September -- Aug and Sep
        are both due and unpaid, so balance_due is 2x the monthly amount
        and months_behind is 2, even though `months` here only asks for
        one month's grid column."""
        student = _Student(1, 3000, _d(2026, 8, 1))
        months = bt.months_between(_d(2026, 9, 1), _d(2026, 9, 1))

        [row] = bt.build_rent_summary([student], [], months, today=_d(2026, 9, 10))

        assert row["statuses"][_d(2026, 9, 1)] == bt.STATUS_PENDING
        assert row["balance_due"] == 6000
        assert row["months_behind"] == 2
        assert row["this_month_due"] == 3000

    def test_a_partial_payment_leaves_the_remainder_as_balance_due(self):
        student = _Student(1, 3000, _d(2026, 9, 1))
        payments = [_Payment(1, _d(2026, 9, 1), 1200)]
        months = bt.months_between(_d(2026, 9, 1), _d(2026, 9, 1))

        [row] = bt.build_rent_summary([student], payments, months, today=_d(2026, 9, 10))

        assert row["this_month_status"] == bt.STATUS_PARTIAL
        assert row["this_month_due"] == 1800
        assert row["balance_due"] == 1800
        assert row["months_behind"] == 1

    def test_a_month_before_joining_never_counts_toward_balance_due(self):
        student = _Student(1, 3000, _d(2026, 9, 1))
        months = bt.months_between(_d(2026, 1, 1), _d(2026, 9, 1))

        [row] = bt.build_rent_summary([student], [], months, today=_d(2026, 9, 10))

        for month in bt.months_between(_d(2026, 1, 1), _d(2026, 8, 1)):
            assert row["statuses"][month] == bt.STATUS_NA
        assert row["balance_due"] == 3000
        assert row["months_behind"] == 1

    def test_an_upcoming_month_never_counts_toward_balance_due(self):
        student = _Student(1, 3000, _d(2026, 9, 1))
        months = bt.months_between(_d(2026, 9, 1), _d(2026, 12, 1))

        [row] = bt.build_rent_summary([student], [], months, today=_d(2026, 9, 10))

        assert row["statuses"][_d(2026, 12, 1)] == bt.STATUS_UPCOMING
        # Only Sep (the current month) is due and unpaid -- Oct/Nov/Dec
        # are Upcoming and must not inflate balance_due.
        assert row["balance_due"] == 3000
        assert row["months_behind"] == 1

    def test_multiple_payments_in_one_month_are_summed(self):
        student = _Student(1, 3000, _d(2026, 9, 1))
        payments = [
            _Payment(1, _d(2026, 9, 1), 1000),
            _Payment(1, _d(2026, 9, 1), 2000),
        ]
        months = bt.months_between(_d(2026, 9, 1), _d(2026, 9, 1))

        [row] = bt.build_rent_summary([student], payments, months, today=_d(2026, 9, 10))

        assert row["this_month_status"] == bt.STATUS_PAID
        assert row["total_paid"] == 3000

    def test_payments_for_a_different_student_never_cross_over(self):
        s1 = _Student(1, 3000, _d(2026, 9, 1))
        s2 = _Student(2, 3000, _d(2026, 9, 1))
        payments = [_Payment(2, _d(2026, 9, 1), 3000)]
        months = bt.months_between(_d(2026, 9, 1), _d(2026, 9, 1))

        rows = bt.build_rent_summary([s1, s2], payments, months, today=_d(2026, 9, 10))

        assert rows[0]["this_month_status"] == bt.STATUS_PENDING
        assert rows[1]["this_month_status"] == bt.STATUS_PAID
