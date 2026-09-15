"""add BACE monthly waiver and remarks adjustments

Revision ID: 8c5d1e7a2b94
Revises: 7b4c2e9a1d63
Create Date: 2026-09-15 18:30:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "8c5d1e7a2b94"
down_revision = "7b4c2e9a1d63"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "bace_rent_adjustments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("student_id", sa.Integer(), nullable=False),
        sa.Column("bace_property_id", sa.Integer(), nullable=True),
        sa.Column("for_month", sa.Date(), nullable=False),
        sa.Column("amount_waived", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"),
        sa.Column("remarks", sa.String(length=500), nullable=True),
        sa.Column("recorded_by", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["student_id"], ["bace_students.id"]),
        sa.ForeignKeyConstraint(["bace_property_id"], ["bace_properties.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("student_id", "for_month", name="uq_bace_rent_adjustment_student_month"),
    )
    op.create_index("ix_bace_rent_adjustments_student_id", "bace_rent_adjustments", ["student_id"])
    op.create_index("ix_bace_rent_adjustments_bace_property_id", "bace_rent_adjustments", ["bace_property_id"])
    op.create_index("ix_bace_rent_adjustments_for_month", "bace_rent_adjustments", ["for_month"])


def downgrade():
    op.drop_index("ix_bace_rent_adjustments_for_month", table_name="bace_rent_adjustments")
    op.drop_index("ix_bace_rent_adjustments_bace_property_id", table_name="bace_rent_adjustments")
    op.drop_index("ix_bace_rent_adjustments_student_id", table_name="bace_rent_adjustments")
    op.drop_table("bace_rent_adjustments")
