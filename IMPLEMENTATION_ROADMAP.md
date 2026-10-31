# Campus Maintenance Management System: Implementation Roadmap

## Planning assumptions

This roadmap is a proposed sequence, not completed work. The initial pilot is IIIT Dharwad (50–200 users), with institutional email/password and Super Admin invitations; SSO is deferred. Students initially use complaint-specific tracking capabilities. It preserves current complaint IDs and records, keeps the existing QR flow, uses PostgreSQL on Vercel and SQLite locally, and avoids production changes until separately approved. The repository is currently on `vercel-preview`; `main` is the production branch per the project brief. Do not merge/promote changes or alter Production settings as part of these phases without explicit authorization.

Dependencies such as SSO, mail/SMS, private object storage, and scheduling are not selected here. A phase that needs one must first identify an institute-managed option or obtain approval for cost and data handling. Until attachment storage is selected, attachments remain disabled rather than written to ephemeral/public storage.

## Controlled Stage 0/1 status

Stage 0 established `codex/campuscare-db-foundation` from the current `vercel-preview` commit and preserved the four existing untracked architecture documents. Local SQLite was inspected read-only; no rows were read. `DATABASE_URL` is not configured locally, so Preview PostgreSQL schema inspection is blocked. Backup/restore steps are documented but were not run. Synthetic migration fixtures are created in temporary databases.

Stage 1 implemented the initial SQLAlchemy models, Alembic migration, invite/password-reset/session/rate-limit/audit services, and synthetic tests. It has not been applied to the existing SQLite file or any Neon database. The legacy shared-password login remains unchanged and is not wired to the new service. PostgreSQL model compilation passed, but live PostgreSQL migration execution still needs a dedicated disposable URL. Do not treat this foundation as a complete staff login cutover.

## Phase 0 — Confirm policy and protect the baseline

**Dependencies:** None. Decisions and named operational owners from IIIT Dharwad.

**Work:** Confirm the authoritative institutional email domain list; role/department roster; location hierarchy; category routing; severity/emergency handling; SLA calendars; resolution/reopen rules; notification channels; privacy/retention; attachment policy; restore ownership. SSO is deferred. Establish isolated SQLite and PostgreSQL fixtures and a verified backup/restore runbook. Record deployed versions and schema revisions without copying complaint data into source control.

**Acceptance criteria:**

- Product owner approves role and lifecycle rules in `ROLE_PERMISSION_MATRIX.md`.
- Department codes, escalation contacts, location hierarchy, and initial category mappings have named owners.
- Production data restore is demonstrated in an isolated environment by the operational owner; no restore overwrites the source database.
- A staging/Preview database and test identity are isolated from real complaints.
- Invite-only institutional email/password is the pilot staff identity method; SSO is explicitly deferred. Notification and attachment providers remain deferred pending approval.

## Phase 1 — Migration foundation and individual staff identity

**Dependencies:** Phase 0 decisions on department roster and the authoritative institutional email domain list. The pilot identity method is confirmed as invite-only email/password; SSO is deferred.

**Work:** Introduce an application factory and modular blueprints/services, SQLAlchemy repository/model layer, Alembic revisions, schema-version check, and migration command. Add users, roles, permissions, departments, role assignments, invitations, session revocation/version, and shared login throttling. Implement invite/redeem/suspend/reset flows. Keep current staff routes behind a deliberately controlled compatibility policy; never allow shared-password access to bypass individual authorization once cutover is enabled.

**Acceptance criteria:**

- Fresh SQLite, fresh PostgreSQL, and each supported legacy schema migrate forward successfully.
- Existing complaint IDs, rows, and access-code hashes remain unchanged after migration tests.
- Staff invite tokens are single-use/hashed/expiring; passwords are never stored in plaintext; reset cannot enumerate accounts.
- Each role grant is department-scoped and revocable. Session rotation/revocation and secure cookie configuration are tested.
- Every staff route has an explicit permission and scope test; unknown roles and cross-department resource IDs deny access.
- Application import no longer performs DDL; deployment fails with a clear schema-version message if migration is missing.

## Phase 2 — Structured intake, classification, routing, and history

**Dependencies:** Phase 1 authorization and migration foundation; Phase 0 location/category decisions.

**Work:** Add campus locations, category/severity vocabularies, routing rules, triage states, duplicate links, audit/history events, capability tracking tokens, and safe QR location references. Add a migration/backfill that preserves legacy reporter/location/category text and original status. Keep old QR URLs, public complaint IDs, and legacy access-code verifier behavior.

**Acceptance criteria:**

- Old QR destinations continue to reach the correct intake path; changing display text does not invalidate stable QR identifiers.
- New reports have separate severity and queue priority, immutable ID, UTC submission time, history event, and configured department suggestion.
- Unmapped historical categories/locations remain visible for triage and do not receive guessed assignments.
- Legacy complaints remain readable using exact IDs and current history/status; no identity or tracking secret is invented.
- Tracking capability tokens are high entropy, stored as hashes, revocable, expiring by policy, and excluded from logs/referrers/cache.
- Duplicate reporting links records while preserving both records and histories.

## Phase 3 — Department operations and technician work orders

**Dependencies:** Phase 2 routing and history; approved departments and SLA rules.

**Work:** Build role-specific Maintenance Head, Supervisor, and Technician workspaces. Add multi-department work orders, assignment/reassignment history, technician queues, state transition service, blockers, resolution notes/evidence metadata, department filters, pagination, workload, and escalation workflow. Add SLA policy snapshots and overdue calculations.

**Acceptance criteria:**

- Department Supervisor queries and mutations are scoped server-side to active department assignments.
- Technician can read/update only assigned work orders and cannot directly set complaint closure.
- Cross-department reassignment requires Maintenance Head permission and a reason; every assignment change is attributable.
- State transitions are validated, atomic, version-checked, and append history in the same transaction.
- SLA deadlines are timezone-aware, derived from an effective policy snapshot, and overdue alerts can be tested with a controllable clock.
- Dashboard queues are paginated, keyboard accessible, mobile usable, and have loading/error/empty states.

## Phase 4 — Student portal and lifecycle completion

**Dependencies:** Identity choice from Phase 0; Phase 2 capability tokens; Phase 3 resolution review.

**Work:** Build the mobile student portal, account-based complaint history when SSO is available, secure receipt/tracking links, status timeline, copy/download receipt, confirmation/reopen and feedback actions. Add notification preferences and transactional outbox with a provider adapter only after channel approval.

**Acceptance criteria:**

- Student accesses only linked complaints or valid capability links; roll number alone grants no access.
- Student can see safe timeline events and never internal notes or out-of-scope data.
- Valid resolution confirmation closes the complaint; reopen follows the approved time window and records an event.
- Receipt and capability URLs do not leak through referrers, logs, caches, or analytics. Revoked/expired links deny access.
- Notification intent commits atomically with state changes; retries are idempotent and delivery errors do not lose the complaint update.
- If no provider is approved, the app retains in-app/receipt progress and clearly documents external notification as deferred.

## Phase 5 — Attachments, reporting, and production hardening

**Dependencies:** Approved private storage and retention policy; completed operational workflows.

**Work:** Add private image upload and evidence retrieval, content validation/scanning, retention and deletion/anonymization procedures, reports/export controls, observability, rate-limit review, backup/restore rehearsal, and load/concurrency verification. Reconcile parallel `static` and `public/static` asset ownership as part of a reviewed UI packaging change.

**Acceptance criteria:**

- Attachment storage is durable and private, object keys are random, file size and detected MIME are enforced, unauthorized downloads fail, and malware/scan states are visible.
- Reports enforce the same department permissions as operational views; exports are audited and redact fields by role.
- Backup restoration and schema upgrade/rollback are rehearsed on isolated copies.
- Tests demonstrate safe operation under concurrent submissions, assignments, duplicate marking, transitions, and pool pressure.
- Monitoring alerts identify application/database failures without logging passwords, access tokens, complaint descriptions, or student identifiers.
- Production-readiness review approves privacy, recovery, security, and cutover procedures before any production migration or branch promotion.

## Dependencies and release gates

| Gate | Required before |
| --- | --- |
| Confirmed institutional email domain and operator verification procedure | Staff invitations and first Super Admin bootstrap |
| Approved role/department roster and escalation owner | Staff authorization and routing |
| Location hierarchy and QR identifier strategy | Structured intake and replacement QR printing |
| Severity definitions, calendars, and SLA owner | SLA deadline calculation and overdue escalation |
| Student notification channel and consent/retention policy | External email/SMS notifications |
| Approved private storage, size/retention/scanning policy | Enabling attachments |
| Isolated test DB, migration rehearsal, restore test | Any hosted schema migration |
| Security and workflow acceptance from operations | Preview evaluation and later production cutover |

Each phase should be delivered to Preview first, exercised with synthetic records, reviewed by the role owners, and accepted before the next dependent phase begins. Production branch/settings, live Neon schema, and real complaint data are outside this planning stage.

## First implementation tasks, prioritized

1. **Policy workshop and named owners:** confirm the authoritative institutional email domain, department/category routing, location hierarchy, emergency criteria, SLA calendars, student reopen rules, and data retention. SSO remains deferred. These decisions change authorization and later schemas.
2. **Migration and recovery rehearsal:** take an approved isolated copy/snapshot, inventory the exact SQLite and Neon schemas, establish Alembic baselines, and prove legacy complaint IDs/statuses/access-code hashes survive migration and restore.
3. **Authorization model and tests:** implement role/permission evaluation with department/resource scope, plus a table-driven allow/deny test matrix before adding new staff screens.
4. **Individual staff identity integration:** add an approved invitation delivery/acceptance path, wire the tested identity services into new login/session endpoints, shared login throttling, and audited role assignment; explicitly gate the legacy shared-password path during transition.
5. **Complaint history and safe state-transition service:** introduce append-only events and atomic transition/version handling while preserving existing route behavior and public complaint IDs.

