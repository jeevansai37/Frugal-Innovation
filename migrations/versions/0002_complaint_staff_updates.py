"""Add public tracking fields for staff updates.

Revision ID: 0002_complaint_staff_updates
Revises: 0001_identity_foundation
"""
from alembic import op
import sqlalchemy as sa


revision = "0002_complaint_staff_updates"
down_revision = "0001_identity_foundation"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("complaints", sa.Column("last_updated_at", sa.Text(), nullable=True))
    op.add_column("complaints", sa.Column("staff_remarks", sa.Text(), nullable=True))


def downgrade():
    raise RuntimeError(
        "Complaint staff updates are forward-only; preserve remarks with a reviewed corrective migration."
    )
