# CampusCare UI Redesign Report

## Implemented

- `/` now serves the CampusCare / IIIT Dharwad homepage with Submit a Complaint, Track a Complaint, and Staff Login actions plus the three-step Submit → Track → Resolve explanation.
- `/complaint` serves the existing validated form with a category selector. Description-based category classification remains a fallback for clients that omit the field. Existing QR links still open `/complaint?location=...`; the QR location remains locked and authoritative.
- Successful submission shows the complaint ID and private eight-digit access code. **Copy Tracking Link** creates a URL with both credentials in the URL fragment. The fragment is not sent in the HTTP request; page JavaScript reads it, removes it from the address bar, and submits the values through the existing CSRF-protected POST to `/student`.
- `/student` still requires the complaint ID and access code and keeps invalid lookups generic. Valid tracking shows category, location, submission time, current status, last update, and the latest staff remark. The progress indicator shows Submitted, In Progress, and Resolved.
- `/staff/login` uses individual institutional email/password authentication from the existing identity services. Session version and active account/role state are checked for protected requests. There is no shared-password fallback on the new staff pages and no registration endpoint.
- `/dashboard` shows authorized complaint counts, status filtering, search, and student details only for categories covered by the signed-in staff member's department permissions. `/dashboard/complaint/<complaint_id>` uses the complaint service to validate permissions and update status/remarks. `/admin` requires the global report permission. Logout clears the identity session.
- Both `static/style.css` and Vercel's `public/static/style.css` now contain the same responsive rules. Breakpoints are defined for tablet and phone widths, and reduced-motion handling and visible keyboard focus are retained.

## Migration

Revision `0002_complaint_staff_updates` follows `0001_identity_foundation` and adds nullable `last_updated_at` and `staff_remarks` columns to the existing `complaints` table. It does not rewrite complaint rows, IDs, statuses, or access-code hashes. Legacy rows with a null update timestamp display their submission time. The migration was run only against temporary synthetic SQLite fixtures; it was not applied to `complaints.db`, Neon Preview, or Production.

The migration is required for complaint tracking and staff detail/update pages to use the new fields. Run the explicit migration command only after an operator verifies the configured target and follows the backup/rehearsal procedure. Application import and startup do not execute DDL.

## Authorization and compatibility

- Tracking remains capability-based and private; dashboard queries do not select or render access-code hashes.
- Status updates require `complaint.triage`; marking a complaint Resolved also requires `complaint.resolve_review`. Department-scoped staff see only categories mapped to their active department. The mapping is explicit: Electrical → Electrical, Plumbing → Plumbing, Network → Network/IT, and Equipment/Furniture/Other → Other.
- A synthetic technician test confirms a department-scoped user cannot see another department's student or complaint details and cannot update status without triage permission. A legacy `is_staff` cookie alone is denied.
- All form POSTs continue using CSRF protection. Existing complaint intake validation, QR generation/location prefill, legacy tracking hashes, and complaint identifiers remain covered by regression tests.
- There is no public staff registration route. Staff creation/invitation remains administrator-controlled; email delivery is not part of this change.

## Workflow and test results

The end-to-end test uses a temporary SQLite database and synthetic accounts/complaints to exercise homepage → complaint form → submission → copied tracking-link credentials → student tracking → individual staff login → dashboard search → In Progress with remark → student verification → Resolved with resolution details → student verification. It also confirms the tracking link keeps credentials out of the query string and the dashboard does not display the access code.

Commands run:

```text
.\.venv\Scripts\python.exe -m unittest tests.test_migrations -v
.\.venv\Scripts\python.exe -m unittest tests.test_app -v
.\.venv\Scripts\python.exe -m unittest tests.test_identity_foundation -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
git diff --check
```

Results:

- Migration tests: **7 passed**.
- Complaint web-flow tests: **26 passed**.
- Identity and authorization tests: **12 passed**.
- Full suite: **58 run, 56 passed, 2 skipped**.
- Skips: `test_complaint_tracking_staff_status_analytics_and_qr` and `test_migration_preserves_synthetic_legacy_complaint` in `tests/test_postgres.py`; both require `POSTGRES_TEST_DATABASE_URL`, which is not configured. PostgreSQL DDL compilation passed, but PostgreSQL migration execution was not verified.
- `git diff --check`: no whitespace errors.

## Visual verification and remaining limitations

Browser visual verification was **not completed**. The computer-use helper could not determine the active browser URL confidently and stopped before opening the local preview. No desktop or mobile screenshot review is claimed. Responsive CSS and mobile/tablet layout rules were inspected in source only.

Other remaining limitations:

- The local and hosted databases still need an approved migration run; no database was modified during this task.
- `POSTGRES_TEST_DATABASE_URL` must point to a separate disposable PostgreSQL database before either optional PostgreSQL integration test can run. Do not use Neon Preview or Production as a substitute.
- The exact institutional email domain allowlist and Super Admin provisioning process still need operator confirmation before real staff accounts are created.
- Only the latest staff remark is stored in `staff_remarks`; this simple pilot does not provide a student-visible update history.
- Technician roles have scoped read permission but do not have complaint triage permission in the current role matrix; authorized supervisors or global maintenance roles perform status updates.

No Stage 2 technician workflow, attachments, email delivery, database deployment, commit, push, or deployment was performed.
