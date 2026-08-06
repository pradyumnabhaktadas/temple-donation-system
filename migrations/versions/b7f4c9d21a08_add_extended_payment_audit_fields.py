"""add extended payment audit fields

Revision ID: b7f4c9d21a08
Revises: a3e62e16cb44
Create Date: 2026-08-05 17:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b7f4c9d21a08'
down_revision = 'a3e62e16cb44'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('donations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('razorpay_order_receipt', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('razorpay_status', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('razorpay_currency', sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column('razorpay_upi_flow', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('razorpay_card_network', sa.String(length=30), nullable=True))
        batch_op.add_column(sa.Column('razorpay_card_type', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('razorpay_utr', sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column('donor_ip_address', sa.String(length=45), nullable=True))
        batch_op.add_column(sa.Column('donor_user_agent', sa.String(length=300), nullable=True))
        batch_op.add_column(sa.Column('updated_at', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('donations', schema=None) as batch_op:
        batch_op.drop_column('updated_at')
        batch_op.drop_column('donor_user_agent')
        batch_op.drop_column('donor_ip_address')
        batch_op.drop_column('razorpay_utr')
        batch_op.drop_column('razorpay_card_type')
        batch_op.drop_column('razorpay_card_network')
        batch_op.drop_column('razorpay_upi_flow')
        batch_op.drop_column('razorpay_currency')
        batch_op.drop_column('razorpay_status')
        batch_op.drop_column('razorpay_order_receipt')
