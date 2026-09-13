"""Historical marker for the removed Zoho queue migration.

Revision ID: a6f0b2c9d4e1
Revises: 5f6a7b8c9d0e

The live database has already recorded this revision.  Keeping this no-op
marker lets Alembic follow that history after the Zoho queue feature was
removed; it creates no table in new installations.
"""

revision = "a6f0b2c9d4e1"
down_revision = "5f6a7b8c9d0e"
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
