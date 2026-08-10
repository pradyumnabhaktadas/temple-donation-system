"""add is_80g to live_to_give_purposes

Revision ID: a1c58f6d0e29
Revises: e7c14b92a380
Create Date: 2026-08-10 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1c58f6d0e29'
down_revision = 'e7c14b92a380'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('live_to_give_purposes', schema=None) as batch_op:
        # server_default='0' so existing rows backfill to Non-80G (the
        # "rest are non-80G" majority case) rather than erroring on the
        # NOT NULL constraint -- staff then flip the six actually-eligible
        # purposes (Food for Life, Charity, Donation, Life Membership,
        # Construction, Annadan) to Yes from Admin -> Live To Give Purposes.
        batch_op.add_column(sa.Column('is_80g', sa.Boolean(), nullable=False, server_default=sa.false()))

    # Drop the server_default after backfilling -- SQLAlchemy's own default
    # (also False) takes over for new rows from here, keeping the column
    # definition clean going forward (same convention used elsewhere in
    # this migration history).
    with op.batch_alter_table('live_to_give_purposes', schema=None) as batch_op:
        batch_op.alter_column('is_80g', server_default=None)


def downgrade():
    with op.batch_alter_table('live_to_give_purposes', schema=None) as batch_op:
        batch_op.drop_column('is_80g')
