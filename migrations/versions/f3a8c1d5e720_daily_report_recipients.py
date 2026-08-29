"""add daily_report_recipients table

Revision ID: f3a8c1d5e720
Revises: a91c7f3e2b56
Create Date: 2026-08-29 13:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f3a8c1d5e720'
down_revision = 'a91c7f3e2b56'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'daily_report_recipients',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('contact_type', sa.String(length=20), nullable=False),
        sa.Column('value', sa.String(length=150), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('contact_type', 'value', name='uq_daily_report_recipient_type_value'),
    )


def downgrade():
    op.drop_table('daily_report_recipients')
