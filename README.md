# QR-Based Smart College Maintenance Complaint Management Process

## Project purpose

This is a college process-innovation prototype for submitting and tracking campus maintenance complaints. It uses Flask, HTML/CSS, and SQLite. New complaints start with `Pending` status. The prototype does not include authentication, AI/ML, or external service integrations.

## Phase 1 features

- Complaint form for name, roll number, location, and problem description
- Automatic category assignment from the description using keyword rules (Electrical, Plumbing, Equipment, Furniture, Network, or Other)
- Automatic priority assignment as High, Medium, or Low using keyword rules
- Unique complaint ID generated for each submission
- SQLite persistence with status and submission timestamp
- Confirmation page showing the complaint ID and status

## Phase 2: location QR codes

- QR links for M Block, PI Block, E Block, H Block, Boys Hostel, and Girls Hostel
- QR links open `/complaint?location=...` with the location prefilled and read-only
- QR-derived location is taken from the URL when saving, then stored in the existing `location` column
- `/qr-codes` displays the QR codes and offers SVG downloads

AI/ML and authentication are not included.

## Phase 3: maintenance dashboard

- `/dashboard` displays all submitted complaints and counts for Total, Pending, In Progress, and Resolved
- Open a complaint to view its details and change its status to Pending, In Progress, or Resolved
- Status updates are saved in the SQLite `complaints` table
- No authentication is implemented; the dashboard is available to anyone who can access the app

## Phase 4A: automatic categorization and priority

- Complaint category and priority are assigned from description keywords at submission time
- Category uses the first matching group in this order: Electrical, Plumbing, Equipment, Furniture, Network; otherwise Other
- Priority checks High terms first, then Medium, then Low; if nothing matches, it defaults to Medium
- Water leaking near an electrical fitting is treated as High priority
- The SQLite table receives a `priority` column automatically; existing rows without a value display as Medium
- Dashboard table includes a priority badge and priority filter
- No ML or external APIs are used

## Phase 4: student dashboard

- `/student` looks up complaints using the submitted roll number (case-insensitive)
- Complaints now also collect a college roll number, which is used for Student Dashboard lookup
- Shows that roll number's complaint counts, list, priority, and read-only complaint details
- Student complaint details have no status update control
- Roll-number lookup is a prototype convenience, not authentication; anyone who knows a roll number can enter it
- Older complaints receive a blank roll number because it was not collected before this update

## Phase 5: admin dashboard and analytics

- `/admin` is a read-only overview with total, status, and high-priority counts
- Displays complaints by category and location, status distribution, and monthly complaint totals
- Highlights the most reported location and common category, plus the ten most recent complaints
- Charts are drawn with local SVG/CSS and require no external APIs or chart dependencies
- The admin dashboard has no authentication yet

## Requirements

- Python 3.9 or newer
- pip

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

## Run

```bash
python app.py
```

Open the local address printed by Flask, usually http://127.0.0.1:5000. The `complaints.db` SQLite database is created in the project directory the first time the app starts. The QR gallery is at http://localhost:5000/qr-codes.

By default, QR URLs use `http://localhost:5000`. To scan a printed code with a phone, point it to the computer running Flask and allow Flask to accept local network connections. Set `APP_BASE_URL` to that computer's LAN IP and `APP_HOST` to `0.0.0.0` before starting Flask. Replace `192.168.1.20` with the computer's LAN IP:

```powershell
$env:APP_BASE_URL = "http://192.168.1.20:5000"
$env:APP_HOST = "0.0.0.0"
python app.py
```

Keep the phone and computer on the same network. For same-computer testing, leave both settings at their defaults.

## Test a complaint submission

1. Open the home page and fill in all required fields.
2. Enter your name, roll number, location, and description, then submit. Category and priority are assigned automatically from description keywords.
3. The confirmation page displays the generated complaint ID and `Pending` status. Keep the ID for reference.
4. Try submitting with a required field blank to see validation.

To try QR location identification, open `/qr-codes`, scan a code or click its URL, and confirm its location is already filled in and read-only. Submit the complaint and confirm the selected location appears on the confirmation screen and is saved in the `location` database column.

To add another QR location, add its name to `QR_LOCATIONS` in `app.py`. It will appear in the gallery automatically. The QR SVG endpoint can also generate a one-off code at `/qr-code/<location>.svg`; its destination uses the configured `APP_BASE_URL`.

The database can be inspected with any SQLite browser or the `sqlite3` command-line tool. Complaints are stored in the `complaints` table with `id`, `complaint_id`, `name`, `roll_number`, `location`, `category`, `description`, `status`, `priority`, and `created_at` columns. The database is local runtime data and is excluded from Git; starting the app creates a fresh local database when one does not exist.

This is a local development prototype. Flask's built-in server is intended for development, not public production deployment.
