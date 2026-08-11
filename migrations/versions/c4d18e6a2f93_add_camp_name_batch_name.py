"""add donations.camp_name / batch_name for IYF camp collections

Plain text rather than a Camp table by explicit choice -- camps are
short-lived and the data arrives from a Zoho export that already carries
the names as strings.

Both are indexed: the entire point of storing them is grouping and
filtering by camp, which is what the per-camp totals on the IYF Camps tab
do on every page load.

batch_alter_table is used so this also applies cleanly on SQLite (local
dev), which can't ALTER a column in place and needs the table rebuilt.

Revision ID: c4d18e6a2f93
Revises: b2e91a7c4d05
Create Date: 2026-08-11 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c4d18e6a2f93'
down_revision = 'b2e91a7c4d05'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('donations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('camp_name', sa.String(length=150), nullable=True))
        batch_op.add_column(sa.Column('batch_name', sa.String(length=150), nullable=True))
        batch_op.create_index('ix_donations_camp_name', ['camp_name'])
        batch_op.create_index('ix_donations_batch_name', ['batch_name'])


def downgrade():
    with op.batch_alter_table('donations', schema=None) as batch_op:
        batch_op.drop_index('ix_donations_batch_name')
        batch_op.drop_index('ix_donations_camp_name')
        batch_op.drop_column('batch_name')
        batch_op.drop_column('camp_name')
