"""add live_to_give_purposes table and donations.live_to_give_purpose_id / is_80g_requested

Revision ID: e2b74a1c8f63
Revises: d9a27e6b5c31
Create Date: 2026-08-07 11:15:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e2b74a1c8f63'
down_revision = 'd9a27e6b5c31'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'live_to_give_purposes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=150), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )
    with op.batch_alter_table('donations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('live_to_give_purpose_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('is_80g_requested', sa.Boolean(), nullable=True))
        batch_op.create_foreign_key(
            'fk_donations_live_to_give_purpose_id', 'live_to_give_purposes', ['live_to_give_purpose_id'], ['id']
        )


def downgrade():
    with op.batch_alter_table('donations', schema=None) as batch_op:
        batch_op.drop_constraint('fk_donations_live_to_give_purpose_id', type_='foreignkey')
        batch_op.drop_column('is_80g_requested')
        batch_op.drop_column('live_to_give_purpose_id')
    op.drop_table('live_to_give_purposes')
