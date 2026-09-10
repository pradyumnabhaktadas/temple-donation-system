"""add bace_students and bace_rent_payments tables

Revision ID: d4b81e6c3f92
Revises: c8f42a1b9d63
Create Date: 2026-09-10 12:00:00.000000

Replaces the "BACE Rent Contribution Tracker" spreadsheet's Students and
Payments Log tabs. Deliberately separate from Donation/the public
/bace-rent flow -- that form only tags a payment to a property, never to
a specific student or month, which is exactly what's needed to compute
who's paid/pending each month. See the BaceStudent/BaceRentPayment model
docstrings for the rest.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd4b81e6c3f92'
down_revision = 'c8f42a1b9d63'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'bace_students',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('full_name', sa.String(length=200), nullable=False),
        sa.Column('phone', sa.String(length=20), nullable=True),
        sa.Column('bace_property_id', sa.Integer(), nullable=False),
        sa.Column('room_notes', sa.String(length=200), nullable=True),
        sa.Column('monthly_amount', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('joined_month', sa.Date(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='Active'),
        sa.Column('notes', sa.String(length=300), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['bace_property_id'], ['bace_properties.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_bace_students_phone', 'bace_students', ['phone'])

    op.create_table(
        'bace_rent_payments',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('student_id', sa.Integer(), nullable=False),
        sa.Column('for_month', sa.Date(), nullable=False),
        sa.Column('amount_paid', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('date_paid', sa.Date(), nullable=False),
        sa.Column('mode', sa.String(length=30), nullable=True),
        sa.Column('recorded_by', sa.String(length=100), nullable=True),
        sa.Column('reference', sa.String(length=200), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['student_id'], ['bace_students.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_bace_rent_payments_student_id', 'bace_rent_payments', ['student_id'])
    op.create_index('ix_bace_rent_payments_for_month', 'bace_rent_payments', ['for_month'])


def downgrade():
    op.drop_index('ix_bace_rent_payments_for_month', table_name='bace_rent_payments')
    op.drop_index('ix_bace_rent_payments_student_id', table_name='bace_rent_payments')
    op.drop_table('bace_rent_payments')
    op.drop_index('ix_bace_students_phone', table_name='bace_students')
    op.drop_table('bace_students')
