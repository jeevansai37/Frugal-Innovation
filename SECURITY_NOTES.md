# CampusCare Security Notes — Database and Identity Foundation

## Scope and status

This note covers the current Stage 0/1 foundation on `codex/campuscare-db-foundation`. It does not claim a production security review or a successful Neon Preview connection. The local SQLite database was inspected read-only for schema only. No complaint rows were read, changed, backed up, or migrated. `DATABASE_URL` is not configured in the local environment, so the configured Preview PostgreSQL schema could not be inspected. No migration was run against local `complaints.db`, Neon Preview, or Production.

## Implemented controls

- **Versioned schema:** SQLAlchemy models and Alembic revision `0001_identity_foundation` add `users`, `roles`, `permissions`, `role_permissions`, `departments`, `staff_role_assignments`, `auth_tokens`, `audit_logs`, and `login_attempt_buckets`. The migration recognizes the legacy complaint table, preserves its integer `id`, public IDs, rows, and hashes, and adds only missing compatibility columns. On a fresh local database, it creates the legacy complaint table. Application import and startup do not execute schema DDL; schema changes require an explicit migration command. The migration runner rejects PostgreSQL unless a caller explicitly opts in after an isolated rehearsal.
- **Email uniqueness:** institutional email is trimmed, local-part/domain case-normalized, IDNA-normalized for the domain, constrained to a configured exact-domain allowlist, and protected by a unique `normalized_email` database constraint. The authoritative IIIT Dharwad domain list is not configured yet; account creation must remain disabled until it is confirmed.
- **Password storage:** Werkzeug's vetted password hashing implementation stores `scrypt:32768:8:1` hashes. Password input is constrained to 12–128 characters. Tests assert the stored value differs from the submitted password and uses the scrypt hash format.
- **Invitations and resets:** the application service creates 256-bit random URL-safe capabilities and stores only their SHA-256 hashes. Invitation lifetime defaults to 48 hours; reset lifetime defaults to 30 minutes. Consumption uses a conditional database update so expired or previously consumed tokens fail. The first Super Admin is provisioned by an operator-only local CLI after a verified institutional email and domain allowlist are supplied. There is no public staff registration route.
- **Email verification state:** invited staff remain `invited` and unverified until their single-use invitation is redeemed. Redemption activates the account and marks the address verified because the future delivery link is intended for that address. No invitation or reset email is sent in this stage; returned raw tokens must not be logged or placed in source control.
- **Account/session controls:** suspended, inactive, unverified, passwordless, or unassigned staff accounts cannot authenticate through the new service. Session claims carry `user_id` and `session_version`; the resolver checks account state and version, and password reset increments the version. These helpers are not yet wired into the legacy `/staff/login` flow or new HTTP staff routes.
- **Shared throttling:** failed login buckets are keyed by an HMAC of normalized email and network address, not stored raw identifiers. SQLite/PostgreSQL upsert paths persist counts in the shared database; the service defaults to five failures in 15 minutes and removes expired buckets. Multi-engine tests show separate application-engine instances share a lockout. All production instances must use the same stable `LOGIN_RATE_LIMIT_SECRET` or stable `SECRET_KEY`.
- **Authorization:** permission checks require a known permission, active/verified user, active role, and valid scope. Department roles require an active assignment to the requested department; global Super Admin/Maintenance Head grants are distinct. Unknown roles/permissions and missing scope deny. No new feature uses the legacy `is_staff` flag.
- **Audit events:** `record_audit_event` is the append-only application interface. ORM updates/deletes are rejected. Events store action, subject, actor, result, timestamp, optional request ID, and redacted details. Passwords, raw tokens, and complaint descriptions must never be passed into `details`.
- **Compatibility:** the shared `ADMIN_PASSWORD` login and existing complaint routes remain unchanged for the controlled transition. They must not be removed until the new identity flow is integrated, tested end-to-end, and separately approved for cutover.

## Backup and isolated restore procedure

These are procedures to follow before a future migration. They were documented but **not executed** during this stage. Store backups in an access-controlled, encrypted location with an approved retention period. Do not commit them or copy real complaint rows into test fixtures.

### SQLite local database

1. Choose a new backup path and confirm it does not already exist. Keep the live `complaints.db` untouched. Use SQLite's online backup API so an open local database is copied consistently:

   ```powershell
   $backup = "backups\complaints-before-migration-YYYYMMDD-HHMMSS.db"
   if (Test-Path -LiteralPath $backup) { throw "Choose a new backup filename" }
   New-Item -ItemType Directory -Force -Path (Split-Path $backup) | Out-Null
   .\.venv\Scripts\python.exe -c "import sqlite3,sys; from pathlib import Path; p=Path(sys.argv[1]); assert not p.exists(); s=sqlite3.connect('file:complaints.db?mode=ro',uri=True); d=sqlite3.connect(p); s.backup(d); d.close(); s.close()" $backup
   ```

2. Validate the backup without editing the source:

   ```powershell
   .\.venv\Scripts\python.exe -c "import sqlite3,sys; c=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True); print(c.execute('PRAGMA integrity_check').fetchone()[0]); c.close()" $backup
   ```

3. To rehearse restore, choose a different, new target path. Restore into that isolated target and verify schema/application checks there:

   ```powershell
   $restore = "backups\complaints-restore-test-YYYYMMDD-HHMMSS.db"
   if (Test-Path -LiteralPath $restore) { throw "Choose a new restore filename" }
   .\.venv\Scripts\python.exe -c "import sqlite3,sys; from pathlib import Path; p=Path(sys.argv[2]); assert not p.exists(); s=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True); d=sqlite3.connect(p); s.backup(d); d.close(); s.close()" $backup $restore
   ```

   Never copy the restore over `complaints.db` as part of a rehearsal. A real cutover restore requires separate approval and a tested recovery plan.

### PostgreSQL / Neon

1. Before any hosted migration, use the provider's approved snapshot/branch procedure or a protected custom-format dump. Use a dedicated Preview/test connection and inject credentials from a secure environment/secret manager; do not paste a connection string into chat, source, terminal output, or a checked-in script.

   ```sh
   pg_dump --format=custom --no-owner --file=campuscare-preview-before-migration.dump "$PREVIEW_DATABASE_URL"
   ```

2. Restore only into a newly created, isolated, empty test database or approved Neon branch. Do not use `--clean`, `--create`, or a production/Preview source as the restore target:

   ```sh
   pg_restore --exit-on-error --no-owner --dbname="$RESTORE_TARGET_URL" campuscare-preview-before-migration.dump
   ```

3. Validate row counts and application flows within the isolated target without printing complaint contents. Record the snapshot/dump identifier, target, validation result, and authorized owner in the operational change record. The source database remains untouched.

The current environment does not provide `DATABASE_URL`; no Neon snapshot or dump was made, and the Preview schema still needs an authorized read-only inspection before a future migration review.

## Remaining risks and blockers

1. **Preview PostgreSQL inspection unavailable:** `DATABASE_URL` is unset locally. PostgreSQL migration execution and data-preservation behavior are not verified against a live PostgreSQL engine. Models compile for PostgreSQL; that is not a substitute for an isolated integration run.
2. **No real delivery channel:** invite/reset capabilities are service-only and no email is sent. Raw tokens returned by the service need a verified delivery/acceptance path before real staff accounts can be invited.
3. **No HTTP cutover:** the legacy shared-password staff UI remains active. New authentication/session/permission services are not connected to staff routes. No dashboard should assume the new role checks until explicitly wired and tested.
4. **Institutional domain pending:** `INSTITUTIONAL_EMAIL_DOMAINS` is blank in the example configuration. Confirm exact official domains and account verification/termination procedures before bootstrap or invitations.
5. **Audit database privilege boundary:** ORM-level append-only guards do not stop a database owner or direct SQL connection from editing audit rows. Before hosted use, create a runtime DB role that cannot update/delete audit events and a separate migration role with DDL privileges.
6. **Login throttling integration:** the shared database service is tested, but no login route calls it yet. All instances must share a stable throttle secret, proxy client-IP handling must be configured carefully, and thresholds/retention must be reviewed for the pilot.
7. **Bootstrap trust:** `python -m campuscare.bootstrap_admin` is an operator-only local command and treats the operator-provided address as verified. Run it only after an authorized administrator has independently checked identity and domain. It has not been run.
8. **Privacy/operations:** session expiry/cookie integration, audit retention/access policy, database backup retention, recovery drills, incident response, and policy approval remain before hosted identity use.

## Verified tests

The new synthetic test modules cover normalized email uniqueness, invitation reuse/expiry, password hashing, invalid/suspended account denial, password reset and session revocation, database-shared throttling across separate engines, fail-closed permission behavior, assignment scope, audit immutability, application import without DDL, cross-dialect model DDL compilation, fresh and legacy SQLite migrations, and preservation of synthetic complaint IDs/hashes. The existing complaint submission/tracking regression suite remains part of the full test run. Actual PostgreSQL migration execution is not verified because no dedicated PostgreSQL test URL is configured.
