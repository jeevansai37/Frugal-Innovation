"""Add permanent complaint activity history without backfilling legacy records.

Revision ID: 0003_complaint_history
Revises: 0002_complaint_staff_updates
"""
from alembic import op
import sqlalchemy as sa


revision = "0003_complaint_history"
down_revision = "0002_complaint_staff_updates"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "complaint_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("complaint_id", sa.String(length=50), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("previous_status", sa.String(length=32), nullable=True),
        sa.Column("new_status", sa.String(length=32), nullable=True),
        sa.Column("remarks", sa.Text(), nullable=True),
        sa.Column("actor_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_key", sa.String(length=96), nullable=True),
        sa.CheckConstraint(
            "event_type IN ('submitted', 'status_changed', 'staff_update', 'resolved')",
            name="ck_complaint_history_event_type",
        ),
        sa.ForeignKeyConstraint(
            ["complaint_id"], ["complaints.complaint_id"], ondelete="RESTRICT",
            name="fk_complaint_history_complaint",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.user_id"], ondelete="RESTRICT",
            name="fk_complaint_history_actor",
        ),
        sa.UniqueConstraint("request_key", name="uq_complaint_history_request_key"),
    )
    op.create_index(
        "ix_complaint_history_complaint_time",
        "complaint_history", ["complaint_id", "created_at", "id"],
    )


def downgrade():
    raise RuntimeError(
        "Complaint history is append-only and forward-only; do not remove recorded events."
    )
