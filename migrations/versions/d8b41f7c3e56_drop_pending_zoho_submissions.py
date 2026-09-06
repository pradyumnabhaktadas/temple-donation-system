"""drop pending_zoho_submissions

Revision ID: d8b41f7c3e56
Revises: c7e39b5a1d84
Create Date: 2026-09-06 19:00:00.000000

This table existed to hold Zoho's pre-payment webhook call -- the donor's
details, with no transaction ID -- so that a later Razorpay payment could
be matched back to it by phone, amount and a time window.

That matching is gone. Razorpay knows which payments were captured and
have no donation behind them, and the Google Sheet that Zoho writes every
submission into supplies the donor's name, so the join is direct and there
is no queue of half-known submissions to keep. See
public.reconcile_zoho_submissions and zoho_sheet.

The rows dropped here are submissions that were still awaiting payment.
They carry no money and no receipt: a donation that was paid for has
already become a Donation row, and one that was not is someone who opened
a form and left. Anything genuinely outstanding still surfaces through
Razorpay, which is the authority on that question and never depended on
this table.

downgrade() recreates the table empty rather than restoring rows, since
the data cannot be reconstructed. Anyone reversing this would also be
reverting the code that populated it.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd8b41f7c3e56'
down_revision = 'c7e39b5a1d84'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_index('ix_pending_zoho_submissions_received_at', table_name='pending_zoho_submissions')
    op.drop_index('ix_pending_zoho_submissions_phone_normalized', table_name='pending_zoho_submissions')
    op.drop_table('pending_zoho_submissions')


def downgrade():
    op.create_table(
        'pending_zoho_submissions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('campaign_id', sa.Integer(), nullable=True),
        sa.Column('campaign_param', sa.String(length=150), nullable=True),
        sa.Column('full_name', sa.String(length=150), nullable=True),
        sa.Column('phone_normalized', sa.String(length=20), nullable=True),
        sa.Column('amount', sa.Float(), nullable=True),
        sa.Column('payload_json', sa.Text(), nullable=True),
        sa.Column('received_at', sa.DateTime(), nullable=False),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
        sa.Column('donation_id', sa.Integer(), nullable=True),
        sa.Column('resolution', sa.String(length=20), nullable=True),
        sa.Column('note', sa.String(length=300), nullable=True),
        sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id']),
        sa.ForeignKeyConstraint(['donation_id'], ['donations.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_pending_zoho_submissions_phone_normalized',
        'pending_zoho_submissions', ['phone_normalized'],
    )
    op.create_index(
        'ix_pending_zoho_submissions_received_at',
        'pending_zoho_submissions', ['received_at'],
    )
