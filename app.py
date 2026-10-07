from datetime import datetime
import io
import os
from pathlib import Path
import secrets
import sqlite3
from urllib.parse import urlencode

import qrcode
import qrcode.image.svg
from flask import Flask, Response, g, redirect, render_template, request, url_for


BASE_DIR = Path(__file__).resolve().parent
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
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:5000").rstrip("/")
APP_HOST = os.environ.get("APP_HOST", "127.0.0.1")

app = Flask(__name__)
app.config["DATABASE"] = DATABASE


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with sqlite3.connect(app.config["DATABASE"]) as db:
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
                created_at TEXT NOT NULL
            )"""
        )
        columns = {row[1] for row in db.execute("PRAGMA table_info(complaints)")}
        if "priority" not in columns:
            db.execute("ALTER TABLE complaints ADD COLUMN priority TEXT NOT NULL DEFAULT 'Medium'")
        if "roll_number" not in columns:
            db.execute("ALTER TABLE complaints ADD COLUMN roll_number TEXT NOT NULL DEFAULT ''")


def categorize_complaint(description):
    text = description.casefold().replace("-", " ")
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            return category
    return "Other"


def assign_priority(description):
    text = description.casefold().replace("-", " ")
    # Water leaking close to an electrical fitting is treated as an immediate hazard.
    electrical_risk = any(word in text for word in ("electric", "electrical", "socket", "power", "wire"))
    water_leak = any(word in text for word in ("water", "leak", "leaking", "leakage", "flood"))
    if (electrical_risk and water_leak) or any(word in text for word in HIGH_PRIORITY_KEYWORDS):
        return "High"
    if any(word in text for word in MEDIUM_PRIORITY_KEYWORDS):
        return "Medium"
    if any(word in text for word in LOW_PRIORITY_KEYWORDS):
        return "Low"
    return "Medium"


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

        if not errors:
            category = categorize_complaint(values["description"])
            priority = assign_priority(values["description"])
            complaint_id = f"CMP-{datetime.now():%Y%m%d}-{secrets.token_hex(3).upper()}"
            db = get_db()
            db.execute(
                """INSERT INTO complaints
                   (complaint_id, name, roll_number, location, category, description, status, priority, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'Pending', ?, ?)""",
                (complaint_id, values["name"], values["roll_number"], values["location"], category,
                 values["description"], priority, datetime.now().isoformat(timespec="seconds")),
            )
            db.commit()
            return redirect(url_for("success", complaint_id=complaint_id))

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


@app.get("/student")
def student_dashboard():
    student_roll_number = request.args.get("roll_number", "").strip()
    complaints = []
    counts = None
    if student_roll_number:
        db = get_db()
        counts = db.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS pending,
                      SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS in_progress,
                      SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS resolved
               FROM complaints WHERE roll_number = ? COLLATE NOCASE""",
            (*COMPLAINT_STATUSES, student_roll_number),
        ).fetchone()
        complaints = db.execute(
            """SELECT complaint_id, location, category, priority, description, status, created_at
               FROM complaints WHERE roll_number = ? COLLATE NOCASE
               ORDER BY created_at DESC, id DESC""",
            (student_roll_number,),
        ).fetchall()
    return render_template(
        "student_dashboard.html", student_roll_number=student_roll_number,
        complaints=complaints, counts=counts,
    )


@app.get("/student/complaint/<complaint_id>")
def student_complaint_detail(complaint_id):
    student_roll_number = request.args.get("roll_number", "").strip()
    if not student_roll_number:
        return render_template("student_not_found.html", student_roll_number=""), 404
    complaint = get_db().execute(
        """SELECT complaint_id, location, category, COALESCE(priority, 'Medium') AS priority,
                  description, status, created_at
           FROM complaints
           WHERE complaint_id = ? AND roll_number = ? COLLATE NOCASE""",
        (complaint_id, student_roll_number),
    ).fetchone()
    if complaint is None:
        return render_template("student_not_found.html", student_roll_number=student_roll_number), 404
    return render_template(
        "student_complaint_detail.html", complaint=complaint,
        student_roll_number=student_roll_number,
    )


@app.route("/dashboard/complaint/<complaint_id>", methods=["GET", "POST"])
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


@app.get("/success/<complaint_id>")
def success(complaint_id):
    complaint = get_db().execute(
        "SELECT complaint_id, name, category, status, priority FROM complaints WHERE complaint_id = ?",
        (complaint_id,),
    ).fetchone()
    if complaint is None:
        return "Complaint not found", 404
    return render_template("success.html", complaint=complaint)


if __name__ == "__main__":
    init_db()
    app.run(host=APP_HOST, debug=APP_HOST in ("127.0.0.1", "localhost"))
