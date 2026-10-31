from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
import tempfile
import unittest

from sqlalchemy import column, delete, select, table, update
from sqlalchemy.orm import aliased

from campuscare.database import engine_for_url, session_factory, sqlite_url
from campuscare.identity import (
    DuplicateEmail,
    InvalidToken,
    authenticate_staff,
    bootstrap_first_super_admin,
    create_password_reset_token,
    create_staff_invitation,
    hash_password,
    login_is_blocked,
    record_login_failure,
    redeem_invitation,
    redeem_password_reset,
    resolve_session_user,
    revoke_sessions,
    session_claims,
)
from campuscare.models import (
    AuditLog,
    BootstrapClaim,
    Department,
    LoginAttemptBucket,
    StaffRoleAssignment,
    User,
    utc_now,
)
from campuscare.permissions import has_permission, is_super_admin
from campuscare.audit import record_audit_event


class IdentityFoundationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "campuscare-test.db"
        from campuscare.migration_runner import upgrade_database
        upgrade_database(sqlite_url(self.database_path))
        self.engine_one = engine_for_url(sqlite_url(self.database_path))
        self.engine_two = engine_for_url(sqlite_url(self.database_path))
        self.sessions_one = session_factory(self.engine_one)
        self.sessions_two = session_factory(self.engine_two)
        self.now = utc_now()
        self.rate_limit_secret = "synthetic-test-key-do-not-use-in-production"
        with self.sessions_one() as db:
            self.admin = User(
                account_kind="staff",
                email="admin@iiitdwd.example",
                normalized_email="admin@iiitdwd.example",
                display_name="Synthetic Admin",
                password_hash=hash_password("synthetic-admin-password-1"),
                status="active",
                email_verified_at=self.now,
                created_at=self.now,
                updated_at=self.now,
            )
            db.add(self.admin)
            db.flush()
            db.add(StaffRoleAssignment(
                user_id=self.admin.user_id,
                role_key="SUPER_ADMIN",
                department_id=None,
                assigned_at=self.now,
            ))
            db.commit()
            self.admin_id = self.admin.user_id
            self.department_id = db.scalar(
                select(Department.department_id).where(Department.department_code == "ELECTRICAL")
            )

    def tearDown(self):
        self.engine_one.dispose()
        self.engine_two.dispose()
        self.temp_dir.cleanup()

    def invite(self, email="tech@iiitdwd.example", role_key="TECHNICIAN", department_id=None):
        with self.sessions_one() as db:
            user, token = create_staff_invitation(
                db,
                actor_user_id=self.admin_id,
                email=email,
                role_key=role_key,
                department_id=department_id or self.department_id,
                allowed_domains={"iiitdwd.example"},
                now=self.now,
            )
            db.commit()
            return user, token

    def test_duplicate_normalized_institutional_email_is_rejected(self):
        self.invite("Technician@IIITDWD.Example")
        with self.sessions_one() as db:
            with self.assertRaises(DuplicateEmail):
                create_staff_invitation(
                    db,
                    actor_user_id=self.admin_id,
                    email=" technician@iiitdwd.example ",
                    role_key="TECHNICIAN",
                    department_id=self.department_id,
                    allowed_domains={"iiitdwd.example"},
                    now=self.now,
                )
        with self.sessions_one() as db:
            self.assertEqual(db.scalar(select(User).where(
                User.normalized_email == "technician@iiitdwd.example"
            )).email, "Technician@IIITDWD.Example")

    def test_invitation_is_single_use_and_password_is_hashed(self):
        invited, token = self.invite()
        with self.sessions_one() as db:
            accepted = redeem_invitation(db, token, "a-strong-synthetic-password", now=self.now)
            self.assertEqual(accepted.user_id, invited.user_id)
            self.assertEqual(accepted.status, "active")
            self.assertIsNotNone(accepted.email_verified_at)
            self.assertNotEqual(accepted.password_hash, "a-strong-synthetic-password")
            self.assertTrue(accepted.password_hash.startswith("scrypt:"))
            self.assertNotIn("a-strong-synthetic-password", accepted.password_hash)
            with self.assertRaises(InvalidToken):
                redeem_invitation(db, token, "another-synthetic-password", now=self.now)

    def test_expired_invitation_is_rejected(self):
        _, token = self.invite()
        with self.sessions_one() as db:
            with self.assertRaises(InvalidToken):
                redeem_invitation(db, token, "a-strong-synthetic-password", now=self.now + timedelta(days=3))

    def test_invalid_unverified_and_suspended_accounts_cannot_authenticate(self):
        user, token = self.invite()
        with self.sessions_one() as db:
            self.assertIsNone(authenticate_staff(
                db, "tech@iiitdwd.example", "wrong-password", "192.0.2.11",
                rate_limit_secret=self.rate_limit_secret, now=self.now,
            ))
            pending_user = db.scalar(select(User).where(User.normalized_email == "tech@iiitdwd.example"))
            self.assertIsNone(pending_user.password_hash)

        with self.sessions_one() as db:
            accepted = redeem_invitation(db, token, "a-strong-synthetic-password", now=self.now)
            self.assertIsNone(authenticate_staff(
                db, accepted.email, "incorrect-synthetic-password", "192.0.2.12",
                rate_limit_secret=self.rate_limit_secret, now=self.now,
            ))
            db.refresh(accepted)
            accepted.status = "suspended"
            db.commit()
            self.assertIsNone(authenticate_staff(
                db, accepted.email, "a-strong-synthetic-password", "192.0.2.11",
                rate_limit_secret=self.rate_limit_secret, now=self.now,
            ))

    def test_successful_authentication_uses_normalized_email_and_hash(self):
        _user, token = self.invite("tech@iiitdwd.example")
        with self.sessions_one() as db:
            accepted = redeem_invitation(db, token, "a-strong-synthetic-password", now=self.now)
            self.assertNotEqual(accepted.password_hash, "a-strong-synthetic-password")
            result = authenticate_staff(
                db, " TECH@IIITDWD.EXAMPLE ", "a-strong-synthetic-password", "192.0.2.21",
                rate_limit_secret=self.rate_limit_secret, now=self.now,
            )
            self.assertEqual(result.user_id, accepted.user_id)

    def test_revoked_session_version_is_rejected(self):
        _user, token = self.invite()
        with self.sessions_one() as db:
            user = redeem_invitation(db, token, "a-strong-synthetic-password", now=self.now)
            claims = session_claims(user)
            self.assertEqual(resolve_session_user(db, claims).user_id, user.user_id)
            self.assertTrue(revoke_sessions(db, user.user_id, now=self.now))
            self.assertIsNone(resolve_session_user(db, claims))

    def test_rate_limit_is_shared_between_independent_database_engines(self):
        for index in range(3):
            with self.sessions_one() as db:
                blocked = record_login_failure(
                    db, "person@iiitdwd.example", "192.0.2.40",
                    rate_limit_secret=self.rate_limit_secret, now=self.now + timedelta(seconds=index),
                )
                self.assertFalse(blocked)
        with self.sessions_two() as db:
            self.assertFalse(record_login_failure(
                db, "person@iiitdwd.example", "192.0.2.40",
                rate_limit_secret=self.rate_limit_secret, now=self.now + timedelta(seconds=3),
            ))
            self.assertTrue(record_login_failure(
                db, "person@iiitdwd.example", "192.0.2.40",
                rate_limit_secret=self.rate_limit_secret, now=self.now + timedelta(seconds=4),
            ))
            self.assertTrue(login_is_blocked(
                db, "person@iiitdwd.example", "192.0.2.40",
                rate_limit_secret=self.rate_limit_secret, now=self.now + timedelta(seconds=4),
            ))
            bucket = db.scalar(select(LoginAttemptBucket))
            self.assertNotIn("person@iiitdwd.example", bucket.bucket_key)
            self.assertNotIn("192.0.2.40", bucket.bucket_key)

    def test_unknown_permissions_and_unassigned_staff_roles_deny_by_default(self):
        with self.sessions_one() as db:
            self.assertFalse(has_permission(db, self.admin, "permission.does_not_exist"))
            unassigned = User(
                account_kind="staff", email="unassigned@iiitdwd.example",
                normalized_email="unassigned@iiitdwd.example", status="active",
                email_verified_at=self.now, created_at=self.now, updated_at=self.now,
            )
            db.add(unassigned)
            db.flush()
            self.assertFalse(has_permission(db, unassigned, "complaint.read_scoped", self.department_id))
            self.assertFalse(has_permission(db, unassigned, "unknown.permission", self.department_id))
            self.assertTrue(is_super_admin(db, self.admin))
            self.assertFalse(is_super_admin(db, unassigned))

    def test_staff_role_requires_valid_department_scope(self):
        with self.sessions_one() as db:
            with self.assertRaises(ValueError):
                create_staff_invitation(
                    db,
                    actor_user_id=self.admin_id,
                    email="head@iiitdwd.example",
                    role_key="MAINTENANCE_HEAD",
                    department_id=self.department_id,
                    allowed_domains={"iiitdwd.example"},
                    now=self.now,
                )

    def test_department_permissions_are_limited_to_active_assignment(self):
        _user, token = self.invite()
        with self.sessions_one() as db:
            technician = redeem_invitation(db, token, "a-strong-synthetic-password", now=self.now)
            self.assertTrue(has_permission(
                db, technician, "work_order.update_assigned", self.department_id,
            ))
            other_department_id = db.scalar(
                select(Department.department_id).where(Department.department_code == "CIVIL")
            )
            self.assertFalse(has_permission(
                db, technician, "work_order.update_assigned", other_department_id,
            ))
            with self.assertRaises(ValueError):
                create_staff_invitation(
                    db,
                    actor_user_id=self.admin_id,
                    email="unknown@iiitdwd.example",
                    role_key="ROOT_OPERATOR",
                    department_id=self.department_id,
                    allowed_domains={"iiitdwd.example"},
                    now=self.now,
                )

    def test_password_reset_is_single_use_and_revokes_old_sessions(self):
        _user, token = self.invite()
        with self.sessions_one() as db:
            user = redeem_invitation(db, token, "a-strong-synthetic-password", now=self.now)
            claims = session_claims(user)
            reset_token = create_password_reset_token(
                db, user.email, allowed_domains={"iiitdwd.example"}, now=self.now,
            )
            reset_user = redeem_password_reset(
                db, reset_token, "a-new-strong-synthetic-password", now=self.now,
            )
            self.assertEqual(reset_user.session_version, claims["session_version"] + 1)
            self.assertIsNone(resolve_session_user(db, claims))
            with self.assertRaises(InvalidToken):
                redeem_password_reset(db, reset_token, "another-strong-password", now=self.now)

    def test_audit_interface_is_append_only_and_does_not_store_passwords(self):
        with self.sessions_one() as db:
            event = record_audit_event(
                db, actor_user_id=self.admin_id, action="test.synthetic",
                subject_type="test", subject_id="fixture-1",
                details={"result": "safe"}, occurred_at=self.now,
            )
            db.commit()
            event.action = "rewritten"
            with self.assertRaises(ValueError):
                db.flush()
            db.rollback()
            persisted = db.get(AuditLog, event.audit_id)
            self.assertEqual(persisted.action, "test.synthetic")
            self.assertNotIn("password", repr(persisted.details).casefold())

    def test_audit_bulk_update_and_delete_are_rejected(self):
        with self.sessions_one() as db:
            event = record_audit_event(
                db, actor_user_id=self.admin_id, action="test.bulk_guard",
                subject_type="test", subject_id="fixture-bulk-1",
                details={"result": "safe"}, occurred_at=self.now,
            )
            db.commit()
            audit_id = event.audit_id

            with self.assertRaisesRegex(ValueError, "append-only"):
                db.execute(
                    update(AuditLog).where(AuditLog.audit_id == audit_id).values(action="rewritten")
                )
            with self.assertRaisesRegex(ValueError, "append-only"):
                db.execute(delete(AuditLog).where(AuditLog.audit_id == audit_id))
            with self.assertRaisesRegex(ValueError, "append-only"):
                db.execute(
                    update(AuditLog.__table__)
                    .where(AuditLog.__table__.c.audit_id == audit_id)
                    .values(action="rewritten")
                )
            with self.assertRaisesRegex(ValueError, "append-only"):
                db.execute(
                    delete(AuditLog.__table__).where(AuditLog.__table__.c.audit_id == audit_id)
                )
            audit_alias = AuditLog.__table__.alias("audit_alias")
            with self.assertRaisesRegex(ValueError, "append-only"):
                db.execute(update(audit_alias).values(action="rewritten"))
            with self.assertRaisesRegex(ValueError, "append-only"):
                db.execute(delete(audit_alias))
            orm_audit_alias = aliased(AuditLog, name="orm_audit_alias")
            with self.assertRaisesRegex(ValueError, "append-only"):
                db.execute(update(orm_audit_alias).values(action="rewritten"))
            with self.assertRaisesRegex(ValueError, "append-only"):
                db.execute(delete(orm_audit_alias))
            audit_expression = table("audit_logs", column("action"))
            with self.assertRaisesRegex(ValueError, "append-only"):
                db.execute(update(audit_expression).values(action="rewritten"))
            with self.assertRaisesRegex(ValueError, "append-only"):
                db.execute(delete(audit_expression))

            bucket = LoginAttemptBucket(
                bucket_key="unrelated-table-guard-test", window_started_at=self.now,
                attempts=1, updated_at=self.now,
            )
            db.add(bucket)
            db.commit()
            db.execute(
                update(LoginAttemptBucket.__table__)
                .where(LoginAttemptBucket.bucket_key == bucket.bucket_key)
                .values(attempts=2)
            )
            db.commit()
            self.assertEqual(
                db.scalar(select(LoginAttemptBucket.attempts).where(
                    LoginAttemptBucket.bucket_key == bucket.bucket_key
                )),
                2,
            )
            db.execute(
                delete(LoginAttemptBucket.__table__).where(
                    LoginAttemptBucket.bucket_key == bucket.bucket_key
                )
            )
            db.commit()
            self.assertIsNone(db.scalar(select(LoginAttemptBucket.bucket_key).where(
                LoginAttemptBucket.bucket_key == bucket.bucket_key
            )))

            persisted = db.get(AuditLog, audit_id)
            self.assertEqual(persisted.action, "test.bulk_guard")
            self.assertEqual(db.query(AuditLog).filter_by(audit_id=audit_id).count(), 1)

    def test_concurrent_initial_super_admin_bootstrap_succeeds_once(self):
        database_path = Path(self.temp_dir.name) / "empty-bootstrap.db"
        from campuscare.migration_runner import upgrade_database
        upgrade_database(sqlite_url(database_path))
        engine_a = engine_for_url(sqlite_url(database_path))
        engine_b = engine_for_url(sqlite_url(database_path))
        factory_a = session_factory(engine_a)
        factory_b = session_factory(engine_b)
        barrier = Barrier(2)

        def attempt(factory, email):
            with factory() as db:
                barrier.wait(timeout=10)
                try:
                    user = bootstrap_first_super_admin(
                        db,
                        email=email,
                        password="synthetic-bootstrap-password",
                        allowed_domains={"iiitdwd.example"},
                        now=self.now,
                    )
                    return ("created", user.user_id)
                except PermissionError:
                    return ("denied", None)

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(attempt, factory_a, "first@iiitdwd.example")
                second = executor.submit(attempt, factory_b, "second@iiitdwd.example")
                results = [first.result(timeout=30), second.result(timeout=30)]
            self.assertEqual(sorted(result[0] for result in results), ["created", "denied"])
            with factory_a() as db:
                self.assertEqual(db.query(BootstrapClaim).count(), 1)
                self.assertEqual(db.query(User).filter_by(account_kind="staff").count(), 1)
                self.assertEqual(
                    db.query(StaffRoleAssignment)
                    .filter_by(role_key="SUPER_ADMIN", revoked_at=None).count(), 1,
                )
                with self.assertRaises(PermissionError):
                    bootstrap_first_super_admin(
                        db, email="later@iiitdwd.example",
                        password="another-bootstrap-password", allowed_domains={"iiitdwd.example"},
                        now=self.now,
                    )
        finally:
            engine_a.dispose()
            engine_b.dispose()


if __name__ == "__main__":
    unittest.main()
