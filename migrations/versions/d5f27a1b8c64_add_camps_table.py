"""add camps table (the IYF camp picker list)

Note what this table is: the list of camps offered in the entry dropdown.
It is not where a donation's camp is stored -- Donation.camp_name (added
in c4d18e6a2f93) holds the name as text, copied when the donation was
recorded.

That separation is the point. Camps get renamed and deleted, and with a
foreign key a deletion would either take its donations with it or leave
them orphaned, either way losing what that camp collected. Holding the
name on the donation means deleting a camp only removes it from the
dropdown; every rupee it collected still reports correctly. Renames are
propagated onto existing donations by admin.camp_edit so a corrected
spelling doesn't split one camp's total in two.

Revision ID: d5f27a1b8c64
Revises: c4d18e6a2f93
Create Date: 2026-08-11 11:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd5f27a1b8c64'
down_revision = 'c4d18e6a2f93'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'camps',
        sa.Column('id', sa.Integer(), nullable=False),
        # Unique so the list itself can't grow two spellings of one camp,
        # which is the failure this whole table exists to prevent.
        sa.Column('name', sa.String(length=150), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )


def downgrade():
    op.drop_table('camps')
