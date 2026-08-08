"""add donations.cheque_number / cheque_bank_name / bank_transaction_id

Revision ID: f1a92c7d3e56
Revises: e2b74a1c8f63
Create Date: 2026-08-08 09:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f1a92c7d3e56'
down_revision = 'e2b74a1c8f63'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('donations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cheque_number', sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column('cheque_bank_name', sa.String(length=150), nullable=True))
        batch_op.add_column(sa.Column('bank_transaction_id', sa.String(length=100), nullable=True))


def downgrade():
    with op.batch_alter_table('donations', schema=None) as batch_op:
        batch_op.drop_column('bank_transaction_id')
        batch_op.drop_column('cheque_bank_name')
        batch_op.drop_column('cheque_number')
