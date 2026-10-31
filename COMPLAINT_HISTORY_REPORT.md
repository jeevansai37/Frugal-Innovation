# CampusCare Complaint Activity History Report

## Implemented

- Alembic revision `0003_complaint_history` adds an indexed `complaint_history` table. It references the existing unique `complaints.complaint_id` and `users.user_id` keys with `RESTRICT` delete behavior, includes a constrained event type and a unique nullable request key, and is forward-only. It does not recreate the complaints table or backfill any events.
- `ComplaintHistory` models persisted event fields. UTC timestamps are stored with their UTC offset in SQLite and as timezone-aware timestamps on PostgreSQL. No optional metadata field was added, avoiding a surface for sensitive internal details.
- New intake writes `submitted` history in the same SQLite/psycopg transaction as the complaint insert. Staff changes write status events and/or remark events together with the complaint update and audit event in the SQLAlchemy transaction. Any write failure rolls the complete update back.
- A random per-form update key and database uniqueness make staff update retries idempotent. When a status changes and a remark is added, two events share the transaction and timestamp: one status event and one `staff_update`. Resolution uses the `resolved` event type.
- Student tracking obtains history only after validating the existing complaint ID/access-code pair. Staff history is read only after the existing complaint read and department-scope authorization service succeeds. The timeline serializer exposes event labels, status values, remarks, and timestamps only; it omits actor IDs and internal fields.
- Both student and staff complaint detail pages show a chronological responsive timeline. History has no normal application edit or delete operation.
- Existing complaints are not backfilled. The known creation timestamp is displayed as the submission event; when a legacy complaint has a remark but no history, the UI displays it as a legacy update with “Original update time unavailable.” Existing `staff_remarks`, `last_updated_at`, IDs, and access-code hashes are left intact.

## Migration and database safety

The new migration follows `0002_complaint_staff_updates`; it creates only `complaint_history` and its index. Migration tests use temporary synthetic SQLite databases, including a synthetic database already at revision `0002` with an existing remark. The migration preserves that entire complaint row and creates no history row for it. Neither `complaints.db` nor Neon was opened or modified. No migration was applied to an existing database.

PostgreSQL runtime verification was not performed. Do not use Neon for that check. Configure `POSTGRES_TEST_DATABASE_URL` to a separately provisioned disposable PostgreSQL database before running the optional PostgreSQL integration tests; the value must not point at Preview or Production.

## Authorization and data handling

- Student history is not queried or rendered for invalid tracking credentials; invalid responses remain generic.
- Staff history uses the existing scoped complaint-read service, so unauthorized department staff receive the same not-found response as for complaint detail.
- Remarks are stripped and capped at 2,000 characters server-side. CSRF remains required on staff update POSTs.
- Actor IDs are stored for staff-authored events but not returned to either page. No emails, access codes, private audit details, or metadata are added to the timeline.
- Unique request keys prevent duplicate committed staff history on replay. Transaction rollback prevents an update from persisting without its matching history.

## Verification

Focused tests run:

```text
.\.venv\Scripts\python.exe -m unittest tests.test_migrations -v
.\.venv\Scripts\python.exe -m unittest tests.test_app -v
.\.venv\Scripts\python.exe -m unittest tests.test_identity_foundation -v
```

- Migration tests: **8 passed**.
- Complaint web-flow tests: **33 passed**.
- Identity and authorization tests: **12 passed**.
- Full suite: **66 run, 64 passed, 2 skipped**. Both skips are optional PostgreSQL integration tests requiring `POSTGRES_TEST_DATABASE_URL`; that variable was removed for the suite invocation to ensure no hosted PostgreSQL target could be contacted.
- `git diff --check`: **passed with no whitespace errors**. Git printed only its existing LF-to-CRLF working-copy notices.

## Limitations

- History starts with complaints submitted after revision `0003` is applied. Older remarks remain visible as undated legacy updates; no original author or timestamp is inferred.
- The timeline is append-only through current application functionality, but database-level permissions/triggers are not configured to prevent a privileged database operator from altering rows.
- PostgreSQL migration execution was not verified in this environment.
- A database must be explicitly migrated to `0003_complaint_history` before the updated application is used against that database. No hosted or existing local database was migrated for this change.
