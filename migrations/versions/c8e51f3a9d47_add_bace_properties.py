"""add bace_properties table and donations.bace_property_id

Revision ID: c8e51f3a9d47
Revises: b7f4c9d21a08
Create Date: 2026-08-05 18:15:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c8e51f3a9d47'
down_revision = 'b7f4c9d21a08'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'bace_properties',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=150), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )
    with op.batch_alter_table('donations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('bace_property_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_donations_bace_property_id', 'bace_properties', ['bace_property_id'], ['id']
        )


def downgrade():
    with op.batch_alter_table('donations', schema=None) as batch_op:
        batch_op.drop_constraint('fk_donations_bace_property_id', type_='foreignkey')
        batch_op.drop_column('bace_property_id')
    op.drop_table('bace_properties')
