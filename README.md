# QR-Based Smart College Maintenance Complaint Management Process

## Project purpose

This is a college process-innovation prototype for submitting and tracking campus maintenance complaints. It uses Flask and HTML/CSS, with SQLite locally and Neon PostgreSQL when `DATABASE_URL` is configured. New complaints start with `Pending` status. Staff routes use a shared password; students use a per-complaint access code. The project does not use AI/ML.

## Phase 1 features

- Complaint form for name, roll number, location, and problem description
- Automatic category assignment from the description using keyword rules (Electrical, Plumbing, Equipment, Furniture, Network, or Other)
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

- `/dashboard` displays all submitted complaints and counts for Total, Pending, In Progress, and Resolved
- Open a complaint to view its details and change its status to Pending, In Progress, or Resolved
- Status updates are saved in the SQLite `complaints` table
- The maintenance dashboard, complaint detail/status update, and admin analytics require staff sign-in

## Phase 4A: automatic categorization and priority

- Complaint category and priority are assigned from description keywords at submission time
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

```powershell
.\.venv\Scripts\python.exe app.py
```

Run the command from the project directory. The app loads settings from a local `.env` file at startup and gives process environment variables precedence. Set `ADMIN_PASSWORD` in `.env` to enable staff access; `.env` is ignored by Git and must not be committed. A blank example is in `.env.example`. Open http://127.0.0.1:5000. The `complaints.db` SQLite database is created in the project directory if it does not exist; existing complaint records are kept. The QR gallery is at http://127.0.0.1:5000/qr-codes.

For macOS/Linux, run the same command from the project directory:

```bash
python app.py
```

`ADMIN_PASSWORD` enables staff sign-in at `/staff/login`. The same password protects `/dashboard`, complaint detail and status updates, and `/admin`. If no password is configured, staff routes stay inaccessible and the sign-in page explains how to configure them. Set `FLASK_SECRET_KEY` to a private random value when the session must survive restarts; without it, a temporary random key is generated at startup and staff sessions expire when the process restarts. Do not commit either value.

By default, QR URLs use `http://localhost:5000`. To scan a printed code with a phone, point it to the computer running Flask and allow Flask to accept local network connections. Set `APP_BASE_URL` to that computer's LAN IP and `APP_HOST` to `0.0.0.0` before starting Flask. Replace `192.168.1.20` with the computer's LAN IP:

```powershell
$env:APP_BASE_URL = "http://192.168.1.20:5000"
$env:APP_HOST = "0.0.0.0"
python app.py
```

Keep the phone and computer on the same network. For same-computer testing, leave both settings at their defaults.

## Demonstration workflow

1. Open `/qr-codes` and choose a campus location. Scan its code or open its URL; the location should already be filled and locked.
2. Submit a synthetic complaint. For a clear classification example, enter `Exposed wire near the projector`. The result should show an Electrical category, High priority, Pending status, a complaint ID, and a one-time access code.
3. Save the complaint ID and access code, then open `/student` and use both to view the current status.
4. Open `/staff/login`, sign in with the password set above, and open the new complaint from the maintenance dashboard. Change the status to In Progress or Resolved.
5. Return to the student tracking page and check the same complaint again. The new status should be visible. Open the admin overview to see its totals, category, location, and status reflected in analytics.
6. Try an empty required field to see the validation message. Sign out from a staff page when finished.

The QR gallery defaults to `localhost`, which works on the same computer. For a phone scan, use the LAN setup below and keep the phone and computer on the same network. Restart the app after changing these environment settings.

## Test a complaint submission

1. Open the home page and fill in all required fields.
2. Enter your name, roll number, location, and description, then submit. Category and priority are assigned automatically from description keywords.
3. The confirmation page displays the generated complaint ID and `Pending` status plus the one-time 8-digit access code. Keep both credentials for tracking. Previously issued longer access codes remain accepted.
4. Try submitting with a required field blank to see validation.

To try QR location identification, open `/qr-codes`, scan a code or click its URL, and confirm its location is already filled in and read-only. Submit the complaint and confirm the selected location appears on the confirmation screen and is saved in the `location` database column.

To add another QR location, add its name to `QR_LOCATIONS` in `app.py`. It will appear in the gallery automatically. The QR SVG endpoint can also generate a one-off code at `/qr-code/<location>.svg`; its destination uses `APP_BASE_URL` when configured, otherwise it uses Vercel's `VERCEL_URL` or the local `http://localhost:5000` default.

The database can be inspected with any SQLite browser or the `sqlite3` command-line tool. Complaints are stored in the `complaints` table with `id`, `complaint_id`, `name`, `roll_number`, `location`, `category`, `description`, `status`, `priority`, `created_at`, and nullable `access_code_hash` columns. The database is local runtime data and is excluded from Git; starting the app creates a fresh local database when one does not exist. Existing rows are preserved during the additive migration and have no student access code unless one was created with the complaint.

This is a local development prototype. Flask's built-in server is intended for development, not public production deployment.

## Vercel preview with Neon PostgreSQL

The Flask application supports the existing local SQLite workflow and uses PostgreSQL when `DATABASE_URL` is set. PostgreSQL connections use a small lazy connection pool (`DB_POOL_MAX_SIZE`, default `5`) suited to warm serverless instances. Schema setup is additive and safe to run more than once. Existing SQLite complaints are never copied or migrated automatically.

1. Push this project to a GitHub repository. Confirm `.env` and `complaints.db` are absent from the repository; `.gitignore` excludes both.
2. In the Vercel dashboard, choose **Add New → Project**, import the repository, and confirm `app.py` is detected as Flask. `.python-version` pins Python 3.13. The checked-in `vercel.json` includes templates and Flask static files in the function bundle; `public/static/style.css` serves the stylesheet from Vercel's static asset directory.
3. Create a dedicated, empty Preview database using one of these paths:
   - **Vercel integration:** open **Storage/Integrations → Browse Marketplace → Neon → Add Integration**. Check plan costs before provisioning and do not confirm a paid resource without approval. Scope its `DATABASE_URL` to Preview only and verify the integration created a new database with no production complaint data.
   - **Neon dashboard:** choose **Create Project**, name it for Preview (for example, `frugal-preview`), select a region near the Vercel function, and use the new project's default database and role. Do not branch from a database that may contain real complaints.
4. In that new Neon project's **Connect** panel, select the database and role, then copy the pooled connection string (the hostname includes `-pooler`). Keep it private.
5. In Vercel **Project Settings → Environment Variables**, set `DATABASE_URL` for **Preview only** to that pooled URL, unless the Neon integration has already injected it. Also set `SECRET_KEY` (generate a long random value, e.g. `python -c "import secrets; print(secrets.token_urlsafe(48))"`) and `ADMIN_PASSWORD`. Ensure Vercel system environment variables are exposed. Leave `APP_BASE_URL` unset for Preview: the app uses Vercel's per-deployment `VERCEL_URL` to form HTTPS QR links. Set `APP_BASE_URL` only if you want a specific stable alias. `DB_POOL_MAX_SIZE` is optional; it defaults to `5`. Do not add Production variables yet.
6. Trigger a Preview Deployment from a branch. Verify complaint submission, save its one-time access code, track the complaint, sign in at `/staff/login`, update status, and check `/admin`. Confirm generated QR links use that Preview deployment URL.
7. Check Vercel function logs for import, database, and schema errors. PostgreSQL schema setup runs when the Flask function imports; the app requires `DATABASE_URL` and `SECRET_KEY` when `VERCEL=1`. Changing environment variables requires a new deployment.

Do not set the production domain or promote a deployment until the preview has been reviewed. No production deployment or real-data migration is performed by this project setup. For PostgreSQL integration tests, set `POSTGRES_TEST_DATABASE_URL` to a dedicated disposable PostgreSQL database using a direct (non-pooled) connection string, then run `python -m unittest discover -s tests -v`. The test suite creates a uniquely named schema and synthetic records there, then removes only that schema. Never point this variable at a database containing real complaints.
