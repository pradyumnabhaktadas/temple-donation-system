"""create preaching center purpose mappings for existing deployments

Revision ID: f6b8c1d2e3a4
Revises: c3a1f7d9e2b4
Create Date: 2026-10-15

The first preaching-center migration was deployed before the optional,
per-center purpose mapping was introduced.  New databases already have this
table from that migration; production databases at its earlier revision need
this small follow-up migration.
"""
from alembic import op
import sqlalchemy as sa


revision = "f6b8c1d2e3a4"
down_revision = "c3a1f7d9e2b4"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if "preaching_center_purposes" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "preaching_center_purposes",
            sa.Column("preaching_center_id", sa.Integer(), nullable=False),
            sa.Column("live_to_give_purpose_id", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["preaching_center_id"], ["preaching_centers.id"]),
            sa.ForeignKeyConstraint(["live_to_give_purpose_id"], ["live_to_give_purposes.id"]),
            sa.PrimaryKeyConstraint("preaching_center_id", "live_to_give_purpose_id"),
        )


def downgrade():
    # The table may have been created by the preceding migration on a fresh
    # database, so a downgrade here must not delete it (or its mappings).
    pass
