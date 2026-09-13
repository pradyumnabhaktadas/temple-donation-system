"""add short-lived durable Zoho Forms reconciliation queue

Revision ID: a6f0b2c9d4e1
Revises: 5f6a7b8c9d0e
Create Date: 2026-09-13 11:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "a6f0b2c9d4e1"
down_revision = "5f6a7b8c9d0e"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "zoho_entry_queue",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("form_key", sa.String(length=150), nullable=False),
        sa.Column("entry_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("zoho_entry_id", sa.String(length=150), nullable=True),
        sa.Column("razorpay_payment_id", sa.String(length=100), nullable=True),
        sa.Column("razorpay_order_id", sa.String(length=100), nullable=True),
        sa.Column("full_name", sa.String(length=200), nullable=True),
        sa.Column("phone", sa.String(length=30), nullable=True),
        sa.Column("email", sa.String(length=200), nullable=True),
        sa.Column("pan", sa.String(length=20), nullable=True),
        sa.Column("amount", sa.String(length=40), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=True),
        sa.Column("stored_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("last_reason", sa.String(length=500), nullable=True),
        sa.Column("checked_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("payload_purged_at", sa.DateTime(), nullable=True),
        sa.Column("donation_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["donation_id"], ["donations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("entry_fingerprint"),
    )
    op.create_index("ix_zoho_entry_queue_form_key", "zoho_entry_queue", ["form_key"])
    op.create_index("ix_zoho_entry_queue_zoho_entry_id", "zoho_entry_queue", ["zoho_entry_id"])
    op.create_index("ix_zoho_entry_queue_razorpay_payment_id", "zoho_entry_queue", ["razorpay_payment_id"])
    op.create_index("ix_zoho_entry_queue_status", "zoho_entry_queue", ["status"])


def downgrade():
    op.drop_table("zoho_entry_queue")
