"""add preachers table and donor relationship/family fields

Revision ID: c4d68b1e9a52
Revises: a7c31f9e2b04
Create Date: 2026-08-08 15:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c4d68b1e9a52'
down_revision = 'a7c31f9e2b04'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'preachers',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(length=150), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('name'),
    )

    with op.batch_alter_table('donors', schema=None) as batch_op:
        batch_op.add_column(sa.Column('donor_type', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('connected_preacher_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('donation_frequency', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('gifts', sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column('dob', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('father_dob', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('mother_dob', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('wife_dob', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('marriage_anniversary', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('additional_info', sa.Text(), nullable=True))
        batch_op.create_foreign_key(
            'fk_donors_connected_preacher_id', 'preachers', ['connected_preacher_id'], ['id']
        )


def downgrade():
    with op.batch_alter_table('donors', schema=None) as batch_op:
        batch_op.drop_constraint('fk_donors_connected_preacher_id', type_='foreignkey')
        batch_op.drop_column('additional_info')
        batch_op.drop_column('marriage_anniversary')
        batch_op.drop_column('wife_dob')
        batch_op.drop_column('mother_dob')
        batch_op.drop_column('father_dob')
        batch_op.drop_column('dob')
        batch_op.drop_column('gifts')
        batch_op.drop_column('donation_frequency')
        batch_op.drop_column('connected_preacher_id')
        batch_op.drop_column('donor_type')

    op.drop_table('preachers')
