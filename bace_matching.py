"""Matching BACE Contribution donations to the BACE Rent Tracker roster,
and turning a matched donation into a rent payment without anyone having
to retype it. Shared logic behind:

  - a brand new BACE Contribution donation auto-recording its own rent
    payment the instant it succeeds, if its donor is already on the
    roster (called from public.py's _finalize_success() for online
    payments, and admin.py's _create_offline_donation() for offline/
    manual entries)
  - a student's past, already-matching donations getting caught up the
    moment they're added to the roster (admin.py's bace_students())
  - the "Record all matched" bulk action and the one-click per-row button
    on Admin -> BACE Contribution Logs (admin.py)

Kept in its own module rather than living in admin.py so public.py can
import it without a circular import (admin.py already imports from
public.py). Every entry point funnels through record_matched_donation(),
which is also the one place the "never guess" rule is enforced: a donor
whose phone or email matches more than one student is left unmatched
rather than picking one, everywhere this is called from -- a wrong guess
here would misattribute someone else's rent.
"""
from collections import defaultdict

from flask import current_app
from flask_login import current_user
from sqlalchemy import or_

from extensions import db
from models import BaceStudent, BaceRentPayment, AdminActivityLog
from utils import to_ist
import bace_tracker


def match_students(donations):
    """Best-effort match of each donation's donor to a BaceStudent by
    phone or email. Returns {donation.id: BaceStudent}."""
    phones = {d.donor.phone for d in donations if d.donor and d.donor.phone}
    emails = {d.donor.email for d in donations if d.donor and d.donor.email}
    if not phones and not emails:
        return {}

    conditions = []
    if phones:
        conditions.append(BaceStudent.phone.in_(phones))
    if emails:
        conditions.append(BaceStudent.email.in_(emails))
    candidates = BaceStudent.query.filter(or_(*conditions)).all()

    by_phone = defaultdict(list)
    by_email = defaultdict(list)
    for s in candidates:
        if s.phone:
            by_phone[s.phone].append(s)
        if s.email:
            by_email[s.email].append(s)

    matched = {}
    for d in donations:
        if not d.donor:
            continue
        found = None
        if d.donor.phone and len(by_phone.get(d.donor.phone, [])) == 1:
            found = by_phone[d.donor.phone][0]
        elif d.donor.email and len(by_email.get(d.donor.email, [])) == 1:
            found = by_email[d.donor.email][0]
        if found:
            matched[d.id] = found
    return matched


def record_matched_donation(donation, student=None):
    """Turns a matched, successful BACE Contribution donation into a
    BaceRentPayment. Adds it to the session (flushed, not committed --
    the caller owns the transaction and must commit) and returns it, or
    returns None and changes nothing if the donation isn't eligible, is
    already recorded, or doesn't match exactly one student.

    The month comes from the donation's own date (IST for online
    payments), not "today" -- a contribution made in August marks August
    paid even if this runs later. recorded_by is the literal string
    "auto-match" (never an admin username) so the Payments Log always
    shows, at a glance, which rows nobody typed in by hand.
    """
    if donation.status != "success" or not donation.bace_property_id:
        return None
    if BaceRentPayment.query.filter_by(source_donation_id=donation.id).first():
        return None
    if student is None:
        student = match_students([donation]).get(donation.id)
    if not student:
        return None

    donation_date = to_ist(donation.donation_date) if donation.payment_mode == "online" else donation.donation_date
    for_month = bace_tracker.month_start(donation_date.date())

    payment = BaceRentPayment(
        student_id=student.id,
        for_month=for_month,
        amount_paid=donation.amount,
        date_paid=donation_date.date(),
        mode="Online (givetokrishna.com)" if donation.payment_mode == "online" else None,
        recorded_by="auto-match",
        reference=f"BACE Contribution {donation.receipt_number or ('#' + str(donation.id))}",
        source_donation_id=donation.id,
    )
    db.session.add(payment)
    db.session.flush()

    # Same activity-log convention as admin.py's log_activity() (falls
    # back to "system" when there's no logged-in admin -- true for the
    # online-payment-success path, which runs from a donor-facing request
    # with nobody authenticated) -- duplicated rather than imported from
    # admin.py to avoid the circular import this module exists to avoid.
    try:
        db.session.add(AdminActivityLog(
            admin_username=current_user.username if current_user.is_authenticated else "system",
            action="bace_payment_add", target_type="bace_rent_payment", target_id=payment.id,
            details=(
                f"Rs. {donation.amount:,.2f} from '{student.full_name}' for "
                f"{for_month.strftime('%b %Y')} (auto-recorded from BACE Contribution "
                f"donation {donation.receipt_number or donation.id})"
            ),
        ))
    except Exception:
        current_app.logger.exception(
            "Failed to write activity log for auto-recorded BACE rent payment, donation %s", donation.id
        )

    return payment


def record_all_matched(donations):
    """Bulk version of record_matched_donation() -- matches every
    donation in `donations` at once (one query instead of N) and records
    whichever ones resolve to exactly one student and aren't already
    recorded. Returns the list of BaceRentPayment rows created (flushed,
    not committed -- caller commits). Used by the "Record all matched"
    bulk action and by bace_students() catching up a newly-added
    student's past donations."""
    matched = match_students(donations)
    created = []
    for d in donations:
        payment = record_matched_donation(d, student=matched.get(d.id))
        if payment:
            created.append(payment)
    return created
