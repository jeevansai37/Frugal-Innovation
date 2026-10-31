from datetime import datetime, timezone
from collections import deque
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
from sqlalchemy.exc import SQLAlchemyError

from campuscare.complaints import (
    ComplaintNotFoundOrForbidden,
    InvalidComplaintUpdate,
    get_staff_complaint,
    get_staff_complaint_history,
    list_complaint_history,
    list_staff_complaints,
    update_complaint_status,
)
from campuscare.database import engine_for_url, session_factory, sqlite_url
from campuscare.identity import authenticate_staff, resolve_session_user, session_claims
from campuscare.permissions import has_permission


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
app.config["LOGIN_RATE_LIMIT_SECRET"] = os.environ.get("LOGIN_RATE_LIMIT_SECRET") or app.config["SECRET_KEY"]
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
        if current_staff_user() is None:
            return redirect(url_for("staff_login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)
    return wrapped_view


def get_identity_db():
    if "identity_db" not in g:
        database_url = app.config.get("DATABASE_URL") or sqlite_url(app.config["DATABASE"])
        g.identity_engine = engine_for_url(database_url)
        g.identity_db = session_factory(g.identity_engine)()
    return g.identity_db


def current_staff_user():
    if "staff_user" in g:
        return g.staff_user
    claims = session.get("staff_identity")
    if not claims:
        g.staff_user = None
        return None
    try:
        g.staff_user = resolve_session_user(get_identity_db(), claims)
    except SQLAlchemyError:
        g.staff_user = None
    if g.staff_user is None:
        session.pop("staff_identity", None)
    return g.staff_user


@app.route("/staff/login", methods=["GET", "POST"])
def staff_login():
    error = None
    if request.method == "POST":
        try:
            user = authenticate_staff(
                get_identity_db(),
                request.form.get("email", ""),
                request.form.get("password", ""),
                request.remote_addr,
                rate_limit_secret=app.config["LOGIN_RATE_LIMIT_SECRET"],
            )
        except SQLAlchemyError:
            user = None
            error = "Staff access is not initialized yet. Contact the CampusCare administrator."
        if user is not None:
            session.clear()
            session["staff_identity"] = session_claims(user)
            _csrf_token()
            target = request.args.get("next", "")
            if not target.startswith("/") or target.startswith("//") or "\\" in target:
                target = url_for("dashboard")
            return redirect(target)
        if error is None:
            error = "Email or password was not accepted. Check your details or contact an administrator."
    return render_template("staff_login.html", error=error,
                           configured=True)


@app.post("/staff/logout")
def staff_logout():
    session.clear()
    return redirect(url_for("home"))


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
    identity_db = g.pop("identity_db", None)
    identity_engine = g.pop("identity_engine", None)
    if identity_db is not None:
        identity_db.close()
    if identity_engine is not None:
        identity_engine.dispose()
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

    def rollback(self):
        self.connection.rollback()


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


@app.get("/")
def home():
    return render_template("home.html")


@app.route("/complaint", methods=["GET", "POST"])
def complaint_form():
    locked_location = request.args.get("location", "").strip()
    errors = []
    values = {field: "" for field in ("name", "roll_number", "location", "category", "description")}
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
        if values["category"] and values["category"] not in CATEGORIES:
            errors.append("Choose a valid complaint category.")

        if not errors:
            category = values["category"] or categorize_complaint(values["description"])
            priority = assign_priority(values["description"])
            complaint_id = f"CMP-{datetime.now():%Y%m%d}-{secrets.token_hex(3).upper()}"
            access_code = f"{secrets.randbelow(100_000_000):08d}"
            db = get_db()
            created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            try:
                db.execute(
                    """INSERT INTO complaints
                       (complaint_id, name, roll_number, location, category, description, status, priority,
                        created_at, access_code_hash)
                       VALUES (?, ?, ?, ?, ?, ?, 'Pending', ?, ?, ?)""",
                    (complaint_id, values["name"], values["roll_number"], values["location"], category,
                     values["description"], priority, created_at, hash_access_code(access_code)),
                )
                db.execute(
                    """INSERT INTO complaint_history
                       (complaint_id, event_type, new_status, created_at, request_key)
                       VALUES (?, 'submitted', 'Pending', ?, ?)""",
                    (complaint_id, created_at, f"submitted:{complaint_id}"),
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
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
                tracking_link=(request.url_root.rstrip("/") + url_for("student_dashboard")
                               + "#" + urlencode({"complaint_id": complaint_id,
                                                  "access_code": access_code})),
            ))
            response.headers["Cache-Control"] = "no-store"
            return response

    return render_template(
        "index.html", values=values, errors=errors, categories=CATEGORIES,
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
    user = current_staff_user()
    db = get_identity_db()
    all_complaints = list_staff_complaints(db, user)
    counts = {
        "total": len(all_complaints),
        "pending": sum(row["status"] == "Pending" for row in all_complaints),
        "in_progress": sum(row["status"] == "In Progress" for row in all_complaints),
        "resolved": sum(row["status"] == "Resolved" for row in all_complaints),
    }
    selected_status = request.args.get("status", "")
    if selected_status not in COMPLAINT_STATUSES:
        selected_status = ""
    search = request.args.get("q", "")[:100]
    complaints = list_staff_complaints(
        db, user, status=selected_status, search=search,
    )
    return render_template(
        "dashboard.html", counts=counts, complaints=complaints,
        statuses=COMPLAINT_STATUSES, selected_status=selected_status, search=search,
    )


@app.get("/admin")
@staff_required
def admin_dashboard():
    if not has_permission(get_identity_db(), current_staff_user(), "report.read_global"):
        return "You do not have permission to view this report.", 403
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
                      description, status, created_at, last_updated_at, staff_remarks, access_code_hash
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
    complaint = {
        key: complaint[key]
        for key in ("complaint_id", "location", "category", "priority", "description",
                    "status", "created_at", "last_updated_at", "staff_remarks")
    }
    complaint["last_updated_at"] = complaint["last_updated_at"] or complaint["created_at"]
    history = list_complaint_history(
        get_identity_db(), complaint_id, complaint=complaint,
    )
    response = make_response(render_template(
        "student_complaint_detail.html", complaint=complaint, history=history,
    ))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/dashboard/complaint/<complaint_id>", methods=["GET", "POST"])
@staff_required
def complaint_detail(complaint_id):
    identity_db = get_identity_db()
    user = current_staff_user()
    complaint = get_staff_complaint(identity_db, user, complaint_id)
    if complaint is None:
        return render_template("not_found.html"), 404

    error = None
    update_key = secrets.token_urlsafe(24)
    if request.method == "POST":
        new_status = request.form.get("status", "")
        remarks = request.form.get("staff_remarks", "").strip()
        update_key = request.form.get("update_key", "") or secrets.token_urlsafe(24)
        try:
            update_complaint_status(
                identity_db, user, complaint_id, new_status, remarks, update_key,
            )
        except InvalidComplaintUpdate as exc:
            error = str(exc)
        except PermissionError as exc:
            error = str(exc)
        except ComplaintNotFoundOrForbidden:
            return render_template("not_found.html"), 404
        else:
            return redirect(url_for("complaint_detail", complaint_id=complaint_id, updated=1))
        complaint = get_staff_complaint(identity_db, user, complaint_id)

    history = get_staff_complaint_history(identity_db, user, complaint_id)
    if history is None:
        return render_template("not_found.html"), 404

    return render_template(
        "complaint_detail.html", complaint=complaint,
        statuses=COMPLAINT_STATUSES, error=error, history=history,
        update_key=update_key,
        updated=request.args.get("updated") == "1",
    )


if __name__ == "__main__":
    app.run(host=APP_HOST, debug=APP_HOST in ("127.0.0.1", "localhost"))
elif os.environ.get("VERCEL") == "1":
    if not app.config["DATABASE_URL"]:
        raise RuntimeError("DATABASE_URL must be configured for Vercel; SQLite is for local development.")
    if not os.environ.get("SECRET_KEY") and not os.environ.get("FLASK_SECRET_KEY"):
        raise RuntimeError("SECRET_KEY must be configured for Vercel.")
