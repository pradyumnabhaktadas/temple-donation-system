"""add preaching centers and donation source

Revision ID: c3a1f7d9e2b4
Revises: a1c9d4e8f2b6
Create Date: 2026-10-15
"""
from alembic import op
import sqlalchemy as sa

revision = "c3a1f7d9e2b4"
down_revision = "a1c9d4e8f2b6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "preaching_centers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("city", sa.String(length=100)),
        sa.Column("address", sa.String(length=400)),
        sa.Column("contact_name", sa.String(length=150)),
        sa.Column("contact_phone", sa.String(length=20)),
        sa.Column("target_amount", sa.Numeric(precision=12, scale=2)),
        sa.Column("target_period", sa.String(length=20)),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"), sa.UniqueConstraint("slug"),
    )
    op.add_column("donations", sa.Column("preaching_center_id", sa.Integer(), nullable=True))
    op.add_column("donations", sa.Column("preaching_center_name", sa.String(length=150), nullable=True))
    op.create_index("ix_donations_preaching_center_id", "donations", ["preaching_center_id"])
    op.create_foreign_key("fk_donations_preaching_center", "donations", "preaching_centers", ["preaching_center_id"], ["id"])
    op.add_column("admin_users", sa.Column("preaching_center_id", sa.Integer(), nullable=True))
    op.create_index("ix_admin_users_preaching_center_id", "admin_users", ["preaching_center_id"])
    op.create_foreign_key("fk_admin_users_preaching_center", "admin_users", "preaching_centers", ["preaching_center_id"], ["id"])


def downgrade():
    op.drop_constraint("fk_admin_users_preaching_center", "admin_users", type_="foreignkey")
    op.drop_index("ix_admin_users_preaching_center_id", table_name="admin_users")
    op.drop_column("admin_users", "preaching_center_id")
    op.drop_constraint("fk_donations_preaching_center", "donations", type_="foreignkey")
    op.drop_index("ix_donations_preaching_center_id", table_name="donations")
    op.drop_column("donations", "preaching_center_name")
    op.drop_column("donations", "preaching_center_id")
    op.drop_table("preaching_centers")
