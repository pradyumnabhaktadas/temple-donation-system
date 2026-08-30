"""add daily_report_recipients.frequency

Revision ID: b6a2e4f81c93
Revises: f3a8c1d5e720
Create Date: 2026-08-30 09:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b6a2e4f81c93'
down_revision = 'f3a8c1d5e720'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('daily_report_recipients', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('frequency', sa.String(length=20), nullable=False, server_default='daily')
        )


def downgrade():
    with op.batch_alter_table('daily_report_recipients', schema=None) as batch_op:
        batch_op.drop_column('frequency')
