from datetime import datetime
from collections import deque
from contextlib import closing
from functools import wraps
import hmac
import hashlib
import io
import os
from pathlib import Path
import re
import secrets
import sqlite3
from threading import Lock
from time import monotonic
from urllib.parse import urlencode

import qrcode
import qrcode.image.svg
from flask import Flask, Response, g, make_response, redirect, render_template, request, session, url_for


BASE_DIR = Path(__file__).resolve().parent


def load_local_environment(path):
    """Load simple KEY=VALUE settings without overriding the process environment."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key.isidentifier():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not os.environ.get(key):
            os.environ[key] = value


load_local_environment(BASE_DIR / ".env")
DATABASE = BASE_DIR / "complaints.db"
CATEGORIES = ["Electrical", "Plumbing", "Equipment", "Furniture", "Network", "Other"]
QR_LOCATIONS = ["M Block", "PI Block", "E Block", "H Block", "Boys Hostel", "Girls Hostel"]
COMPLAINT_STATUSES = ["Pending", "In Progress", "Resolved"]
PRIORITIES = ["High", "Medium", "Low"]
CATEGORY_KEYWORDS = {
    "Electrical": ("fan", "light", "switch", "socket", "power", "electricity", "electrical", "wiring", "wire"),
    "Plumbing": ("water", "leakage", "leaking", "leak", "pipe", "tap", "toilet", "drainage"),
    "Equipment": ("projector", "computer", "printer", "machine", "lab equipment", "equipment"),
    "Furniture": ("chair", "table", "desk", "bench", "door", "window"),
    "Network": ("wifi", "wi fi", "internet", "network", "router", "connection"),
}
HIGH_PRIORITY_KEYWORDS = (
    "fire", "shock", "electric shock", "exposed wire", "major leakage",
    "flooding", "danger", "unsafe", "emergency",
)
MEDIUM_PRIORITY_KEYWORDS = (
    "fan", "light", "projector", "computer", "printer", "wifi", "wi fi",
    "water leakage", "equipment failure",
)
LOW_PRIORITY_KEYWORDS = ("chair", "table", "desk", "paint", "minor", "cosmetic")
ACCESS_CODE_SCRYPT_N = 2 ** 14
ACCESS_CODE_SCRYPT_R = 8
ACCESS_CODE_SCRYPT_P = 1
ACCESS_CODE_SALT_BYTES = 16
TRACKING_ATTEMPT_LIMIT = 5
TRACKING_ATTEMPT_WINDOW_SECONDS = 15 * 60
_tracking_failures = {}
_tracking_failure_order = deque()
_tracking_failure_lock = Lock()
def resolve_app_base_url(environ=None):
    """Prefer an explicit public URL, then the current Vercel deployment URL."""
    values = os.environ if environ is None else environ
    configured_url = values.get("APP_BASE_URL", "").strip().rstrip("/")
    if configured_url:
        return configured_url
    deployment_host = values.get("VERCEL_URL", "").strip().strip("/")
    if deployment_host:
        if not deployment_host.lower().startswith(("http://", "https://")):
            deployment_host = f"https://{deployment_host}"
        return deployment_host.rstrip("/")
    return "http://localhost:5000"


APP_BASE_URL = resolve_app_base_url()
APP_HOST = os.environ.get("APP_HOST", "127.0.0.1")

app = Flask(__name__)
app.config["DATABASE"] = DATABASE
app.config["DATABASE_URL"] = os.environ.get("DATABASE_URL", "").strip()
app.config["SECRET_KEY"] = (os.environ.get("SECRET_KEY") or os.environ.get("FLASK_SECRET_KEY")
                            or secrets.token_hex(32))
app.config["ADMIN_PASSWORD"] = os.environ.get("ADMIN_PASSWORD", "")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("VERCEL") == "1")
app.secret_key = app.config["SECRET_KEY"]


def _csrf_token():
    token = session.get("csrf_token")
    if token is None:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


@app.before_request
def protect_post_requests():
    if request.method == "POST":
        submitted_token = request.form.get("csrf_token", "")
        if not submitted_token or not hmac.compare_digest(submitted_token, session.get("csrf_token", "")):
            return "Invalid or missing CSRF token", 400
    else:
        _csrf_token()


def staff_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not session.get("is_staff"):
            return redirect(url_for("staff_login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)
    return wrapped_view


@app.route("/staff/login", methods=["GET", "POST"])
def staff_login():
    error = None
    if request.method == "POST":
        configured_password = app.config.get("ADMIN_PASSWORD", "")
        submitted_password = request.form.get("password", "")
        if not configured_password:
            error = "Staff sign-in is not configured. Set ADMIN_PASSWORD and restart the app."
        elif hmac.compare_digest(submitted_password, configured_password):
            session.clear()
            session["is_staff"] = True
            _csrf_token()
            target = request.args.get("next", "")
            if not target.startswith("/") or target.startswith("//") or "\\" in target:
                target = url_for("dashboard")
            return redirect(target)
        else:
            error = "The staff password was not accepted."
    return render_template("staff_login.html", error=error,
                           configured=bool(app.config.get("ADMIN_PASSWORD")))


@app.post("/staff/logout")
def staff_logout():
    session.clear()
    return redirect(url_for("complaint_form"))


def get_db():
    if "db" not in g:
        database_url = app.config.get("DATABASE_URL", "")
        if database_url:
            g.db_context = _postgres_pool().connection()
            g.db = _PostgresConnection(g.db_context.__enter__())
        else:
            g.db = sqlite3.connect(app.config["DATABASE"])
            g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(error=None):
    db = g.pop("db", None)
    db_context = g.pop("db_context", None)
    if db_context is not None:
        db_context.__exit__(type(error) if error else None, error, error.__traceback__ if error else None)
    elif db is not None:
        db.close()


class _PostgresConnection:
    """Expose the small sqlite-style execute API used by the Flask routes."""

    def __init__(self, connection):
        self.connection = connection

    def execute(self, query, parameters=()):
        return self.connection.execute(query.replace("?", "%s"), parameters)

    def commit(self):
        self.connection.commit()


_postgres_connection_pool = None


def _postgres_pool():
    global _postgres_connection_pool
    if _postgres_connection_pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        _postgres_connection_pool = ConnectionPool(
            conninfo=app.config["DATABASE_URL"],
            min_size=0,
            max_size=int(os.environ.get("DB_POOL_MAX_SIZE", "5")),
            timeout=10,
            kwargs={"row_factory": dict_row},
            open=False,
        )
        _postgres_connection_pool.open()
    return _postgres_connection_pool


def init_db():
    if app.config.get("DATABASE_URL"):
        with _postgres_pool().connection() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS complaints (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    complaint_id TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    roll_number TEXT NOT NULL DEFAULT '',
                    location TEXT NOT NULL,
                    category TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'Pending',
                    priority TEXT NOT NULL DEFAULT 'Medium',
                    created_at TEXT NOT NULL,
                    access_code_hash TEXT
                )"""
            )
            connection.execute("ALTER TABLE complaints ADD COLUMN IF NOT EXISTS priority TEXT NOT NULL DEFAULT 'Medium'")
            connection.execute("ALTER TABLE complaints ADD COLUMN IF NOT EXISTS roll_number TEXT NOT NULL DEFAULT ''")
            connection.execute("ALTER TABLE complaints ADD COLUMN IF NOT EXISTS access_code_hash TEXT")
        return

    with closing(sqlite3.connect(app.config["DATABASE"])) as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS complaints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                complaint_id TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                roll_number TEXT NOT NULL DEFAULT '',
                location TEXT NOT NULL,
                category TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Pending',
                priority TEXT NOT NULL DEFAULT 'Medium',
                created_at TEXT NOT NULL,
                access_code_hash TEXT
            )"""
        )
        columns = {row[1] for row in db.execute("PRAGMA table_info(complaints)")}
        if "priority" not in columns:
            db.execute("ALTER TABLE complaints ADD COLUMN priority TEXT NOT NULL DEFAULT 'Medium'")
        if "roll_number" not in columns:
            db.execute("ALTER TABLE complaints ADD COLUMN roll_number TEXT NOT NULL DEFAULT ''")
        if "access_code_hash" not in columns:
            db.execute("ALTER TABLE complaints ADD COLUMN access_code_hash TEXT")
        db.commit()


def categorize_complaint(description):
    text = description.casefold().replace("-", " ")
    for category, keywords in CATEGORY_KEYWORDS.items():
        if _contains_keyword(text, keywords):
            return category
    return "Other"


def _contains_keyword(text, keywords):
    return any(re.search(rf"\b{re.escape(keyword)}\b", text) for keyword in keywords)


def assign_priority(description):
    text = description.casefold().replace("-", " ")
    # Water leaking close to an electrical fitting is treated as an immediate hazard.
    electrical_risk = _contains_keyword(text, ("electric", "electrical", "socket", "power", "wire"))
    water_leak = _contains_keyword(text, ("water", "leak", "leaking", "leakage", "flood"))
    if (electrical_risk and water_leak) or _contains_keyword(text, HIGH_PRIORITY_KEYWORDS):
        return "High"
    if _contains_keyword(text, MEDIUM_PRIORITY_KEYWORDS):
        return "Medium"
    if _contains_keyword(text, LOW_PRIORITY_KEYWORDS):
        return "Low"
    return "Medium"


def hash_access_code(access_code):
    salt = secrets.token_bytes(ACCESS_CODE_SALT_BYTES)
    digest = hashlib.scrypt(
        access_code.encode("utf-8"), salt=salt,
        n=ACCESS_CODE_SCRYPT_N, r=ACCESS_CODE_SCRYPT_R, p=ACCESS_CODE_SCRYPT_P,
        dklen=32,
    )
    return (f"scrypt${ACCESS_CODE_SCRYPT_N}${ACCESS_CODE_SCRYPT_R}${ACCESS_CODE_SCRYPT_P}"
            f"${salt.hex()}${digest.hex()}")


def verify_access_code(access_code, stored_hash):
    """Verify new salted scrypt hashes and the legacy SHA-256 format."""
    if not stored_hash:
        return False
    if stored_hash.startswith("scrypt$"):
        try:
            scheme, n, r, p, salt_hex, expected_hex = stored_hash.split("$")
            if (scheme != "scrypt" or int(n) != ACCESS_CODE_SCRYPT_N
                    or int(r) != ACCESS_CODE_SCRYPT_R or int(p) != ACCESS_CODE_SCRYPT_P):
                return False
            salt = bytes.fromhex(salt_hex)
            expected = bytes.fromhex(expected_hex)
            if len(salt) != ACCESS_CODE_SALT_BYTES or len(expected) != 32:
                return False
            actual = hashlib.scrypt(
                access_code.encode("utf-8"), salt=salt,
                n=ACCESS_CODE_SCRYPT_N, r=ACCESS_CODE_SCRYPT_R, p=ACCESS_CODE_SCRYPT_P,
                dklen=32,
            )
            return hmac.compare_digest(actual, expected)
        except (ValueError, TypeError):
            return False

    # Existing complaints store unsalted SHA-256 hashes of their original long codes.
    if not re.fullmatch(r"[a-f0-9]{64}", stored_hash):
        return False
    legacy_digest = hashlib.sha256(access_code.encode("utf-8")).hexdigest()
    return hmac.compare_digest(legacy_digest, stored_hash)


def _prune_tracking_failures(now):
    cutoff = now - TRACKING_ATTEMPT_WINDOW_SECONDS
    while _tracking_failure_order and _tracking_failure_order[0][0] <= cutoff:
        timestamp, key = _tracking_failure_order.popleft()
        attempts = _tracking_failures.get(key)
        if attempts and attempts[0] == timestamp:
            attempts.popleft()
            if not attempts:
                _tracking_failures.pop(key, None)


def register_tracking_attempt(key):
    now = monotonic()
    with _tracking_failure_lock:
        _prune_tracking_failures(now)
        attempts = _tracking_failures.get(key, ())
        if len(attempts) >= TRACKING_ATTEMPT_LIMIT:
            return True
        _tracking_failures.setdefault(key, deque()).append(now)
        _tracking_failure_order.append((now, key))
        return False


def clear_tracking_failures(key):
    with _tracking_failure_lock:
        _tracking_failures.pop(key, None)


@app.route("/", methods=["GET", "POST"])
@app.route("/complaint", methods=["GET", "POST"])
def complaint_form():
    locked_location = request.args.get("location", "").strip()
    errors = []
    values = {field: "" for field in ("name", "roll_number", "location", "description")}
    values["location"] = locked_location

    if request.method == "POST":
        values = {field: request.form.get(field, "").strip() for field in values}
        # The query parameter is the trusted source for a QR-locked location.
        if locked_location:
            values["location"] = locked_location
        for field, label in (("name", "Name"), ("roll_number", "Roll number"), ("location", "Location"),
                             ("description", "Problem description")):
            if not values[field]:
                errors.append(f"{label} is required.")
        max_lengths = {"name": 100, "roll_number": 50, "location": 150, "description": 2000}
        labels = {"name": "Name", "roll_number": "Roll number", "location": "Location",
                  "description": "Problem description"}
        for field, max_length in max_lengths.items():
            if len(values[field]) > max_length:
                errors.append(f"{labels[field]} must be {max_length} characters or fewer.")

        if not errors:
            category = categorize_complaint(values["description"])
            priority = assign_priority(values["description"])
            complaint_id = f"CMP-{datetime.now():%Y%m%d}-{secrets.token_hex(3).upper()}"
            access_code = f"{secrets.randbelow(100_000_000):08d}"
            db = get_db()
            db.execute(
                """INSERT INTO complaints
                   (complaint_id, name, roll_number, location, category, description, status, priority,
                    created_at, access_code_hash)
                   VALUES (?, ?, ?, ?, ?, ?, 'Pending', ?, ?, ?)""",
                (complaint_id, values["name"], values["roll_number"], values["location"], category,
                 values["description"], priority, datetime.now().isoformat(timespec="seconds"),
                 hash_access_code(access_code)),
            )
            db.commit()
            complaint = {
                "complaint_id": complaint_id,
                "name": values["name"],
                "location": values["location"],
                "category": category,
                "priority": priority,
                "status": "Pending",
            }
            response = make_response(render_template(
                "success.html", complaint=complaint, access_code=access_code,
            ))
            response.headers["Cache-Control"] = "no-store"
            return response

    return render_template(
        "index.html", values=values, errors=errors,
        locked_location=locked_location,
    )


@app.get("/qr-codes")
def qr_codes():
    entries = [
        (location, f"{APP_BASE_URL}/complaint?{urlencode({'location': location})}")
        for location in QR_LOCATIONS
    ]
    return render_template("qr_codes.html", entries=entries)


@app.get("/qr-code/<path:location>.svg")
def qr_code(location):
    if not location.strip():
        return "Location is required", 400
    target_url = f"{APP_BASE_URL}/complaint?{urlencode({'location': location})}"
    image = qrcode.make(target_url, image_factory=qrcode.image.svg.SvgImage, box_size=8, border=4)
    output = io.BytesIO()
    image.save(output)
    response = Response(output.getvalue(), mimetype="image/svg+xml")
    if request.args.get("download") == "1":
        safe_location = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in location)
        response.headers["Content-Disposition"] = f'attachment; filename="{safe_location}-qr.svg"'
    return response


@app.get("/dashboard")
@staff_required
def dashboard():
    db = get_db()
    counts = db.execute(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS pending,
                  SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS in_progress,
                  SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS resolved
           FROM complaints""",
        tuple(COMPLAINT_STATUSES),
    ).fetchone()
    selected_priority = request.args.get("priority", "")
    if selected_priority not in PRIORITIES:
        selected_priority = ""
    query = """SELECT complaint_id, name, location, category, description, status, priority, created_at
               FROM complaints"""
    params = ()
    if selected_priority:
        query += " WHERE priority = ?"
        params = (selected_priority,)
    query += " ORDER BY created_at DESC, id DESC"
    complaints = db.execute(query, params).fetchall()
    return render_template("dashboard.html", counts=counts, complaints=complaints,
                           priorities=PRIORITIES, selected_priority=selected_priority)


@app.get("/admin")
@staff_required
def admin_dashboard():
    db = get_db()
    summary = db.execute(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS pending,
                  SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS in_progress,
                  SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS resolved,
                  SUM(CASE WHEN COALESCE(priority, 'Medium') = ? THEN 1 ELSE 0 END) AS high_priority
           FROM complaints""",
        (*COMPLAINT_STATUSES, "High"),
    ).fetchone()

    category_rows = db.execute(
        """SELECT category, COUNT(*) AS total FROM complaints
           WHERE category IN (?, ?, ?, ?, ?, ?) GROUP BY category""",
        tuple(CATEGORIES),
    ).fetchall()
    category_values = {row["category"]: row["total"] for row in category_rows}
    max_category = max(category_values.values(), default=0)
    categories = [
        {"label": label, "total": category_values.get(label, 0),
         "width": round(category_values.get(label, 0) * 100 / max_category) if max_category else 0}
        for label in CATEGORIES
    ]

    location_rows = db.execute(
        """SELECT COALESCE(NULLIF(TRIM(location), ''), 'Unknown') AS location, COUNT(*) AS total
           FROM complaints GROUP BY COALESCE(NULLIF(TRIM(location), ''), 'Unknown')
           ORDER BY total DESC, location ASC"""
    ).fetchall()
    max_location = max((row["total"] for row in location_rows), default=0)
    locations = [
        {"label": row["location"], "total": row["total"],
         "width": round(row["total"] * 100 / max_location) if max_location else 0}
        for row in location_rows
    ]

    status_values = [
        ("Pending", summary["pending"] or 0, "#d99b24"),
        ("In Progress", summary["in_progress"] or 0, "#268d88"),
        ("Resolved", summary["resolved"] or 0, "#57936d"),
    ]
    status_total = sum(value for _, value, _ in status_values)
    status_segments = []
    offset = 0
    for label, value, color in status_values:
        percentage = (value * 100 / status_total) if status_total else 0
        status_segments.append({
            "label": label, "total": value, "color": color,
            "percentage": round(percentage, 2), "offset": offset,
        })
        offset += percentage

    monthly_rows = db.execute(
        """SELECT substr(created_at, 1, 7) AS month, COUNT(*) AS total
           FROM complaints WHERE created_at IS NOT NULL AND length(created_at) >= 7
           GROUP BY substr(created_at, 1, 7) ORDER BY month ASC"""
    ).fetchall()
    max_month = max((row["total"] for row in monthly_rows), default=0)
    chart_left, chart_right, chart_top, chart_bottom = 42, 578, 24, 166
    time_points = []
    for index, row in enumerate(monthly_rows):
        x = ((chart_left + chart_right) / 2 if len(monthly_rows) == 1 else
             chart_left + (chart_right - chart_left) * index / (len(monthly_rows) - 1))
        y = chart_bottom - (chart_bottom - chart_top) * row["total"] / max_month if max_month else chart_bottom
        try:
            month_label = datetime.strptime(row["month"], "%Y-%m").strftime("%b %Y")
        except ValueError:
            month_label = row["month"]
        time_points.append({"x": round(x, 1), "y": round(y, 1),
                            "label": month_label, "total": row["total"]})
    time_polyline = " ".join(f"{point['x']},{point['y']}" for point in time_points)

    most_reported_location = location_rows[0]["location"] if location_rows else None
    most_common_category = max(CATEGORIES, key=lambda label: category_values.get(label, 0)) if max_category else None
    recent_complaints = db.execute(
        """SELECT complaint_id, location, category, status, created_at
           FROM complaints ORDER BY created_at DESC, id DESC LIMIT ?""",
        (10,),
    ).fetchall()
    return render_template(
        "admin_dashboard.html", summary=summary, categories=categories,
        locations=locations, status_segments=status_segments, status_total=status_total,
        time_points=time_points, time_polyline=time_polyline,
        most_reported_location=most_reported_location,
        most_common_category=most_common_category,
        recent_complaints=recent_complaints,
    )


@app.route("/student", methods=["GET", "POST"])
def student_dashboard():
    if request.method == "GET":
        return render_template("student_dashboard.html")

    complaint_id = request.form.get("complaint_id", "").strip()
    access_code = request.form.get("access_code", "").strip()
    attempt_key = (request.remote_addr or "unknown", complaint_id[:50].casefold())
    if register_tracking_attempt(attempt_key):
        response = make_response(render_template("student_not_found.html", rate_limited=True), 429)
        response.headers["Cache-Control"] = "no-store"
        return response

    complaint = None
    if complaint_id and len(complaint_id) <= 50:
        complaint = get_db().execute(
            """SELECT complaint_id, location, category, COALESCE(priority, 'Medium') AS priority,
                      description, status, created_at, access_code_hash
               FROM complaints WHERE complaint_id = ?""",
            (complaint_id,),
        ).fetchone()
    stored_hash = complaint["access_code_hash"] if complaint is not None else None
    is_new_hash = bool(stored_hash and stored_hash.startswith("scrypt$"))
    code_format_valid = (
        re.fullmatch(r"[0-9]{8}", access_code) is not None
        if is_new_hash else bool(access_code) and len(access_code) <= 100
    )
    if not code_format_valid or not verify_access_code(access_code, stored_hash):
        response = make_response(render_template("student_not_found.html"), 404)
        response.headers["Cache-Control"] = "no-store"
        return response

    clear_tracking_failures(attempt_key)
    response = make_response(render_template("student_complaint_detail.html", complaint=complaint))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/dashboard/complaint/<complaint_id>", methods=["GET", "POST"])
@staff_required
def complaint_detail(complaint_id):
    db = get_db()
    complaint = db.execute(
        """SELECT complaint_id, name, location, category, description, status, priority, created_at
           FROM complaints WHERE complaint_id = ?""",
        (complaint_id,),
    ).fetchone()
    if complaint is None:
        return render_template("not_found.html"), 404

    error = None
    if request.method == "POST":
        new_status = request.form.get("status", "")
        if new_status not in COMPLAINT_STATUSES:
            error = "Choose a valid status."
        else:
            db.execute(
                "UPDATE complaints SET status = ? WHERE complaint_id = ?",
                (new_status, complaint_id),
            )
            db.commit()
            return redirect(url_for("complaint_detail", complaint_id=complaint_id, updated=1))

    return render_template(
        "complaint_detail.html", complaint=complaint,
        statuses=COMPLAINT_STATUSES, error=error,
        updated=request.args.get("updated") == "1",
    )


if __name__ == "__main__":
    init_db()
    app.run(host=APP_HOST, debug=APP_HOST in ("127.0.0.1", "localhost"))
elif os.environ.get("VERCEL") == "1":
    if not app.config["DATABASE_URL"]:
        raise RuntimeError("DATABASE_URL must be configured for Vercel; SQLite is for local development.")
    if not os.environ.get("SECRET_KEY") and not os.environ.get("FLASK_SECRET_KEY"):
        raise RuntimeError("SECRET_KEY must be configured for Vercel.")
    init_db()
