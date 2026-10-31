"""Add the initial CampusCare identity and security schema.

This revision preserves an existing complaints table and its integer primary key.
It creates the legacy table only for a fresh local installation.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

from campuscare.permissions import PERMISSION_DESCRIPTIONS, ROLE_NAMES, ROLE_PERMISSIONS


revision = "0001_identity_foundation"
down_revision = None
branch_labels = None
depends_on = None


def _ensure_legacy_complaints_table():
    bind = op.get_bind()
    inspector = inspect(bind)
    if "complaints" not in inspector.get_table_names():
        op.create_table(
            "complaints",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("complaint_id", sa.Text(), nullable=False, unique=True),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("roll_number", sa.Text(), nullable=False, server_default=""),
            sa.Column("location", sa.Text(), nullable=False),
            sa.Column("category", sa.Text(), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("status", sa.Text(), nullable=False, server_default="Pending"),
            sa.Column("priority", sa.Text(), nullable=False, server_default="Medium"),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.Column("access_code_hash", sa.Text(), nullable=True),
        )
        return

    columns = {column["name"] for column in inspector.get_columns("complaints")}
    if "priority" not in columns:
        op.add_column(
            "complaints",
            sa.Column("priority", sa.Text(), nullable=False, server_default="Medium"),
        )
    if "roll_number" not in columns:
        op.add_column(
            "complaints",
            sa.Column("roll_number", sa.Text(), nullable=False, server_default=""),
        )
    if "access_code_hash" not in columns:
        op.add_column("complaints", sa.Column("access_code_hash", sa.Text(), nullable=True))


def upgrade():
    _ensure_legacy_complaints_table()

    op.create_table(
        "users",
        sa.Column("user_id", sa.String(36), primary_key=True),
        sa.Column("account_kind", sa.String(16), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("normalized_email", sa.String(320), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("password_hash", sa.String(512), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="invited"),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("session_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("account_kind IN ('student', 'staff')", name="ck_users_account_kind"),
        sa.CheckConstraint(
            "status IN ('invited', 'active', 'suspended', 'disabled')", name="ck_users_status"
        ),
        sa.UniqueConstraint("normalized_email", name="uq_users_normalized_email"),
    )
    op.create_table(
        "roles",
        sa.Column("role_key", sa.String(48), primary_key=True),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("is_system_role", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_table(
        "permissions",
        sa.Column("permission_key", sa.String(96), primary_key=True),
        sa.Column("description", sa.String(240), nullable=False),
    )
    op.create_table(
        "role_permissions",
        sa.Column("role_key", sa.String(48), sa.ForeignKey("roles.role_key", ondelete="RESTRICT"), primary_key=True),
        sa.Column("permission_key", sa.String(96), sa.ForeignKey("permissions.permission_key", ondelete="RESTRICT"), primary_key=True),
    )
    op.create_table(
        "departments",
        sa.Column("department_id", sa.String(36), primary_key=True),
        sa.Column("department_code", sa.String(48), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("department_code", name="uq_departments_code"),
    )
    op.create_table(
        "staff_role_assignments",
        sa.Column("assignment_id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.user_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("role_key", sa.String(48), sa.ForeignKey("roles.role_key", ondelete="RESTRICT"), nullable=False),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.department_id", ondelete="RESTRICT"), nullable=True),
        sa.Column("assigned_by_user_id", sa.String(36), sa.ForeignKey("users.user_id", ondelete="RESTRICT"), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(role_key IN ('SUPER_ADMIN', 'MAINTENANCE_HEAD') AND department_id IS NULL) "
            "OR (role_key IN ('DEPARTMENT_SUPERVISOR', 'TECHNICIAN') AND department_id IS NOT NULL)",
            name="ck_staff_assignment_department_scope",
        ),
    )
    op.create_index(
        "uq_staff_assignment_active_scope",
        "staff_role_assignments",
        ["user_id", "role_key", "department_id"],
        unique=True,
        sqlite_where=sa.text("revoked_at IS NULL AND department_id IS NOT NULL"),
        postgresql_where=sa.text("revoked_at IS NULL AND department_id IS NOT NULL"),
    )
    op.create_index(
        "uq_staff_assignment_active_global",
        "staff_role_assignments",
        ["user_id", "role_key"],
        unique=True,
        sqlite_where=sa.text("revoked_at IS NULL AND department_id IS NULL"),
        postgresql_where=sa.text("revoked_at IS NULL AND department_id IS NULL"),
    )
    op.create_table(
        "auth_tokens",
        sa.Column("token_id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.user_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("purpose", sa.String(24), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_by_user_id", sa.String(36), sa.ForeignKey("users.user_id", ondelete="RESTRICT"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("purpose IN ('invitation', 'password_reset')", name="ck_auth_tokens_purpose"),
    )
    op.create_index("ix_auth_tokens_user_purpose", "auth_tokens", ["user_id", "purpose"])
    op.create_table(
        "audit_logs",
        sa.Column("audit_id", sa.String(36), primary_key=True),
        sa.Column("actor_user_id", sa.String(36), sa.ForeignKey("users.user_id", ondelete="RESTRICT"), nullable=True),
        sa.Column("action", sa.String(96), nullable=False),
        sa.Column("subject_type", sa.String(64), nullable=False),
        sa.Column("subject_id", sa.String(128), nullable=False),
        sa.Column("outcome", sa.String(24), nullable=False, server_default="success"),
        sa.Column("request_id", sa.String(128), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_logs_subject_time", "audit_logs", ["subject_type", "subject_id", "occurred_at"])
    op.create_index("ix_audit_logs_actor_time", "audit_logs", ["actor_user_id", "occurred_at"])
    op.create_table(
        "login_attempt_buckets",
        sa.Column("bucket_key", sa.String(64), primary_key=True),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("blocked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    role_table = sa.table(
        "roles",
        sa.column("role_key", sa.String),
        sa.column("display_name", sa.String),
        sa.column("is_system_role", sa.Boolean),
        sa.column("active", sa.Boolean),
    )
    permission_table = sa.table(
        "permissions", sa.column("permission_key", sa.String), sa.column("description", sa.String)
    )
    role_permission_table = sa.table(
        "role_permissions", sa.column("role_key", sa.String), sa.column("permission_key", sa.String)
    )
    op.bulk_insert(role_table, [
        {"role_key": key, "display_name": name, "is_system_role": True, "active": True}
        for key, name in ROLE_NAMES.items()
    ])
    op.bulk_insert(permission_table, [
        {"permission_key": key, "description": description}
        for key, description in PERMISSION_DESCRIPTIONS.items()
    ])
    op.bulk_insert(role_permission_table, [
        {"role_key": role_key, "permission_key": permission_key}
        for role_key, permission_keys in ROLE_PERMISSIONS.items()
        for permission_key in sorted(permission_keys)
    ])

    department_table = sa.table(
        "departments",
        sa.column("department_id", sa.String),
        sa.column("department_code", sa.String),
        sa.column("name", sa.String),
        sa.column("active", sa.Boolean),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    import datetime
    department_rows = [
        ("00000000-0000-4000-8000-000000000001", "ELECTRICAL", "Electrical"),
        ("00000000-0000-4000-8000-000000000002", "CIVIL", "Civil"),
        ("00000000-0000-4000-8000-000000000003", "PLUMBING", "Plumbing"),
        ("00000000-0000-4000-8000-000000000004", "HOUSEKEEPING", "Housekeeping"),
        ("00000000-0000-4000-8000-000000000005", "NETWORK_IT", "Network/IT"),
        ("00000000-0000-4000-8000-000000000006", "OTHER", "Other"),
    ]
    op.bulk_insert(department_table, [
        {"department_id": identity, "department_code": code, "name": name,
         "active": True, "created_at": datetime.datetime.now(datetime.timezone.utc)}
        for identity, code, name in department_rows
    ])


def downgrade():
    raise RuntimeError(
        "CampusCare identity foundation is forward-only; use a reviewed corrective migration."
    )
