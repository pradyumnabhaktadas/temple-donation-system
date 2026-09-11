"""link signed BACE payment-link donations to a student

Revision ID: 5f6a7b8c9d0e
Revises: 4e5f6a7b8c9d
Create Date: 2026-09-11 16:30:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "5f6a7b8c9d0e"
down_revision = "4e5f6a7b8c9d"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("donations", schema=None) as batch_op:
        batch_op.add_column(sa.Column("bace_student_id", sa.Integer(), nullable=True))
        batch_op.create_index("ix_donations_bace_student_id", ["bace_student_id"])
        batch_op.create_foreign_key(
            "fk_donations_bace_student_id", "bace_students", ["bace_student_id"], ["id"]
        )


def downgrade():
    with op.batch_alter_table("donations", schema=None) as batch_op:
        batch_op.drop_constraint("fk_donations_bace_student_id", type_="foreignkey")
        batch_op.drop_index("ix_donations_bace_student_id")
        batch_op.drop_column("bace_student_id")
