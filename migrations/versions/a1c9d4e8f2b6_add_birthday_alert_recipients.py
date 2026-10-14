"""add birthday alert recipients

Revision ID: a1c9d4e8f2b6
Revises: 9a2f7c4e8b31
Create Date: 2026-09-20
"""
from alembic import op
import sqlalchemy as sa

revision = "a1c9d4e8f2b6"
down_revision = "9a2f7c4e8b31"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "birthday_alert_recipients",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("contact_type", sa.String(length=20), nullable=False),
        sa.Column("value", sa.String(length=150), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("contact_type", "value", name="uq_birthday_alert_recipient_type_value"),
    )


def downgrade():
    op.drop_table("birthday_alert_recipients")
