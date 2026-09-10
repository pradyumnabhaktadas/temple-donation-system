"""index donations' hot filter/lookup columns

Part of a general optimization pass. Postgres doesn't auto-index foreign
key columns the way some other databases do, and donor_id/campaign_id/
bace_property_id are exactly the columns every admin list/report filters
or joins on (Donations Log, Donor detail, BACE Contribution Logs/Tracker,
Festival/Dhoti Kurta/Associated With reports). status and donation_date
are filtered/sorted on almost every one of those same pages. razorpay_
order_id/payment_id are looked up on every Razorpay webhook call,
independent of admin traffic entirely.

None of this matters yet -- the whole donations table is under a
thousand rows in production today -- but a plain CREATE INDEX costs
nothing at this size and pays for itself the moment the table grows
past what a sequential scan can shrug off, so there's no reason to wait
for that day to arrive before adding it.

Plain create_index/drop_index rather than batch_alter_table: no column
or constraint is being added or changed, just an index, which SQLite
supports directly without a table rebuild -- batch mode exists for the
ALTER-in-place cases SQLite can't do, not this one.

Revision ID: 069fa9d71441
Revises: b3f9d61a7c22
Create Date: 2026-09-11 00:00:00.000000

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = '069fa9d71441'
down_revision = 'b3f9d61a7c22'
branch_labels = None
depends_on = None


INDEXES = [
    ('ix_donations_donor_id', 'donor_id'),
    ('ix_donations_campaign_id', 'campaign_id'),
    ('ix_donations_bace_property_id', 'bace_property_id'),
    ('ix_donations_status', 'status'),
    ('ix_donations_donation_date', 'donation_date'),
    ('ix_donations_razorpay_order_id', 'razorpay_order_id'),
    ('ix_donations_razorpay_payment_id', 'razorpay_payment_id'),
]


def upgrade():
    for index_name, column in INDEXES:
        op.create_index(index_name, 'donations', [column])


def downgrade():
    for index_name, _ in reversed(INDEXES):
        op.drop_index(index_name, table_name='donations')
