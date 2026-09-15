"""One-off data repair (2026-09-15): the BACE Tracker showed Ashutosh
Sharma (BaceStudent id 25, Yogapitha BACE) as having paid Rs. 18,000 in
September when he'd actually paid Rs. 6,000 -- two separate bugs stacked
on the same day:

1. Donation 15328 (Ashutosh's real Rs. 6000 online payment) was finalized
   twice, 23 milliseconds apart -- see public.py's _finalize_success() and
   the 2026-09-15 comment there. That issued two receipt numbers
   (032511/ISK500684, then 032511/ISK500685, the second silently
   overwriting the first on the donation row) and created two
   BaceRentPayment rows for the same donation. 500684 was never left on
   any donation -- it's not "missing from the system", it was allocated by
   the receipt counter and then orphaned the instant the second call's
   commit overwrote it. This script removes the duplicate payment row
   (whichever one's reference text doesn't match the donation's real,
   final receipt_number).

2. Donation 15329 -- a separate Rs. 6000 donation from a different person,
   Aditya Sharma (phone 7696870108) -- auto-matched onto Ashutosh's roster
   entry because both donations used the same email address
   (kd.ashu.sharma108@gmail.com) and Aditya wasn't on the BACE roster
   under his own record yet. This script creates that record (Yogapitha
   BACE, Rs. 6000/month, joined September 2026, deliberately WITHOUT that
   shared email -- see the note on the new BaceStudent below) and moves
   his payment onto it.

Safe to run more than once -- every step checks first and skips if
already done. Also safe to run before or after `flask db upgrade` for
migration 9a2f7c4e8b31 (the new unique index) -- this script's own dedupe
step is what that index exists to make permanent.

Usage (Render Shell, from the project root):
    python3 fix_bace_ashutosh_aditya_20260915.py
"""
import datetime

from app import create_app
from extensions import db
from models import BaceStudent, BaceRentPayment, Donation

ASHUTOSH_STUDENT_ID = 25
ASHUTOSH_DONATION_ID = 15328
ADITYA_DONATION_ID = 15329
ADITYA_PHONE = "7696870108"
ADITYA_NAME = "Aditya Sharma"

app = create_app()

with app.app_context():
    # --- Step 1: dedupe Ashutosh's double-finalized donation -----------
    dupes = BaceRentPayment.query.filter_by(
        source_donation_id=ASHUTOSH_DONATION_ID, recorded_by="auto-match",
    ).order_by(BaceRentPayment.id).all()
    if len(dupes) <= 1:
        print(f"Donation {ASHUTOSH_DONATION_ID}: {len(dupes)} auto-matched payment(s) -- nothing to dedupe.")
    else:
        donation = db.session.get(Donation, ASHUTOSH_DONATION_ID)
        keep = next((p for p in dupes if p.reference and donation.receipt_number and
                     donation.receipt_number in p.reference), dupes[-1])
        removed = 0
        for p in dupes:
            if p.id == keep.id:
                continue
            print(f"Deleting duplicate BaceRentPayment id={p.id} (reference={p.reference!r}), "
                  f"keeping id={keep.id} (reference={keep.reference!r})")
            db.session.delete(p)
            removed += 1
        db.session.commit()
        print(f"Donation {ASHUTOSH_DONATION_ID}: removed {removed} duplicate row(s).")

    # --- Step 2: give Aditya Sharma his own roster record ---------------
    aditya = BaceStudent.query.filter_by(phone=ADITYA_PHONE).first()
    if aditya:
        print(f"BaceStudent for phone {ADITYA_PHONE} already exists (id={aditya.id}) -- skipping create.")
    else:
        ashutosh = db.session.get(BaceStudent, ASHUTOSH_STUDENT_ID)
        aditya = BaceStudent(
            full_name=ADITYA_NAME,
            phone=ADITYA_PHONE,
            email=None,  # deliberately blank -- see this script's module docstring:
                         # this is exactly the shared-email collision that caused the
                         # mismatch, so Aditya's own record only matches by phone.
            bace_property_id=ashutosh.bace_property_id,
            monthly_amount=ashutosh.monthly_amount,
            joined_month=datetime.date(2026, 9, 1),
            status="Active",
            notes=(
                "Added 2026-09-15: donation 15329 had been auto-matched onto "
                "Ashutosh Sharma's record (id 25) via a shared donor email; split "
                "out into this record. See fix_bace_ashutosh_aditya_20260915.py."
            ),
        )
        db.session.add(aditya)
        db.session.commit()
        print(f"Created BaceStudent id={aditya.id} for {ADITYA_NAME} ({ADITYA_PHONE}).")

    # --- Step 3: move Aditya's payment onto his own record ---------------
    payment = BaceRentPayment.query.filter_by(source_donation_id=ADITYA_DONATION_ID).first()
    if payment is None:
        print(f"No BaceRentPayment found for donation {ADITYA_DONATION_ID} -- nothing to move.")
    elif payment.student_id == aditya.id:
        print(f"Payment {payment.id} already points at {ADITYA_NAME} (id={aditya.id}) -- skipping.")
    else:
        print(f"Moving BaceRentPayment id={payment.id} from student_id={payment.student_id} "
              f"to student_id={aditya.id} ({ADITYA_NAME}).")
        payment.student_id = aditya.id
        payment.bace_property_id = aditya.bace_property_id
        db.session.commit()

print("Done. Refresh the BACE Tracker -- Ashutosh should show Rs. 6,000 for September, "
      "and Aditya Sharma should now appear as his own row with his own Rs. 6,000.")
