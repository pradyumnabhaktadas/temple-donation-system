"""add donations.cancelled_at / cancelled_by / cancellation_reason

Revision ID: a7c31f9e2b04
Revises: f1a92c7d3e56
Create Date: 2026-08-08 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a7c31f9e2b04'
down_revision = 'f1a92c7d3e56'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('donations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cancelled_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('cancelled_by', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('cancellation_reason', sa.String(length=300), nullable=True))


def downgrade():
    with op.batch_alter_table('donations', schema=None) as batch_op:
        batch_op.drop_column('cancellation_reason')
        batch_op.drop_column('cancelled_by')
        batch_op.drop_column('cancelled_at')
