"""add unique index on bace_rent_payments.source_donation_id

Revision ID: 9a2f7c4e8b31
Revises: 8c5d1e7a2b94
Create Date: 2026-09-15 19:10:00.000000

Incident (2026-09-15): the same online donation got finalized twice, 23ms
apart -- see public.py's _finalize_success() docstring for the with_for_update
lock that's supposed to prevent exactly this. Whatever let that through,
record_matched_donation()'s own idempotency check (a plain
"SELECT ... WHERE source_donation_id = X" with no lock -- see
bace_matching.py) offered zero protection against two near-simultaneous
callers each seeing "not recorded yet" before either had inserted: it
created two BaceRentPayment rows for the same donation, showing up on the
BACE Tracker as a student appearing to have paid twice for one payment.

This is the hard backstop: a real database constraint, not just an
application-level check that can race. Scoped to WHERE recorded_by =
'auto-match' -- that literal string is used exclusively by
bace_matching.record_matched_donation() (never by a human), which only
ever creates one payment per donation. The admin's "Allocate across
students/months" screen (admin.py's allocate_bace_contribution_rent)
deliberately creates several BaceRentPayment rows sharing one
source_donation_id when a contribution is split between people/months --
those are recorded_by=<admin username>, so this constraint doesn't touch
them.

First deploy attempt (2026-09-15) failed outright: CREATE UNIQUE INDEX
found a SECOND, previously undiscovered instance of this same race from
2026-09-13 (donation 15319, Manish Kumar -- payments 99 and 100), on top
of the already-known one (donation 15328, payments 106 and 107). Alembic's
transactional DDL rolled the whole migration back cleanly on failure, so
this never left the schema half-migrated -- but it means an unknown number
of these duplicate pairs could exist, and this migration can't assume
they've all been found and hand-fixed first. So upgrade() now dedupes
generically -- every (source_donation_id, recorded_by='auto-match') group
with more than one row, keeping whichever row's reference names the
donation's own current receipt_number (falling back to the highest id if
none match) -- before creating the index, instead of requiring a one-off
script to run first and hoping nothing was missed.
"""
from alembic import op
import sqlalchemy as sa


revision = "9a2f7c4e8b31"
down_revision = "8c5d1e7a2b94"
branch_labels = None
depends_on = None


def _dedupe_auto_matched_payments(conn):
    groups = conn.execute(sa.text(
        "SELECT source_donation_id FROM bace_rent_payments "
        "WHERE recorded_by = 'auto-match' AND source_donation_id IS NOT NULL "
        "GROUP BY source_donation_id HAVING COUNT(*) > 1"
    )).fetchall()
    for (donation_id,) in groups:
        rows = conn.execute(sa.text(
            "SELECT id, reference FROM bace_rent_payments "
            "WHERE recorded_by = 'auto-match' AND source_donation_id = :donation_id "
            "ORDER BY id"
        ), {"donation_id": donation_id}).fetchall()
        receipt_number = conn.execute(sa.text(
            "SELECT receipt_number FROM donations WHERE id = :donation_id"
        ), {"donation_id": donation_id}).scalar()
        keep_id = next(
            (row.id for row in rows if receipt_number and row.reference and receipt_number in row.reference),
            rows[-1].id,
        )
        remove_ids = [row.id for row in rows if row.id != keep_id]
        if remove_ids:
            conn.execute(
                sa.text("DELETE FROM bace_rent_payments WHERE id IN :ids").bindparams(
                    sa.bindparam("ids", expanding=True)
                ),
                {"ids": remove_ids},
            )
            print(f"9a2f7c4e8b31: donation {donation_id} had {len(rows)} auto-matched payments "
                  f"({[row.id for row in rows]}) -- kept {keep_id}, removed {remove_ids}")


def upgrade():
    _dedupe_auto_matched_payments(op.get_bind())
    op.create_index(
        "uq_bace_rent_payments_auto_match_source_donation_id",
        "bace_rent_payments",
        ["source_donation_id"],
        unique=True,
        postgresql_where=sa.text("recorded_by = 'auto-match'"),
        sqlite_where=sa.text("recorded_by = 'auto-match'"),
    )


def downgrade():
    op.drop_index("uq_bace_rent_payments_auto_match_source_donation_id", table_name="bace_rent_payments")
