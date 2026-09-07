"""Add zoho_forms.link_name

The name Zoho's own API knows a form by, which is not always the name
Razorpay carries in the payment notes. form_key is Razorpay's; link_name
is Zoho's. They are usually identical, so this is nullable and callers
fall back to form_key -- a form only needs it filled in when the two
genuinely differ.

Needed because reconciliation now asks Zoho's API for the donor's name
when no locally-recorded call carries the transaction ID. That covers
payments older than the local record and forms whose webhook was never
configured to send an ID -- between them, every payment currently sitting
unreceipted on the report.

Revision ID: b6c30f8a2e91
Revises: a9f24b6d1e37
"""
import sqlalchemy as sa
from alembic import op

revision = "b6c30f8a2e91"
down_revision = "a9f24b6d1e37"
branch_labels = None
depends_on = None


def upgrade():
    # batch_alter_table so this works on SQLite too, which the test suite
    # and any local copy run on -- plain ALTER TABLE ADD COLUMN is fine on
    # Postgres but the downgrade's DROP COLUMN is not supported by older
    # SQLite, and batch mode handles both by rebuilding the table.
    with op.batch_alter_table("zoho_forms", schema=None) as batch_op:
        batch_op.add_column(sa.Column("link_name", sa.String(length=150), nullable=True))


def downgrade():
    with op.batch_alter_table("zoho_forms", schema=None) as batch_op:
        batch_op.drop_column("link_name")
