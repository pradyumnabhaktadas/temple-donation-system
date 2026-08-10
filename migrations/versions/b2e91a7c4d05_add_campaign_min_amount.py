"""add min_amount to campaigns

Revision ID: b2e91a7c4d05
Revises: a1c58f6d0e29
Create Date: 2026-08-10 13:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b2e91a7c4d05'
down_revision = 'a1c58f6d0e29'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('campaigns', schema=None) as batch_op:
        batch_op.add_column(sa.Column('min_amount', sa.Numeric(12, 2), nullable=True))

    # Preserve the previously-hardcoded Rs. 101 minimum for Live To Give so
    # behavior doesn't change until an admin edits it from Admin -> Campaigns.
    op.execute("UPDATE campaigns SET min_amount = 101 WHERE name = 'Live To Give'")


def downgrade():
    with op.batch_alter_table('campaigns', schema=None) as batch_op:
        batch_op.drop_column('min_amount')
