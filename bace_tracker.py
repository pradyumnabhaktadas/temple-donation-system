"""Shared rent-status computation for the BACE Rent Contribution Tracker
admin pages.

BaceStudent (roster) and BaceRentPayment (every payment logged) are the
only two tables anyone types into -- see their docstrings in models.py.
Everything else (the Tracker grid, the Dashboard's totals, the Pending
List) is *computed* from those two, the same way the spreadsheet this
replaces computed its Tracker/Dashboard/Pending List tabs from its
Students/Payments Log tabs with SUMIFS formulas. This module is that
computation, done once in Python instead of once per cell in Excel, and
kept as pure functions (no Flask, no querying) so it's trivial to test
without a database or a request context.

One student's status for one month: sum every payment recorded for that
student in that month, compare against their monthly_amount. >= is Paid,
some but not enough is Partial, none is Pending -- unless the month is
before they joined (N/A) or hasn't arrived yet (Upcoming), which take
priority over all of that.
"""
import datetime
from decimal import Decimal

STATUS_NA = "N/A"
STATUS_UPCOMING = "Upcoming"
STATUS_PAID = "Paid"
STATUS_PARTIAL = "Partial"
STATUS_PENDING = "Pending"

# What counts as "still owed" for the Dashboard / Pending List -- N/A and
# Upcoming are never owed (too early either way), Paid is settled.
OUTSTANDING_STATUSES = (STATUS_PARTIAL, STATUS_PENDING)


def month_start(d):
    """The 1st of the month containing date/datetime `d`. Every "month"
    value in this feature -- joined_month, for_month, "this month" -- is
    normalized to this, so every comparison is date == date, never a
    fuzzy year/month pair that has to be unpacked separately."""
    return datetime.date(d.year, d.month, 1)


def add_months(d, n):
    """month_start(d) shifted by n calendar months. n may be negative."""
    month_index = d.month - 1 + n
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    return datetime.date(year, month, 1)


def months_between(start, end):
    """Every month_start from `start` to `end`, inclusive, ascending.
    Empty if start is after end."""
    months = []
    cur = month_start(start)
    stop = month_start(end)
    while cur <= stop:
        months.append(cur)
        cur = add_months(cur, 1)
    return months


def payments_by_student_month(payments):
    """{student_id: {month_start_date: Decimal total paid}} from a flat
    list of BaceRentPayment rows (or anything with .student_id, .for_month,
    .amount_paid) -- one pass, reused by every student/month lookup below
    instead of summing per-cell like the spreadsheet's SUMIFS does."""
    totals = {}
    for p in payments:
        by_month = totals.setdefault(p.student_id, {})
        month = month_start(p.for_month)
        by_month[month] = by_month.get(month, Decimal("0")) + Decimal(p.amount_paid or 0)
    return totals


def status_for(monthly_amount, joined_month, month, paid, today=None):
    """Paid/Partial/Pending/Upcoming/N/A for one student-month. `paid` is
    the Decimal total already paid for that exact month (from
    payments_by_student_month) -- 0 if nothing was found."""
    today = today or datetime.date.today()
    month = month_start(month)
    if month < month_start(joined_month):
        return STATUS_NA
    if month > month_start(today):
        return STATUS_UPCOMING
    paid = Decimal(paid or 0)
    if paid >= Decimal(monthly_amount):
        return STATUS_PAID
    if paid > 0:
        return STATUS_PARTIAL
    return STATUS_PENDING


def build_rent_summary(students, payments, months, today=None):
    """The whole tracker, computed once: a list of dicts, one per student
    in `students` order --

      student, statuses ({month: status} for every month in `months`),
      total_paid, balance_due (sum of what's still short for every due
      month from joined_month through this month -- not just the months
      in `months`, so a narrow grid view still reports the true balance),
      months_behind (count of Partial/Pending months up to and including
      this month), this_month_status, this_month_due (0 once Paid).

    `payments` is the flat list for every student shown -- callers query
    once (BaceRentPayment.query.filter(student_id.in_(...))) and pass the
    rows in, keeping this a pure function with no database access of its
    own, the same reasoning unreconciled_razorpay_payments' callers
    follow for Razorpay."""
    today = today or datetime.date.today()
    this_month = month_start(today)
    by_student = payments_by_student_month(payments)

    summary = []
    for student in students:
        paid_by_month = by_student.get(student.id, {})
        joined = month_start(student.joined_month)
        monthly_amount = Decimal(student.monthly_amount)

        statuses = {}
        for month in months:
            statuses[month] = status_for(
                monthly_amount, joined, month, paid_by_month.get(month_start(month)), today,
            )

        # Balance due / months behind: every month from joined through
        # this month, regardless of whether it's in the visible `months`
        # window -- a Tracker filtered to "just this quarter" must not
        # under-report what a student actually owes.
        total_paid = sum(paid_by_month.values(), Decimal("0"))
        balance_due = Decimal("0")
        months_behind = 0
        this_month_status = STATUS_NA
        this_month_due = Decimal("0")
        if joined <= this_month:
            for month in months_between(joined, this_month):
                paid = paid_by_month.get(month, Decimal("0"))
                short = monthly_amount - paid
                if short > 0:
                    balance_due += short
                    months_behind += 1
                if month == this_month:
                    this_month_status = status_for(monthly_amount, joined, month, paid, today)
                    this_month_due = short if short > 0 else Decimal("0")

        summary.append({
            "student": student,
            "statuses": statuses,
            "total_paid": total_paid,
            "balance_due": balance_due,
            "months_behind": months_behind,
            "this_month_status": this_month_status,
            "this_month_due": this_month_due,
        })
    return summary
