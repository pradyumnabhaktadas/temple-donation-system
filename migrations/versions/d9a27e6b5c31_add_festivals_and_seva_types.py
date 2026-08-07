"""add festivals and seva_types tables

Revision ID: d9a27e6b5c31
Revises: c8e51f3a9d47
Create Date: 2026-08-05 19:45:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd9a27e6b5c31'
down_revision = 'c8e51f3a9d47'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'festivals',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=150), nullable=False),
        sa.Column('event_date', sa.Date(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )
    op.create_table(
        'seva_types',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=150), nullable=False),
        sa.Column('suggested_amount', sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )
    with op.batch_alter_table('donations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('festival_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('seva_type_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_donations_festival_id', 'festivals', ['festival_id'], ['id'])
        batch_op.create_foreign_key('fk_donations_seva_type_id', 'seva_types', ['seva_type_id'], ['id'])


def downgrade():
    with op.batch_alter_table('donations', schema=None) as batch_op:
        batch_op.drop_constraint('fk_donations_seva_type_id', type_='foreignkey')
        batch_op.drop_constraint('fk_donations_festival_id', type_='foreignkey')
        batch_op.drop_column('seva_type_id')
        batch_op.drop_column('festival_id')
    op.drop_table('seva_types')
    op.drop_table('festivals')
