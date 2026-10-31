from __future__ import annotations

from datetime import datetime, timezone
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.types import TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now():
    return datetime.now(timezone.utc)


def new_id():
    return str(uuid.uuid4())


class UTCDateTime(TypeDecorator):
    """Keep UTC offsets round-trippable on SQLite and use timestamptz on PostgreSQL."""

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(40))
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.astimezone(timezone.utc)
        return value.isoformat(timespec="microseconds") if dialect.name == "sqlite" else value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("account_kind IN ('student', 'staff')", name="ck_users_account_kind"),
        CheckConstraint(
            "status IN ('invited', 'active', 'suspended', 'disabled')",
            name="ck_users_status",
        ),
        UniqueConstraint("normalized_email", name="uq_users_normalized_email"),
    )

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    normalized_email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    password_hash: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="invited")
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    session_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Role(Base):
    __tablename__ = "roles"

    role_key: Mapped[str] = mapped_column(String(48), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    is_system_role: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Permission(Base):
    __tablename__ = "permissions"

    permission_key: Mapped[str] = mapped_column(String(96), primary_key=True)
    description: Mapped[str] = mapped_column(String(240), nullable=False)


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role_key: Mapped[str] = mapped_column(
        ForeignKey("roles.role_key", ondelete="RESTRICT"), primary_key=True
    )
    permission_key: Mapped[str] = mapped_column(
        ForeignKey("permissions.permission_key", ondelete="RESTRICT"), primary_key=True
    )


class Department(Base):
    __tablename__ = "departments"
    __table_args__ = (UniqueConstraint("department_code", name="uq_departments_code"),)

    department_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    department_code: Mapped[str] = mapped_column(String(48), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class StaffRoleAssignment(Base):
    __tablename__ = "staff_role_assignments"
    __table_args__ = (
        CheckConstraint(
            "(role_key IN ('SUPER_ADMIN', 'MAINTENANCE_HEAD') AND department_id IS NULL) "
            "OR (role_key IN ('DEPARTMENT_SUPERVISOR', 'TECHNICIAN') AND department_id IS NOT NULL)",
            name="ck_staff_assignment_department_scope",
        ),
        Index(
            "uq_staff_assignment_active_scope",
            "user_id",
            "role_key",
            "department_id",
            unique=True,
            sqlite_where=text("revoked_at IS NULL AND department_id IS NOT NULL"),
            postgresql_where=text("revoked_at IS NULL AND department_id IS NOT NULL"),
        ),
        Index(
            "uq_staff_assignment_active_global",
            "user_id",
            "role_key",
            unique=True,
            sqlite_where=text("revoked_at IS NULL AND department_id IS NULL"),
            postgresql_where=text("revoked_at IS NULL AND department_id IS NULL"),
        ),
    )

    assignment_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id", ondelete="RESTRICT"), nullable=False)
    role_key: Mapped[str] = mapped_column(ForeignKey("roles.role_key", ondelete="RESTRICT"), nullable=False)
    department_id: Mapped[str | None] = mapped_column(
        ForeignKey("departments.department_id", ondelete="RESTRICT")
    )
    assigned_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.user_id", ondelete="RESTRICT"))
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthToken(Base):
    __tablename__ = "auth_tokens"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('invitation', 'password_reset')", name="ck_auth_tokens_purpose"
        ),
        Index("ix_auth_tokens_user_purpose", "user_id", "purpose"),
    )

    token_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id", ondelete="RESTRICT"), nullable=False)
    purpose: Mapped[str] = mapped_column(String(24), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.user_id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_subject_time", "subject_type", "subject_id", "occurred_at"),
        Index("ix_audit_logs_actor_time", "actor_user_id", "occurred_at"),
    )

    audit_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.user_id", ondelete="RESTRICT"))
    action: Mapped[str] = mapped_column(String(96), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False, default="success")
    request_id: Mapped[str | None] = mapped_column(String(128))
    details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class LoginAttemptBucket(Base):
    __tablename__ = "login_attempt_buckets"

    bucket_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class BootstrapClaim(Base):
    """Unique transaction claim ensuring the initial Super Admin is created once."""

    __tablename__ = "bootstrap_claims"

    claim_key: Mapped[str] = mapped_column(String(48), primary_key=True)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class ComplaintHistory(Base):
    """Append-only, student-safe complaint activity events."""

    __tablename__ = "complaint_history"
    __table_args__ = (
        UniqueConstraint("request_key", name="uq_complaint_history_request_key"),
        Index("ix_complaint_history_complaint_time", "complaint_id", "created_at", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # The legacy complaints table remains outside this metadata; its FK is declared
    # explicitly in revision 0003 so SQLAlchemy never tries to recreate that table.
    complaint_id: Mapped[str] = mapped_column(String(50), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    previous_status: Mapped[str | None] = mapped_column(String(32))
    new_status: Mapped[str | None] = mapped_column(String(32))
    remarks: Mapped[str | None] = mapped_column(Text)
    actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.user_id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    request_key: Mapped[str | None] = mapped_column(String(96))

