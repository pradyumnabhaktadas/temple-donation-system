"""add pending_zoho_submissions table

Revision ID: c7e39b5a1d84
Revises: b6a2e4f81c93
Create Date: 2026-09-06 10:00:00.000000

Backs PendingZohoSubmission -- see that model's docstring. Zoho Forms'
webhook fires once, before payment completes, so its only call carries the
donor's details but no transaction ID; this table is where that call is
kept until reconciliation can match it to a captured Razorpay payment,
instead of it being dropped on the floor as it was before.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c7e39b5a1d84'
down_revision = 'b6a2e4f81c93'
branch_labels = None
depends_on = None


def upgrade():
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


def downgrade():
    op.drop_index('ix_pending_zoho_submissions_received_at', table_name='pending_zoho_submissions')
    op.drop_index('ix_pending_zoho_submissions_phone_normalized', table_name='pending_zoho_submissions')
    op.drop_table('pending_zoho_submissions')
