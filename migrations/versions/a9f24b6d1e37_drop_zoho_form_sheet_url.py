"""drop zoho_forms.sheet_csv_url

Revision ID: a9f24b6d1e37
Revises: f5d18e3c7a24
Create Date: 2026-09-07 14:00:00.000000

The sheet held Zoho's submissions but not their transaction IDs, so it
could only ever be matched to a Razorpay payment by phone number and
amount. That inference is where every hard case came from -- a donor with
two identical submissions, a shared family phone, a payment made from a
different number than the form carried -- and a guess that looks like a
match is worse than no match, because it produces a tax receipt in the
wrong name and nobody notices.

Matching is now on the transaction ID alone, against this app's own record
of Zoho's calls (zoho_submissions, f5d18e3c7a24). Nothing reads a sheet
any more, so the column goes rather than sitting there inviting someone to
wire it back up.

batch_alter_table because SQLite (local/dev) cannot drop a column in
place; on Postgres it is an ordinary ALTER.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a9f24b6d1e37'
down_revision = 'f5d18e3c7a24'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('zoho_forms') as batch_op:
        batch_op.drop_column('sheet_csv_url')


def downgrade():
    with op.batch_alter_table('zoho_forms') as batch_op:
        batch_op.add_column(sa.Column('sheet_csv_url', sa.String(length=600), nullable=True))
