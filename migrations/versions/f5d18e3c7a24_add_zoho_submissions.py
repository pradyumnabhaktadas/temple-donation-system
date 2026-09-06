"""add zoho_submissions table

Revision ID: f5d18e3c7a24
Revises: e4c72a9b8d13
Create Date: 2026-09-07 12:00:00.000000

This app's own copy of every call Zoho Forms makes -- the same thing the
Google Sheet holds, kept locally instead. Razorpay knows a payment was
captured but records a phone number, not a name; this supplies the name.

Reading it from a published Google Sheet worked, but meant exposing donor
names and phone numbers on a public link, configuring a sheet per form,
and depending on Google being reachable when a receipt was due.

Not to be confused with pending_zoho_submissions, dropped in d8b41f7c3e56.
That was a work queue that tried to decide whether a receipt was owed, and
could not do so unambiguously. This is a log that only ever supplies a
name; Razorpay still decides everything else. See the ZohoSubmission
model's docstring.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f5d18e3c7a24'
down_revision = 'e4c72a9b8d13'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'zoho_submissions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('campaign_param', sa.String(length=150), nullable=True),
        sa.Column('campaign_id', sa.Integer(), nullable=True),
        sa.Column('full_name', sa.String(length=200), nullable=True),
        sa.Column('phone_normalized', sa.String(length=20), nullable=True),
        sa.Column('amount', sa.Float(), nullable=True),
        sa.Column('transaction_id', sa.String(length=100), nullable=True),
        sa.Column('payment_status', sa.String(length=50), nullable=True),
        sa.Column('payload_json', sa.Text(), nullable=True),
        sa.Column('received_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_zoho_submissions_phone_normalized', 'zoho_submissions', ['phone_normalized'])
    op.create_index('ix_zoho_submissions_transaction_id', 'zoho_submissions', ['transaction_id'])
    op.create_index('ix_zoho_submissions_received_at', 'zoho_submissions', ['received_at'])


def downgrade():
    op.drop_index('ix_zoho_submissions_received_at', table_name='zoho_submissions')
    op.drop_index('ix_zoho_submissions_transaction_id', table_name='zoho_submissions')
    op.drop_index('ix_zoho_submissions_phone_normalized', table_name='zoho_submissions')
    op.drop_table('zoho_submissions')
