from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import os
import re
import secrets

from sqlalchemy import case, delete, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from .audit import record_audit_event
from .models import (
    AuthToken, BootstrapClaim, Department, LoginAttemptBucket, Role,
    StaffRoleAssignment, User,
)
from .permissions import GLOBAL_ROLES, is_super_admin, validate_role_scope


PASSWORD_HASH_METHOD = "scrypt:32768:8:1"
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 128
LOGIN_FAILURE_LIMIT = 5
LOGIN_WINDOW = timedelta(minutes=15)
INVITATION_LIFETIME = timedelta(hours=48)
PASSWORD_RESET_LIFETIME = timedelta(minutes=30)
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+$")


class DuplicateEmail(ValueError):
    pass


class InvalidToken(ValueError):
    pass


def utc_now():
    return datetime.now(timezone.utc)


def normalize_email(email, allowed_domains=None):
    if not isinstance(email, str):
        raise ValueError("Enter a valid institutional email address.")
    value = email.strip()
    if len(value) > 320 or not EMAIL_RE.fullmatch(value):
        raise ValueError("Enter a valid institutional email address.")
    local, domain = value.rsplit("@", 1)
    if len(local) > 64 or not local or not domain:
        raise ValueError("Enter a valid institutional email address.")
    try:
        normalized_domain = domain.encode("idna").decode("ascii").casefold()
    except UnicodeError as exc:
        raise ValueError("Enter a valid institutional email address.") from exc
    normalized = f"{local.casefold()}@{normalized_domain}"
    if allowed_domains is not None:
        allowed = {item.strip().lstrip("@").encode("idna").decode("ascii").casefold()
                   for item in allowed_domains if item and item.strip()}
        if not allowed or normalized_domain not in allowed:
            raise ValueError("Use an approved institutional email domain.")
    return normalized


def configured_institutional_email_domains():
    return {
        item.strip().lstrip("@").casefold()
        for item in os.environ.get("INSTITUTIONAL_EMAIL_DOMAINS", "").split(",")
        if item.strip()
    }


def hash_password(password):
    _validate_password(password)
    return generate_password_hash(password, method=PASSWORD_HASH_METHOD)


def _validate_password(password):
    if not isinstance(password, str) or not PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH:
        raise ValueError(f"Password must be between {PASSWORD_MIN_LENGTH} and {PASSWORD_MAX_LENGTH} characters.")


def _hash_token(raw_token):
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _new_token():
    raw = secrets.token_urlsafe(32)
    return raw, _hash_token(raw)


def create_staff_invitation(
    db,
    *,
    actor_user_id,
    email,
    role_key,
    department_id=None,
    allowed_domains,
    now=None,
    lifetime=INVITATION_LIFETIME,
):
    """Create an invite record and return the raw token once; no email is sent."""
    now = now or utc_now()
    normalized_email = normalize_email(email, allowed_domains)
    actor = db.get(User, actor_user_id)
    if not is_super_admin(db, actor):
        raise PermissionError("Only an active Super Admin may invite staff.")
    validate_role_scope(db, role_key, department_id)
    if role_key == "SUPER_ADMIN" and department_id is not None:
        raise ValueError("Super Admin assignments are global.")
    if department_id is not None:
        department = db.get(Department, department_id)
        if department is None or not department.active:
            raise ValueError("Choose an active department.")

    raw_token, token_hash = _new_token()
    user = User(
        account_kind="staff",
        email=email.strip(),
        normalized_email=normalized_email,
        status="invited",
        email_verified_at=None,
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise DuplicateEmail("An account already uses this institutional email.") from exc

    assignment = StaffRoleAssignment(
        user_id=user.user_id,
        role_key=role_key,
        department_id=department_id,
        assigned_by_user_id=actor_user_id,
        assigned_at=now,
    )
    token = AuthToken(
        user_id=user.user_id,
        purpose="invitation",
        token_hash=token_hash,
        created_by_user_id=actor_user_id,
        created_at=now,
        expires_at=now + lifetime,
    )
    db.add_all([assignment, token])
    record_audit_event(
        db,
        actor_user_id=actor_user_id,
        action="staff.invitation_created",
        subject_type="user",
        subject_id=user.user_id,
        details={"role_key": role_key, "department_id": department_id},
        occurred_at=now,
    )
    db.flush()
    return user, raw_token


def bootstrap_first_super_admin(db, *, email, password, allowed_domains, now=None):
    """Create the first administrator through an operator-only CLI, never a web route."""
    now = now or utc_now()
    normalized_email = normalize_email(email, allowed_domains)
    password_hash = hash_password(password)

    # The unique claim is acquired before the existence check and commits atomically
    # with the initial account. Concurrent PostgreSQL/SQLite transactions serialize
    # on this primary key; only one can bootstrap successfully.
    try:
        db.execute(insert(BootstrapClaim).values(claim_key="initial_super_admin", claimed_at=now))
    except IntegrityError as exc:
        db.rollback()
        raise PermissionError("A Super Admin already exists; bootstrap is disabled.") from exc

    if db.scalar(select(StaffRoleAssignment.assignment_id).where(
        StaffRoleAssignment.role_key == "SUPER_ADMIN",
        StaffRoleAssignment.revoked_at.is_(None),
    )) is not None:
        db.rollback()
        raise PermissionError("A Super Admin already exists; bootstrap is disabled.")
    try:
        user = User(
            account_kind="staff",
            email=email.strip(),
            normalized_email=normalized_email,
            display_name="Super Admin",
            password_hash=password_hash,
            status="active",
            email_verified_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(user)
        db.flush()
        db.add(StaffRoleAssignment(
            user_id=user.user_id,
            role_key="SUPER_ADMIN",
            department_id=None,
            assigned_by_user_id=user.user_id,
            assigned_at=now,
        ))
        record_audit_event(
            db,
            actor_user_id=user.user_id,
            action="staff.super_admin_bootstrapped",
            subject_type="user",
            subject_id=user.user_id,
            occurred_at=now,
        )
        db.commit()
        return user
    except Exception:
        db.rollback()
        raise


def _consume_token(db, raw_token, purpose, now):
    if not isinstance(raw_token, str) or not raw_token or len(raw_token) > 200:
        return None
    statement = (
        update(AuthToken)
        .where(
            AuthToken.token_hash == _hash_token(raw_token),
            AuthToken.purpose == purpose,
            AuthToken.consumed_at.is_(None),
            AuthToken.expires_at > now,
        )
        .values(consumed_at=now)
    )
    result = db.execute(statement)
    if result.rowcount != 1:
        return None
    token_hash = _hash_token(raw_token)
    return db.scalar(select(AuthToken).where(AuthToken.token_hash == token_hash))


def redeem_invitation(db, raw_token, password, *, now=None):
    now = now or utc_now()
    _validate_password(password)
    token = _consume_token(db, raw_token, "invitation", now)
    if token is None:
        db.rollback()
        raise InvalidToken("Invitation is invalid, expired, or already used.")
    user = db.get(User, token.user_id)
    if user is None or user.account_kind != "staff" or user.status != "invited" or user.email_verified_at:
        db.rollback()
        raise InvalidToken("Invitation is invalid, expired, or already used.")
    password_hash = hash_password(password)
    user.password_hash = password_hash
    user.status = "active"
    # The invitation is a one-time capability addressed to this email. Actual delivery is deferred.
    user.email_verified_at = now
    user.updated_at = now
    record_audit_event(
        db,
        actor_user_id=user.user_id,
        action="staff.invitation_redeemed",
        subject_type="user",
        subject_id=user.user_id,
        occurred_at=now,
    )
    db.commit()
    return user


def create_password_reset_token(db, email, *, allowed_domains, now=None, lifetime=PASSWORD_RESET_LIFETIME):
    """Create a reset token for a future approved delivery adapter; sends nothing."""
    now = now or utc_now()
    try:
        normalized = normalize_email(email, allowed_domains)
    except ValueError:
        return None
    user = db.scalar(select(User).where(User.normalized_email == normalized))
    if (user is None or user.account_kind != "staff" or user.status != "active"
            or user.email_verified_at is None):
        return None
    raw_token, token_hash = _new_token()
    db.add(AuthToken(
        user_id=user.user_id,
        purpose="password_reset",
        token_hash=token_hash,
        created_at=now,
        expires_at=now + lifetime,
    ))
    record_audit_event(
        db,
        actor_user_id=user.user_id,
        action="staff.password_reset_token_created",
        subject_type="user",
        subject_id=user.user_id,
        occurred_at=now,
    )
    db.commit()
    return raw_token


def redeem_password_reset(db, raw_token, new_password, *, now=None):
    now = now or utc_now()
    _validate_password(new_password)
    token = _consume_token(db, raw_token, "password_reset", now)
    if token is None:
        db.rollback()
        raise InvalidToken("Password reset token is invalid, expired, or already used.")
    user = db.get(User, token.user_id)
    if (user is None or user.account_kind != "staff" or user.status != "active"
            or user.email_verified_at is None):
        db.rollback()
        raise InvalidToken("Password reset token is invalid, expired, or already used.")
    password_hash = hash_password(new_password)
    user.password_hash = password_hash
    user.session_version += 1
    user.updated_at = now
    db.execute(
        update(AuthToken)
        .where(
            AuthToken.user_id == user.user_id,
            AuthToken.purpose == "password_reset",
            AuthToken.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )
    record_audit_event(
        db,
        actor_user_id=user.user_id,
        action="staff.password_reset_completed",
        subject_type="user",
        subject_id=user.user_id,
        occurred_at=now,
    )
    db.commit()
    return user


def _rate_limit_key(email, remote_addr, secret):
    try:
        normalized = normalize_email(email)
    except ValueError:
        normalized = str(email).strip().casefold()[:320]
    scope = f"{normalized}\0{remote_addr or 'unknown'}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), scope, hashlib.sha256).hexdigest()


def login_is_blocked(db, email, remote_addr, *, rate_limit_secret, now=None):
    now = now or utc_now()
    key = _rate_limit_key(email, remote_addr, rate_limit_secret)
    bucket = db.get(LoginAttemptBucket, key)
    if bucket is None:
        return False
    blocked_until = bucket.blocked_until
    if blocked_until is not None and blocked_until.tzinfo is None:
        blocked_until = blocked_until.replace(tzinfo=timezone.utc)
    return blocked_until is not None and blocked_until > now


def record_login_failure(
    db,
    email,
    remote_addr,
    *,
    rate_limit_secret,
    now=None,
    limit=LOGIN_FAILURE_LIMIT,
    window=LOGIN_WINDOW,
):
    """Atomically update the shared DB bucket; separate app instances see the same count."""
    now = now or utc_now()
    key = _rate_limit_key(email, remote_addr, rate_limit_secret)
    cutoff = now - window
    db.execute(delete(LoginAttemptBucket).where(LoginAttemptBucket.updated_at <= cutoff))
    table = LoginAttemptBucket.__table__
    values = {
        "bucket_key": key,
        "window_started_at": now,
        "attempts": 1,
        "blocked_until": now + window if limit <= 1 else None,
        "updated_at": now,
    }
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        insert = sqlite_insert(LoginAttemptBucket)
    elif dialect == "postgresql":
        insert = pg_insert(LoginAttemptBucket)
    else:
        raise RuntimeError(f"Unsupported database dialect for shared login throttling: {dialect}")
    expired = table.c.window_started_at <= cutoff
    next_attempts = case((expired, 1), else_=table.c.attempts + 1)
    statement = insert.values(**values).on_conflict_do_update(
        index_elements=[table.c.bucket_key],
        set_={
            "window_started_at": case((expired, now), else_=table.c.window_started_at),
            "attempts": next_attempts,
            "blocked_until": case(
                (expired, None),
                (next_attempts >= limit, now + window),
                else_=table.c.blocked_until,
            ),
            "updated_at": now,
        },
    )
    db.execute(statement)
    db.commit()
    return login_is_blocked(db, email, remote_addr, rate_limit_secret=rate_limit_secret, now=now)


def clear_login_failures(db, email, remote_addr, *, rate_limit_secret):
    key = _rate_limit_key(email, remote_addr, rate_limit_secret)
    db.execute(delete(LoginAttemptBucket).where(LoginAttemptBucket.bucket_key == key))
    db.commit()


def has_active_staff_assignment(db, user_id):
    return db.scalar(
        select(StaffRoleAssignment.assignment_id)
        .join(Role, Role.role_key == StaffRoleAssignment.role_key)
        .outerjoin(Department, Department.department_id == StaffRoleAssignment.department_id)
        .where(
            StaffRoleAssignment.user_id == user_id,
            StaffRoleAssignment.revoked_at.is_(None),
            or_(StaffRoleAssignment.expires_at.is_(None), StaffRoleAssignment.expires_at > utc_now()),
            Role.active.is_(True),
            or_(StaffRoleAssignment.role_key.in_(GLOBAL_ROLES), Department.active.is_(True)),
        )
        .limit(1)
    ) is not None


def authenticate_staff(
    db,
    email,
    password,
    remote_addr,
    *,
    rate_limit_secret,
    now=None,
):
    now = now or utc_now()
    try:
        normalized = normalize_email(email)
    except ValueError:
        normalized = str(email).strip().casefold()[:320]
    if login_is_blocked(db, normalized, remote_addr, rate_limit_secret=rate_limit_secret, now=now):
        return None
    user = db.scalar(select(User).where(User.normalized_email == normalized))
    valid_account = (
        user is not None
        and user.account_kind == "staff"
        and user.status == "active"
        and user.email_verified_at is not None
        and bool(user.password_hash)
        and has_active_staff_assignment(db, user.user_id)
    )
    password_matches = (
        check_password_hash(user.password_hash, password)
        if valid_account else False
    )
    if not valid_account or not password_matches:
        record_login_failure(
            db, normalized, remote_addr,
            rate_limit_secret=rate_limit_secret, now=now,
        )
        if user is not None:
            from .audit import record_audit_event
            record_audit_event(
                db,
                actor_user_id=user.user_id,
                action="staff.login_failed",
                subject_type="user",
                subject_id=user.user_id,
                outcome="denied",
                occurred_at=now,
            )
            db.commit()
        return None
    user.last_login_at = now
    db.commit()
    clear_login_failures(db, normalized, remote_addr, rate_limit_secret=rate_limit_secret)
    return user


def session_claims(user):
    return {"user_id": user.user_id, "session_version": user.session_version}


def resolve_session_user(db, claims):
    if not isinstance(claims, dict):
        return None
    user_id = claims.get("user_id")
    version = claims.get("session_version")
    if not isinstance(user_id, str) or not isinstance(version, int):
        return None
    user = db.get(User, user_id)
    if (user is None or user.status != "active" or user.email_verified_at is None
            or user.session_version != version):
        return None
    if user.account_kind == "staff" and not has_active_staff_assignment(db, user.user_id):
        return None
    return user


def revoke_sessions(db, user_id, *, now=None):
    user = db.get(User, user_id)
    if user is None:
        return False
    user.session_version += 1
    user.updated_at = now or utc_now()
    db.commit()
    return True

