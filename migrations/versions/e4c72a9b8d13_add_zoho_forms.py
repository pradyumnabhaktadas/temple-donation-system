"""add zoho_forms table

Revision ID: e4c72a9b8d13
Revises: d8b41f7c3e56
Create Date: 2026-09-07 09:00:00.000000

Replaces the ZOHO_SHEET_CSV_URL / ZOHO_FORM_CAMPAIGNS environment
settings, which assumed one sheet for everything. Each Zoho form writes to
its own Google Sheet and belongs to its own campaign, and there are six of
them already, so this is a row per form managed from Admin -> Zoho Forms
instead of a semicolon-delimited string requiring a redeploy to change.

See the ZohoForm model's docstring for what each column is for.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e4c72a9b8d13'
down_revision = 'd8b41f7c3e56'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'zoho_forms',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('form_key', sa.String(length=150), nullable=False),
        sa.Column('display_name', sa.String(length=200), nullable=True),
        sa.Column('campaign_id', sa.Integer(), nullable=True),
        sa.Column('sheet_csv_url', sa.String(length=600), nullable=True),
        sa.Column('is_test', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('form_key', name='uq_zoho_forms_form_key'),
    )
    op.create_index('ix_zoho_forms_form_key', 'zoho_forms', ['form_key'])


def downgrade():
    op.drop_index('ix_zoho_forms_form_key', table_name='zoho_forms')
    op.drop_table('zoho_forms')
