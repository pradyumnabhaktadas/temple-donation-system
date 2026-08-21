"""add associated_withs table and donations.associated_with_id

Revision ID: f4a08d3c6e12
Revises: d5f27a1b8c64
Create Date: 2026-08-21 12:00:00.000000

"""
import datetime

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f4a08d3c6e12'
down_revision = 'd5f27a1b8c64'
branch_labels = None
depends_on = None


# The starting list from the feature request -- seeded here so it's live
# the moment `flask db upgrade` runs on deploy, rather than needing
# someone to type all eight in by hand through the new admin page first.
# Office staff can rename/reorder/deactivate/add to this from
# Admin -> Associated With afterwards; nothing about this seed is special
# or re-run on a later migration.
_SEED_OPTIONS = [
    "IYF Dwarka Temple Preaching",
    "Online Preaching",
    "HG Achyutanand Pr",
    "IYF Bhakti Vriksha - Sujeet Pr",
    "IYF Bhakti Vriksha - HG Sri Gaur Pr",
    "IYF Bhakti Vriksha - General",
    "College Preaching",
    "HG Veer Chaitanya Pr",
]


def upgrade():
    associated_withs = op.create_table(
        'associated_withs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=150), nullable=False),
        sa.Column('display_order', sa.Integer(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )
    with op.batch_alter_table('donations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('associated_with_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_donations_associated_with_id', 'associated_withs', ['associated_with_id'], ['id']
        )

    now = datetime.datetime.utcnow()
    op.bulk_insert(associated_withs, [
        {"name": name, "display_order": i * 10, "is_active": True, "created_at": now}
        for i, name in enumerate(_SEED_OPTIONS)
    ])


def downgrade():
    with op.batch_alter_table('donations', schema=None) as batch_op:
        batch_op.drop_constraint('fk_donations_associated_with_id', type_='foreignkey')
        batch_op.drop_column('associated_with_id')
    op.drop_table('associated_withs')
