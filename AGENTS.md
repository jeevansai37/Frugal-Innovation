# Project instructions

## Project context

This is a college demonstration project for QR-based maintenance complaint intake and tracking. Preserve the existing Flask application and QR-code flows. Support SQLite for local development and Neon PostgreSQL for hosted deployments, using the existing complaint schema and safe additive migrations. Keep the interface simple enough for a college demonstration while maintaining an accessible, responsive presentation.

## Development workflow

- Inspect the current routes, templates, database schema, and repository state before changing code. Reuse existing routes, templates, and database structures where possible.
- For each development task, make a short plan, implement the change, run relevant tests, fix failures, review the diff, and update `ROADMAP.md` to reflect the verified result.
- After a task meets its acceptance criteria, continue to the next independent task in the approved scope. Do not ask about routine coding decisions.
- Never claim tests passed unless they were actually run. State when no relevant test exists or when a check was not run.
- Preserve unrelated and uncommitted user changes.

## Data, security, and external actions

- Never expose secrets or commit real complaint data. Treat the local SQLite database as user data; use synthetic fixtures for development and testing.
- Never delete project data, rewrite Git history, force-push, deploy, spend money, or modify production resources without explicit user approval.
- Keep changes scoped to the requested work. Do not make destructive data or environment changes as part of routine development.
