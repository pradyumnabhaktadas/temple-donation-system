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
"""
from alembic import op
import sqlalchemy as sa


revision = "9a2f7c4e8b31"
down_revision = "8c5d1e7a2b94"
branch_labels = None
depends_on = None


def upgrade():
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
