# QR-Based Smart College Maintenance Complaint Management Process

## Project purpose

This is a college process-innovation prototype for submitting and tracking campus maintenance complaints. It uses Flask and HTML/CSS, with SQLite locally and PostgreSQL when `DATABASE_URL` is configured. The public homepage links to complaint submission and private tracking. Staff sign in with individually provisioned institutional email accounts. Students use a per-complaint access code. The project does not use AI/ML.

## Phase 1 features

- Complaint form for name, roll number, location, category, and problem description
- Student-selected category; older clients that omit the category retain description-based classification
- Automatic priority assignment as High, Medium, or Low using keyword rules
- Unique complaint ID generated for each submission
- SQLite persistence with status and submission timestamp
- Confirmation page showing the complaint ID, assigned category and priority, and one-time student access code

## Phase 2: location QR codes

- QR links for M Block, PI Block, E Block, H Block, Boys Hostel, and Girls Hostel
- QR links open `/complaint?location=...` with the location prefilled and read-only
- QR-derived location is taken from the URL when saving, then stored in the existing `location` column
- `/qr-codes` displays the QR codes and offers SVG downloads

AI/ML is not included. See the maintenance and student dashboard sections for the current access model.

## Phase 3: maintenance dashboard

- `/dashboard` displays complaints and counts the signed-in staff member is authorized to view
- Open a complaint to view its details and change its status to Pending, In Progress, or Resolved
- Staff can leave the latest student-visible progress or resolution remark
- Status and remark updates are saved through the authorized complaint service
- The maintenance dashboard, complaint detail/status update, and admin analytics require individual staff sign-in and server-side role permissions

## Phase 4A: automatic categorization and priority

- Students choose a complaint category; priority is assigned from description keywords
- Description-based category classification remains as a fallback for clients that omit category
- Category uses the first matching group in this order: Electrical, Plumbing, Equipment, Furniture, Network; otherwise Other
- Priority checks High terms first, then Medium, then Low; if nothing matches, it defaults to Medium
- Water leaking near an electrical fitting is treated as High priority
- The SQLite table receives a `priority` column automatically; existing rows without a value display as Medium
- Dashboard table includes a priority badge and priority filter
- No ML or external APIs are used

## Phase 4: student dashboard

- Each new complaint receives an exactly eight-digit, cryptographically random access code (leading zeros are retained) shown once on its successful submission page
- The database stores only a per-code-salted scrypt hash; the complaint ID and access code remain separate
- `/student` requires both the complaint ID and its access code before showing complaint details or status
- Previously issued long access codes continue to work; their existing SHA-256 hashes are verified without rewriting the database
- Five failed attempts for the same complaint from one IP within 15 minutes trigger a temporary 429 response
- The throttle is in memory and resets on app restart; a multi-worker deployment would need shared rate-limit storage
- Roll number is retained as complaint information but is not used for student lookup or access
- Existing complaints are migrated with a nullable access-code hash and remain available to staff; no access codes are invented for old records

## Phase 5: admin dashboard and analytics

- `/admin` is a read-only overview with total, status, and high-priority counts
- Displays complaints by category and location, status distribution, and monthly complaint totals
- Highlights the most reported location and common category, plus the ten most recent complaints
- Charts are drawn with local SVG/CSS and require no external APIs or chart dependencies
- The admin dashboard requires staff sign-in

## Requirements

- Python 3.9 or newer
- pip

## Run the automated tests

With the project virtual environment activated, run:

```powershell
python -m unittest discover -s tests -v
```

By default, tests use temporary SQLite databases and synthetic complaints; they do not write to the project's `complaints.db`. If `POSTGRES_TEST_DATABASE_URL` is set to a dedicated direct-connection test database, the optional PostgreSQL flow test creates and drops its own schema there.

The database foundation tests create temporary synthetic SQLite databases and verify forward migrations preserve legacy complaint IDs and access-code hashes. PostgreSQL model DDL is compiled in tests, but executing migrations against PostgreSQL requires a dedicated disposable test database. The local `.env` does not currently configure one.

The latest UI migration, `0002_complaint_staff_updates`, adds nullable `last_updated_at` and `staff_remarks` columns so the tracking page can show the latest staff update. It does not rewrite complaint rows. It must be applied with the explicit migration command after the operator verifies the target and completes the documented backup/rehearsal steps. No migration was run against the checked-in local or hosted database during this work.

## Install

From the project directory, create and activate a virtual environment (recommended), then install dependencies:

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

macOS/Linux:

```bash
source .venv/bin/activate
```

Install the dependencies:

```bash
pip install -r requirements.txt
```

## Run locally (Windows PowerShell)

Schema changes are explicit. Before the first launch, verify the configured target, back up an existing local database using the procedure in [SECURITY_NOTES.md](SECURITY_NOTES.md), then run the migration command against a local/test database:

```powershell
.\.venv\Scripts\python.exe -m campuscare.migrate
```

This command uses `DATABASE_URL` if configured; otherwise it targets local `complaints.db`. The runner refuses PostgreSQL by default; PostgreSQL migration execution requires an explicit code-level opt-in after an isolated rehearsal. Do not run it against Preview or Production Neon in this stage. Flask import and app startup do not run schema DDL.

```powershell
.\.venv\Scripts\python.exe app.py
```

Run the command from the project directory. The app loads settings from a local `.env` file at startup and gives process environment variables precedence. A blank example is in `.env.example`. Open http://127.0.0.1:5000. On a fresh install, the explicit migration creates `complaints.db` and the initial schema; on an existing install it preserves complaint rows and adds the identity foundation. The QR gallery is at http://127.0.0.1:5000/qr-codes.

For macOS/Linux, run the same command from the project directory:

```bash
python app.py
```

`/staff/login`, `/dashboard`, complaint details, and `/admin` use the individual identity foundation and permission checks. No shared-password fallback is used for these routes. Set `SECRET_KEY` (or the supported local `FLASK_SECRET_KEY`) to a private stable value so signed staff sessions survive restarts. Do not commit it.

The identity foundation accepts a configured institutional domain allowlist through `INSTITUTIONAL_EMAIL_DOMAINS` (comma-separated). Confirm the official IIIT Dharwad domains before provisioning accounts. After applying migrations to an approved target, the first Super Admin can be provisioned through the operator-only command `python -m campuscare.bootstrap_admin`; it prompts for the password and sends no email. Staff invitations remain administrator-created service operations. There is no public staff registration route or email delivery.

The homepage is `/`. Student submission is at `/complaint` (QR codes keep opening `/complaint?location=...`), private tracking is at `/student`, and staff login is at `/staff/login`. The success page offers a Copy Tracking Link action. Its access code is placed in the URL fragment, which browsers do not send to the server; the tracking page turns it into a CSRF-protected POST. Staff complaint details use permission-checked service methods for status and remark updates. Students can see the latest remark only after presenting both complaint credentials.

By default, QR URLs use `http://localhost:5000`. To scan a printed code with a phone, point it to the computer running Flask and allow Flask to accept local network connections. Set `APP_BASE_URL` to that computer's LAN IP and `APP_HOST` to `0.0.0.0` before starting Flask. Replace `192.168.1.20` with the computer's LAN IP:

```powershell
$env:APP_BASE_URL = "http://192.168.1.20:5000"
$env:APP_HOST = "0.0.0.0"
python app.py
```

Keep the phone and computer on the same network. For same-computer testing, leave both settings at their defaults.

## Demonstration workflow

1. Open `/` and select **Submit a Complaint**, or scan a QR code from `/qr-codes` to open the form with a locked location.
2. Submit synthetic details and save the complaint ID and access code. Use **Copy Tracking Link** to copy a private link, then return to `/` and choose **Track a Complaint**.
3. Sign in at `/staff/login` with an individually invited institutional account. Open the complaint from the dashboard, set it to In Progress, and add a student-visible update.
4. Track the complaint again with the same credentials to confirm the update. Staff can mark it Resolved and add resolution details, which also appear in private tracking.
5. Sign out from a staff page when finished.

The QR gallery defaults to `localhost`, which works on the same computer. For a phone scan, use the LAN setup below and keep the phone and computer on the same network. Restart the app after changing these environment settings.

## Test a complaint submission

1. Open the home page and fill in all required fields.
2. Enter your name, roll number, location, category, and description, then submit. Priority is assigned from description keywords.
3. The confirmation page displays the generated complaint ID and `Pending` status plus the one-time 8-digit access code. Keep both credentials for tracking. Previously issued longer access codes remain accepted.
4. Try submitting with a required field blank to see validation.

To try QR location identification, open `/qr-codes`, scan a code or click its URL, and confirm its location is already filled in and read-only. Submit the complaint and confirm the selected location appears on the confirmation screen and is saved in the `location` database column.

To add another QR location, add its name to `QR_LOCATIONS` in `app.py`. It will appear in the gallery automatically. The QR SVG endpoint can also generate a one-off code at `/qr-code/<location>.svg`; its destination uses `APP_BASE_URL` when configured, otherwise it uses Vercel's `VERCEL_URL` or the local `http://localhost:5000` default.

The database can be inspected with any SQLite browser or the `sqlite3` command-line tool. Complaints remain in the existing `complaints` table with its integer `id`, public `complaint_id`, and current fields. The database is local runtime data and is excluded from Git. Starting the app does not create tables; run the explicit Alembic migration command after reviewing the target. Existing rows and access-code hashes are covered by synthetic migration-preservation tests. No student identities or new tracking capabilities are fabricated for legacy complaints.

This is a local development prototype. Flask's built-in server is intended for development, not public production deployment.

## Vercel preview with Neon PostgreSQL

The Flask application supports the existing local SQLite workflow and uses PostgreSQL when `DATABASE_URL` is set. PostgreSQL connections use a small lazy connection pool (`DB_POOL_MAX_SIZE`, default `5`) suited to warm serverless instances. Schema changes use explicit Alembic revisions; Flask import and startup do not execute DDL. The initial migration preserves the existing complaints table and integer primary key. Existing SQLite complaints are never copied or migrated automatically.

1. Push this project to a GitHub repository. Confirm `.env` and `complaints.db` are absent from the repository; `.gitignore` excludes both.
2. In the Vercel dashboard, choose **Add New → Project**, import the repository, and confirm `app.py` is detected as Flask. `.python-version` pins Python 3.13. The checked-in `vercel.json` includes templates and Flask static files in the function bundle; `public/static/style.css` serves the stylesheet from Vercel's static asset directory.
3. Create a dedicated, empty Preview database using one of these paths:
   - **Vercel integration:** open **Storage/Integrations → Browse Marketplace → Neon → Add Integration**. Check plan costs before provisioning and do not confirm a paid resource without approval. Scope its `DATABASE_URL` to Preview only and verify the integration created a new database with no production complaint data.
   - **Neon dashboard:** choose **Create Project**, name it for Preview (for example, `frugal-preview`), select a region near the Vercel function, and use the new project's default database and role. Do not branch from a database that may contain real complaints.
4. In that new Neon project's **Connect** panel, select the database and role, then copy the pooled connection string (the hostname includes `-pooler`). Keep it private.
5. In Vercel **Project Settings → Environment Variables**, set `DATABASE_URL` for **Preview only** to that pooled URL, unless the Neon integration has already injected it. Also set `SECRET_KEY` (generate a long random value, e.g. `python -c "import secrets; print(secrets.token_urlsafe(48))"`) and the confirmed `INSTITUTIONAL_EMAIL_DOMAINS`. The staff login uses individual accounts; `ADMIN_PASSWORD` is not used by these routes. Ensure Vercel system environment variables are exposed. Leave `APP_BASE_URL` unset for Preview: the app uses Vercel's per-deployment `VERCEL_URL` to form HTTPS QR links. Set `APP_BASE_URL` only if you want a specific stable alias. `DB_POOL_MAX_SIZE` is optional; it defaults to `5`. Do not add Production variables yet.
6. Trigger a Preview Deployment from a branch. Verify complaint submission, save its one-time access code, track the complaint, sign in at `/staff/login`, update status, and check `/admin`. Confirm generated QR links use that Preview deployment URL.
7. Check Vercel function logs for import and database errors. The app requires `DATABASE_URL` and `SECRET_KEY` when `VERCEL=1`, but does not migrate the database during import. Alembic migrations must be separately rehearsed and explicitly run against a dedicated database. Do not run one against the existing Preview Neon database in this stage.

Do not set the production domain or promote a deployment until the preview has been reviewed. No production deployment or real-data migration is performed by this project setup. For PostgreSQL integration tests, set `POSTGRES_TEST_DATABASE_URL` to a dedicated disposable PostgreSQL database using a direct (non-pooled) connection string, then run `python -m unittest discover -s tests -v`. The test suite creates a uniquely named schema and synthetic records there, then removes only that schema. Never point this variable at a database containing real complaints. See [SECURITY_NOTES.md](SECURITY_NOTES.md) for the backup and isolated restore procedure.
