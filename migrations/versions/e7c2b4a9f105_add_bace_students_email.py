"""add bace_students.email

Revision ID: e7c2b4a9f105
Revises: d4b81e6c3f92
Create Date: 2026-09-10 13:00:00.000000

Lets a student be found by email too, not just phone -- Payments Log's
search, and matching a public BACE Contribution donation (public
/bace-rent) back to this roster on Admin -> BACE Contribution Logs.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e7c2b4a9f105'
down_revision = 'd4b81e6c3f92'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('bace_students', sa.Column('email', sa.String(length=200), nullable=True))
    op.create_index('ix_bace_students_email', 'bace_students', ['email'])


def downgrade():
    op.drop_index('ix_bace_students_email', table_name='bace_students')
    op.drop_column('bace_students', 'email')
