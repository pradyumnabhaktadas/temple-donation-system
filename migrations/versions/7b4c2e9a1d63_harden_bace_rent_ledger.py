"""harden BACE rent history, close controls and viewer scopes

Revision ID: 7b4c2e9a1d63
Revises: 5f6a7b8c9d0e, a6f0b2c9d4e1
Create Date: 2026-09-13 17:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "7b4c2e9a1d63"
# a6f0b2c9d4e1 is a no-op historical marker retained for databases that
# briefly received the removed Zoho queue migration.  Merging it here keeps
# both production histories valid and gives Alembic one unambiguous head.
down_revision = ("5f6a7b8c9d0e", "a6f0b2c9d4e1")
branch_labels = None
depends_on = None


def upgrade():
    # batch mode keeps the migration portable to local SQLite and Render
    # Postgres. Existing rows are backfilled from the student roster; new
    # rows always retain a property snapshot at creation time.
    with op.batch_alter_table("bace_rent_charges", schema=None) as batch_op:
        batch_op.add_column(sa.Column("bace_property_id", sa.Integer(), nullable=True))
        batch_op.create_index("ix_bace_rent_charges_bace_property_id", ["bace_property_id"])
        batch_op.create_foreign_key(
            "fk_bace_rent_charges_bace_property_id", "bace_properties",
            ["bace_property_id"], ["id"],
        )
    with op.batch_alter_table("bace_rent_payments", schema=None) as batch_op:
        batch_op.add_column(sa.Column("bace_property_id", sa.Integer(), nullable=True))
        batch_op.create_index("ix_bace_rent_payments_bace_property_id", ["bace_property_id"])
        batch_op.create_foreign_key(
            "fk_bace_rent_payments_bace_property_id", "bace_properties",
            ["bace_property_id"], ["id"],
        )
    with op.batch_alter_table("admin_users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("bace_property_id", sa.Integer(), nullable=True))
        batch_op.create_index("ix_admin_users_bace_property_id", ["bace_property_id"])
        batch_op.create_foreign_key(
            "fk_admin_users_bace_property_id", "bace_properties",
            ["bace_property_id"], ["id"],
        )

    # Correlated subqueries work on both Postgres and SQLite.
    op.execute("""
        UPDATE bace_rent_charges
        SET bace_property_id = (SELECT bace_property_id FROM bace_students
                                WHERE bace_students.id = bace_rent_charges.student_id)
        WHERE bace_property_id IS NULL
    """)
    op.execute("""
        UPDATE bace_rent_payments
        SET bace_property_id = (SELECT bace_property_id FROM bace_students
                                WHERE bace_students.id = bace_rent_payments.student_id)
        WHERE bace_property_id IS NULL
    """)
    op.create_table(
        "bace_rent_month_closes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("for_month", sa.Date(), nullable=False),
        sa.Column("closed_at", sa.DateTime(), nullable=False),
        sa.Column("closed_by", sa.String(length=100), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("for_month"),
    )
    op.create_index("ix_bace_rent_month_closes_for_month", "bace_rent_month_closes", ["for_month"])


def downgrade():
    op.drop_index("ix_bace_rent_month_closes_for_month", table_name="bace_rent_month_closes")
    op.drop_table("bace_rent_month_closes")
    with op.batch_alter_table("admin_users", schema=None) as batch_op:
        batch_op.drop_constraint("fk_admin_users_bace_property_id", type_="foreignkey")
        batch_op.drop_index("ix_admin_users_bace_property_id")
        batch_op.drop_column("bace_property_id")
    with op.batch_alter_table("bace_rent_payments", schema=None) as batch_op:
        batch_op.drop_constraint("fk_bace_rent_payments_bace_property_id", type_="foreignkey")
        batch_op.drop_index("ix_bace_rent_payments_bace_property_id")
        batch_op.drop_column("bace_property_id")
    with op.batch_alter_table("bace_rent_charges", schema=None) as batch_op:
        batch_op.drop_constraint("fk_bace_rent_charges_bace_property_id", type_="foreignkey")
        batch_op.drop_index("ix_bace_rent_charges_bace_property_id")
        batch_op.drop_column("bace_property_id")
