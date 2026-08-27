"""add campaigns.suppress_receipt and seed the Dhoti Kurta Contribution campaign

Revision ID: a91c7f3e2b56
Revises: f4a08d3c6e12
Create Date: 2026-08-27 12:00:00.000000

"""
import datetime

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a91c7f3e2b56'
down_revision = 'f4a08d3c6e12'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('campaigns', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('suppress_receipt', sa.Boolean(), nullable=False, server_default=sa.false())
        )

    campaigns = sa.table(
        'campaigns',
        sa.column('name', sa.String),
        sa.column('is_80g', sa.Boolean),
        sa.column('is_active', sa.Boolean),
        sa.column('suppress_receipt', sa.Boolean),
        sa.column('created_at', sa.DateTime),
    )
    conn = op.get_bind()
    exists = conn.execute(
        sa.text("SELECT 1 FROM campaigns WHERE name = :name"), {"name": "Dhoti Kurta Contribution"}
    ).first()
    if not exists:
        op.bulk_insert(campaigns, [{
            "name": "Dhoti Kurta Contribution",
            "is_80g": False,
            "is_active": True,
            "suppress_receipt": True,
            "created_at": datetime.datetime.utcnow(),
        }])


def downgrade():
    op.execute(sa.text("DELETE FROM campaigns WHERE name = 'Dhoti Kurta Contribution'"))
    with op.batch_alter_table('campaigns', schema=None) as batch_op:
        batch_op.drop_column('suppress_receipt')
