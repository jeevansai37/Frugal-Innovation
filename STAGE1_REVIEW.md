# Stage 1 Independent Code Review

**Review scope:** `codex/campuscare-db-foundation`, SQLAlchemy models, Alembic environment and revision, identity services, and existing complaint/QR/tracking routes. No Stage 2 work was started.

## Summary

- **Critical:** None found.
- **High:** One migration safety issue was confirmed and fixed. Direct Alembic invocation could bypass the PostgreSQL opt-in guard in `campuscare.migration_runner`.
- **Medium:** The original review found two defense-in-depth issues. Both were addressed in the follow-up addendum below; PostgreSQL runtime privileges remain an operational requirement.
- **Low:** None identified that changes the current Stage 1 acceptance boundary.

The local SQLite database was not modified. No existing database was migrated. No Neon database was contacted. `POSTGRES_TEST_DATABASE_URL` is not configured, so PostgreSQL execution checks could not be run.

## Findings

### High — Direct Alembic CLI bypassed PostgreSQL migration opt-in — Fixed

**Locations:** `migrations/env.py` (online, connection-supplied, and offline entry paths); `campuscare/migration_runner.py:19-33`.

`upgrade_database()` refused PostgreSQL by default, but the regular `alembic upgrade head` CLI entered `migrations/env.py` and connected using `DATABASE_URL` without passing through that check. This meant the ordinary operator safeguard could be bypassed accidentally.

**Fix applied:** `migrations/env.py` now checks the database dialect before opening or using a PostgreSQL connection. The only opt-in is a code-level `allow_postgres=True` passed by `upgrade_database`; the direct CLI cannot silently opt in. A regression test invokes Alembic with a deliberately fake `example.invalid` PostgreSQL URL and asserts it stops at the guard before any connection attempt.

### Medium — Audit ORM hooks do not cover bulk UPDATE/DELETE

**Locations:** `campuscare/audit.py:33-40`.

The `before_update` and `before_delete` mapper hooks reject normal ORM object mutation. SQLAlchemy bulk operations such as `session.execute(update(AuditLog)...)` or `session.execute(delete(AuditLog)...)` do not invoke those mapper hooks. No current application service performs such a bulk operation on audit rows, but the model-level rule alone is not a complete application boundary.

**Recommended fix, prioritized:** Before adding audit-management services, add a SQLAlchemy session execution guard for ORM UPDATE/DELETE statements targeting `audit_logs`, and use a hosted runtime database role that has no UPDATE/DELETE privileges on that table. Keep migrations on a separate privileged role. Add tests for unit-of-work and bulk-DML attempts.

### Medium — First Super Admin bootstrap is not serialized

**Locations:** `campuscare/identity.py:163-203`.

Bootstrap checks for an existing active Super Admin, then inserts the user and assignment. Two operator invocations against the same empty database can both pass the check and create two initial Super Admins. The command is local/operator-only, so this is an operational race rather than a public registration path.

**Recommended fix, prioritized:** Before enabling bootstrap in a shared hosted environment, serialize it with a database lock or a transactionally enforced singleton bootstrap record/constraint. Add a concurrent bootstrap test using the PostgreSQL test database.

## Review details

### Schema, migration, and compatibility

- `campuscare/models.py` defines the requested identity tables. Primary keys are UUID strings for users, departments, assignments, tokens, and audit events; existing complaints retain their integer primary key. `DATABASE_DESIGN.md` specifies a later UUID mapping/extension for complaint relationships without rewriting the existing IDs.
- `migrations/versions/0001_identity_foundation.py` is explicit and versioned. For an existing complaints table it only adds missing `priority`, `roll_number`, and `access_code_hash` columns; it does not rebuild or rewrite complaint rows. Its fresh-install path creates the legacy table only when absent.
- Synthetic SQLite migration tests compared the complete fixture row before and after migration, including integer ID `41`, public complaint ID, status, and access-code hash. An older synthetic schema test also preserved its row and status while adding compatibility columns.
- Foreign keys use `RESTRICT` (no destructive cascades). Role-permission pairs have a composite primary key; normalized email, department code, token hash, and active assignment scopes have uniqueness constraints. Role/status/purpose and department-scope checks are present. Audit and token lookup indexes are present; scoped active assignment uniqueness uses partial indexes for SQLite and PostgreSQL.
- ORM timestamps use `DateTime(timezone=True)` and service-generated UTC-aware values. SQLite may return naive timestamps; the login-block check normalizes that value before comparison. PostgreSQL timestamp behavior could not be exercised here.
- PostgreSQL DDL for every model/table and index compiles successfully in the existing dialect compilation test. This does not prove a live PostgreSQL migration succeeds.

### Identity, tokens, throttling, and authorization

- Staff passwords use Werkzeug scrypt hashes (`scrypt:32768:8:1`) with a 12–128 character limit. Tests check hashes are not plaintext.
- Invitation and reset tokens are generated from 32 random bytes, stored only as SHA-256 hashes, and have configured expiry. Conditional `UPDATE ... WHERE consumed_at IS NULL AND expires_at > now` provides single-use redemption. Tests cover repeat and expired invitations.
- Invitation redemption updates token consumption, account activation, password hash, verification timestamp, and audit event in one transaction and commits together. Invalid/ineligible redemption rolls back. Invitation creation flushes the invited user, assignment, token, and audit event but leaves commit ownership to its caller; callers must roll back if later assignment/token persistence raises.
- Session claims include a version; resolution rejects suspended/unverified users and stale versions. Password reset increments the version and consumes other outstanding reset tokens.
- Login-failure updates use a database upsert keyed by HMAC(normalized email, remote address), and separate SQLite engines share the counter in tests. PostgreSQL concurrency and multi-instance behavior remain unverified in a PostgreSQL runtime.
- Unknown permissions, unknown/inactive roles, missing assignment, inactive department, and wrong department scope fail closed in service tests. The existing legacy `is_staff` check remains only on the old shared-password routes; the new identity foundation does not use it.
- `authenticate_staff()` and shared throttling are service functions and are not yet wired to an HTTP login route. The legacy shared-password route remains active by design during Stage 1.

### Startup and complaint compatibility

- `tests/test_migrations.py::test_application_import_does_not_create_a_database_or_run_ddl` verifies importing `app` in a temporary directory does not create a database. Schema DDL is only run through an explicit migration command.
- Existing synthetic complaint intake, QR selection/SVG, tracking capability, status update, and legacy access-code tests passed. Existing complaint routes still use their compatibility database layer; no identity route cutover was introduced.

## Tests performed

Commands run:

```text
.\.venv\Scripts\python.exe -m unittest tests.test_migrations -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Results:

- Migration suite: **7 passed**. This includes fresh and synthetic legacy SQLite migrations, complaint row/hash preservation, import without DDL, SQLite/PostgreSQL dialect DDL compilation, the runner's PostgreSQL opt-in check, and the new direct-Alembic bypass regression test.
- Full suite: **53 run, 52 passed, 1 skipped**. The skipped test is the optional PostgreSQL integration test because `POSTGRES_TEST_DATABASE_URL` is unset.
- `git diff --check`: no whitespace errors.
- No migration was run against `complaints.db`, Neon Preview, or Production.

## PostgreSQL verification status and setup

**Status: Not run.** `POSTGRES_TEST_DATABASE_URL` is not configured in this environment. No Neon endpoint was used as a substitute.

To enable the existing PostgreSQL integration suite, provision a separate disposable PostgreSQL database (not Neon Preview or Production), then set:

```powershell
$env:POSTGRES_TEST_DATABASE_URL = "postgresql+psycopg://<test-user>:<test-password>@<test-host>:5432/<disposable-test-db>"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Use a direct, non-pooled connection. The test role must be able to create and drop its own uniquely named schema in that disposable database. The current optional PostgreSQL test covers fresh-schema migrations and complaint/QR flows; PostgreSQL migration from a synthetic legacy complaint schema is not currently implemented as a test and should be added before claiming that case is verified. Do not put credentials in source control or the review document.

## Changed files in this review

- `migrations/env.py` — guard PostgreSQL across Alembic execution paths.
- `campuscare/migration_runner.py` — pass explicit PostgreSQL opt-in state into Alembic.
- `tests/test_migrations.py` — regression for direct CLI bypass.
- `ROADMAP.md` — record review result and verified test count.
- `STAGE1_REVIEW.md` — this report.

## Remaining blockers

1. PostgreSQL migration/integration verification needs a dedicated disposable database via `POSTGRES_TEST_DATABASE_URL`.
2. Add the PostgreSQL synthetic-legacy migration case before claiming PostgreSQL legacy migration preservation.
3. The two Medium findings were addressed in the follow-up addendum below; hosted use still requires the documented PostgreSQL runtime-role grants and a database migration performed through the separately approved operational process.

No Stage 2 work was performed. No commit, push, deployment, merge, Production change, local database change, or existing database migration was performed.

## Follow-up security-hardening addendum

The two Medium findings above were remediated in the follow-up hardening work on the same development branch:

- `campuscare/audit.py` now rejects SQLAlchemy session bulk UPDATE/DELETE statements targeting `audit_logs`, alongside the existing mapper hooks. It walks the DML target expression tree to catch ORM aliases, Core aliases, mapped tables, and table-expression targets. Regression tests cover ORM/Core UPDATE/DELETE and confirm unrelated-table DML and normal audit insertion still work. Engine/Connection execution, DBAPI calls, external clients, and opaque textual SQL do not pass enough target information through this session guard; the PostgreSQL role boundary remains required.
- `migrations/versions/0004_bootstrap_claims.py` adds a database primary-key claim. `bootstrap_first_super_admin()` inserts that unique claim before checking for an existing Super Admin, and commits it atomically with the user, assignment, and audit event. Duplicate claims fail closed and rollback. Concurrent bootstrap is covered by a two-thread/two-engine SQLite test; PostgreSQL execution was intentionally not attempted.
- `SECURITY_NOTES.md` documents the PostgreSQL runtime role restrictions: runtime must not own the audit table or inherit the migration/table-owner role, and is granted only SELECT/INSERT (with UPDATE, DELETE, and TRUNCATE revoked). Those grants were not applied to a database.
- Follow-up full suite: **68 run, 66 passed, 2 PostgreSQL integration tests skipped** because remote database access was explicitly prohibited. The targeted audit bulk-DML and concurrent-bootstrap regressions passed. `git diff --check` passed with no whitespace errors.

The follow-up migration was exercised only by tests against temporary SQLite fixtures. No migration or write was performed against an existing database, no remote connection was made, and no deployment occurred.
