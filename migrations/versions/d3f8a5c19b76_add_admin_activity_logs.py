"""add admin_activity_logs table

Revision ID: d3f8a5c19b76
Revises: c4d68b1e9a52
Create Date: 2026-08-09 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd3f8a5c19b76'
down_revision = 'c4d68b1e9a52'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'admin_activity_logs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('admin_username', sa.String(length=80), nullable=False),
        sa.Column('action', sa.String(length=50), nullable=False),
        sa.Column('target_type', sa.String(length=50), nullable=True),
        sa.Column('target_id', sa.Integer(), nullable=True),
        sa.Column('details', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('admin_activity_logs', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_admin_activity_logs_created_at'), ['created_at'], unique=False
        )


def downgrade():
    with op.batch_alter_table('admin_activity_logs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_admin_activity_logs_created_at'))
    op.drop_table('admin_activity_logs')
