"""Add a unique database claim for one-time initial administrator bootstrap.

Revision ID: 0004_bootstrap_claims
Revises: 0003_complaint_history
"""
from alembic import op
import sqlalchemy as sa


revision = "0004_bootstrap_claims"
down_revision = "0003_complaint_history"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "bootstrap_claims",
        sa.Column("claim_key", sa.String(length=48), primary_key=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    raise RuntimeError(
        "Bootstrap claims are security state and forward-only; do not remove them."
    )
