"""Drop zoho_submissions, along with the Zoho webhook that filled it

The webhook is gone. It could never do the one job it was there for:
Zoho Forms fires it once, at submission time, *before* the donor pays, so
that call carries no transaction ID, and the follow-up call Zoho's docs
promise repeatedly never arrived on this account. It also had to be
configured per form, and four of the six forms taking money were never
configured at all -- so their donations were invisible.

zoho_submissions was the local mirror of those calls, kept so a later
payment could be matched to a donor by transaction ID. Reconciliation now
asks Zoho's API for that instead (public._name_from_zoho_api), on the same
key. The API knows every form's entries regardless of webhook
configuration and regardless of how old the payment is, which the mirror
could not: it only ever held calls received after the day it was deployed.

Nothing is lost with the table. It never decided entitlement -- Razorpay
did, and still does -- and every donation it ever contributed to is
already a row in donations with its own receipt number.

Revision ID: c8f42a1b9d63
Revises: b6c30f8a2e91
"""
import sqlalchemy as sa
from alembic import op

revision = "c8f42a1b9d63"
down_revision = "b6c30f8a2e91"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_table("zoho_submissions")


def downgrade():
    """Recreates the table empty. The rows are not recoverable, which is
    fine: it was a log, never a source of truth."""
    op.create_table(
        "zoho_submissions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_param", sa.String(length=150), nullable=True),
        sa.Column("campaign_id", sa.Integer(), nullable=True),
        sa.Column("full_name", sa.String(length=200), nullable=True),
        sa.Column("phone_normalized", sa.String(length=20), nullable=True),
        sa.Column("amount", sa.Float(), nullable=True),
        sa.Column("transaction_id", sa.String(length=100), nullable=True),
        sa.Column("payment_status", sa.String(length=50), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_zoho_submissions_phone_normalized", "zoho_submissions",
                    ["phone_normalized"], unique=False)
    op.create_index("ix_zoho_submissions_transaction_id", "zoho_submissions",
                    ["transaction_id"], unique=False)
    op.create_index("ix_zoho_submissions_received_at", "zoho_submissions",
                    ["received_at"], unique=False)
