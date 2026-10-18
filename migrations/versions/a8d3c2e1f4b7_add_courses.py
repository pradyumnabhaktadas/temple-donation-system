"""add courses table and donations.course_id

Revision ID: a8d3c2e1f4b7
Revises: f6b8c1d2e3a4
Create Date: 2026-10-18
"""
from alembic import op
import sqlalchemy as sa


revision = "a8d3c2e1f4b7"
down_revision = "f6b8c1d2e3a4"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "courses" not in insp.get_table_names():
        op.create_table(
            "courses",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=150), nullable=False),
            sa.Column("suggested_amount", sa.Numeric(precision=12, scale=2), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("name"),
        )

    existing_cols = {c["name"] for c in insp.get_columns("donations")}
    if "course_id" not in existing_cols:
        with op.batch_alter_table("donations", schema=None) as batch_op:
            batch_op.add_column(sa.Column("course_id", sa.Integer(), nullable=True))
            batch_op.create_index("ix_donations_course_id", ["course_id"])
            batch_op.create_foreign_key(
                "fk_donations_course_id", "courses", ["course_id"], ["id"]
            )


def downgrade():
    with op.batch_alter_table("donations", schema=None) as batch_op:
        batch_op.drop_constraint("fk_donations_course_id", type_="foreignkey")
        batch_op.drop_index("ix_donations_course_id")
        batch_op.drop_column("course_id")
    op.drop_table("courses")
