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
    # batch_alter_table so the same migration runs on SQLite (the test suite's
    # db_deploy tests) as well as PostgreSQL (production): SQLite can't ALTER
    # a table to add a constraint. On PostgreSQL batch mode emits the same
    # plain ALTER TABLE statements as before, so the resulting schema is
    # identical to what production already has.
    with op.batch_alter_table("donations") as batch_op:
        batch_op.add_column(sa.Column("preaching_center_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("preaching_center_name", sa.String(length=150), nullable=True))
        batch_op.create_index("ix_donations_preaching_center_id", ["preaching_center_id"])
        batch_op.create_foreign_key("fk_donations_preaching_center", "preaching_centers", ["preaching_center_id"], ["id"])
    with op.batch_alter_table("admin_users") as batch_op:
        batch_op.add_column(sa.Column("preaching_center_id", sa.Integer(), nullable=True))
        batch_op.create_index("ix_admin_users_preaching_center_id", ["preaching_center_id"])
        batch_op.create_foreign_key("fk_admin_users_preaching_center", "preaching_centers", ["preaching_center_id"], ["id"])
    op.create_table(
        "preaching_center_purposes",
        sa.Column("preaching_center_id", sa.Integer(), nullable=False),
        sa.Column("live_to_give_purpose_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["preaching_center_id"], ["preaching_centers.id"]),
        sa.ForeignKeyConstraint(["live_to_give_purpose_id"], ["live_to_give_purposes.id"]),
        sa.PrimaryKeyConstraint("preaching_center_id", "live_to_give_purpose_id"),
    )


def downgrade():
    op.drop_table("preaching_center_purposes")
    with op.batch_alter_table("admin_users") as batch_op:
        batch_op.drop_constraint("fk_admin_users_preaching_center", type_="foreignkey")
        batch_op.drop_index("ix_admin_users_preaching_center_id")
        batch_op.drop_column("preaching_center_id")
    with op.batch_alter_table("donations") as batch_op:
        batch_op.drop_constraint("fk_donations_preaching_center", type_="foreignkey")
        batch_op.drop_index("ix_donations_preaching_center_id")
        batch_op.drop_column("preaching_center_name")
        batch_op.drop_column("preaching_center_id")
    op.drop_table("preaching_centers")
