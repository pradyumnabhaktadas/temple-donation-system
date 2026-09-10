"""add bace_rent_payments.source_donation_id

Revision ID: b3f9d61a7c22
Revises: e7c2b4a9f105
Create Date: 2026-09-10 14:00:00.000000

Lets a BACE Contribution donation be turned into a rent payment with one
click (Admin -> BACE Contribution Logs -> "Record as rent payment")
without risking it being recorded twice -- this column is how that page
tells "already recorded" apart from "no match yet".
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b3f9d61a7c22'
down_revision = 'e7c2b4a9f105'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('bace_rent_payments', schema=None) as batch_op:
        batch_op.add_column(sa.Column('source_donation_id', sa.Integer(), nullable=True))
        batch_op.create_index(
            'ix_bace_rent_payments_source_donation_id', ['source_donation_id']
        )
        batch_op.create_foreign_key(
            'fk_bace_rent_payments_source_donation_id', 'donations', ['source_donation_id'], ['id']
        )


def downgrade():
    with op.batch_alter_table('bace_rent_payments', schema=None) as batch_op:
        batch_op.drop_constraint('fk_bace_rent_payments_source_donation_id', type_='foreignkey')
        batch_op.drop_index('ix_bace_rent_payments_source_donation_id')
        batch_op.drop_column('source_donation_id')
