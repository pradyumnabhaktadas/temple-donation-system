"""add immutable monthly BACE rent ledger

Revision ID: 4e5f6a7b8c9d
Revises: 069fa9d71441
Create Date: 2026-09-11 14:30:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "4e5f6a7b8c9d"
down_revision = "069fa9d71441"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "bace_rent_charges",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("student_id", sa.Integer(), nullable=False),
        sa.Column("for_month", sa.Date(), nullable=False),
        sa.Column("amount_due", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["student_id"], ["bace_students.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("student_id", "for_month", name="uq_bace_rent_charge_student_month"),
    )
    op.create_index("ix_bace_rent_charges_student_id", "bace_rent_charges", ["student_id"])
    op.create_index("ix_bace_rent_charges_for_month", "bace_rent_charges", ["for_month"])


def downgrade():
    op.drop_index("ix_bace_rent_charges_for_month", table_name="bace_rent_charges")
    op.drop_index("ix_bace_rent_charges_student_id", table_name="bace_rent_charges")
    op.drop_table("bace_rent_charges")
