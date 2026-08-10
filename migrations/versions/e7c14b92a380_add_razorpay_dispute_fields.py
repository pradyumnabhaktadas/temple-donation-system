"""add razorpay dispute fields to donations

Revision ID: e7c14b92a380
Revises: d3f8a5c19b76
Create Date: 2026-08-10 09:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e7c14b92a380'
down_revision = 'd3f8a5c19b76'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('donations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('razorpay_dispute_id', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('razorpay_dispute_status', sa.String(length=30), nullable=True))
        batch_op.add_column(sa.Column('razorpay_dispute_reason', sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column('disputed_at', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('donations', schema=None) as batch_op:
        batch_op.drop_column('disputed_at')
        batch_op.drop_column('razorpay_dispute_reason')
        batch_op.drop_column('razorpay_dispute_status')
        batch_op.drop_column('razorpay_dispute_id')
