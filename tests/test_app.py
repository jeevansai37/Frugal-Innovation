import re
from contextlib import closing
import hashlib
import secrets
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import app, hash_access_code, verify_access_code
from campuscare.database import engine_for_url, session_factory, sqlite_url
from campuscare.identity import hash_password, revoke_sessions
from campuscare.models import ComplaintHistory, StaffRoleAssignment, User, utc_now
from campuscare.migration_runner import upgrade_database


class AppFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database = app.config["DATABASE"]
        self.original_database_url = app.config["DATABASE_URL"]
        app.config["DATABASE"] = Path(self.temp_dir.name) / "test-complaints.db"
        app.config["DATABASE_URL"] = ""
        app.config["LOGIN_RATE_LIMIT_SECRET"] = "synthetic-http-rate-limit-key"
        app.config["TESTING"] = True
        upgrade_database(sqlite_url(app.config["DATABASE"]))
        engine = engine_for_url(sqlite_url(app.config["DATABASE"]))
        try:
            with session_factory(engine)() as db:
                now = utc_now()
                admin = User(
                    account_kind="staff", email="admin@iiitdwd.example",
                    normalized_email="admin@iiitdwd.example", display_name="Synthetic Admin",
                    password_hash=hash_password("synthetic-http-admin-password"), status="active",
                    email_verified_at=now, created_at=now, updated_at=now,
                )
                db.add(admin)
                db.flush()
                db.add(StaffRoleAssignment(
                    user_id=admin.user_id, role_key="SUPER_ADMIN", assigned_by_user_id=admin.user_id,
                    department_id=None, assigned_at=now,
                ))
                db.commit()
                self.admin_user_id = admin.user_id
        finally:
            engine.dispose()
        self.client = app.test_client()
        self.sign_in()

    def tearDown(self):
        app.config["DATABASE"] = self.original_database
        app.config["DATABASE_URL"] = self.original_database_url
        app.config.pop("TESTING", None)
        self.temp_dir.cleanup()

    def count_complaints(self):
        with app.app_context():
            from app import get_db
            return get_db().execute("SELECT COUNT(*) FROM complaints").fetchone()[0]

    def create_complaint(self, **overrides):
        form = {
            "name": "Demo Student",
            "roll_number": "DEMO-001",
            "location": "M Block",
            "description": "Broken classroom chair",
        }
        form.update(overrides)
        form["csrf_token"] = self.csrf_token("/complaint")
        return self.client.post("/complaint", data=form)

    def csrf_token(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
        self.assertIsNotNone(match)
        return match.group(1).decode("ascii")

    def sign_in(self, client=None):
        client = client or self.client
        page = client.get("/staff/login")
        match = re.search(rb'name="csrf_token" value="([^"]+)"', page.data)
        token = match.group(1).decode("ascii")
        return client.post("/staff/login", data={
            "email": "admin@iiitdwd.example",
            "password": "synthetic-http-admin-password",
            "csrf_token": token,
        })

    def test_homepage_and_complaint_action_are_separate(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"CampusCare", response.data)
        self.assertIn(b"Submit a Complaint", response.data)
        self.assertIn(b"Track a Complaint", response.data)
        self.assertIn(b"Staff Login", response.data)
        self.assertNotIn(b'name="roll_number"', response.data)
        form = self.client.get("/complaint")
        self.assertEqual(form.status_code, 200)
        self.assertIn(b'name="roll_number"', form.data)
        self.assertEqual(self.client.get("/staff/register").status_code, 404)

    def test_valid_submission_is_saved_and_shows_credentials_once(self):
        with patch("app.secrets.randbelow", return_value=123):
            response = self.create_complaint()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn(b"Copy Tracking Link", response.data)
        complaint_id = re.search(rb"YOUR COMPLAINT ID</span>\s*<strong>([^<]+)", response.data).group(1).decode()
        access_code = re.search(rb'id="access-code">([^<]+)', response.data).group(1).decode()
        self.assertRegex(complaint_id, r"CMP-\d{8}-[A-F0-9]{6}")
        self.assertEqual(access_code, "00000123")
        self.assertRegex(access_code, r"^[0-9]{8}$")
        self.assertEqual(self.count_complaints(), 1)
        with app.app_context():
            from app import get_db
            stored_hash = get_db().execute(
                "SELECT access_code_hash FROM complaints WHERE complaint_id = ?", (complaint_id,)
            ).fetchone()[0]
        self.assertTrue(stored_hash.startswith("scrypt$"))
        self.assertNotEqual(stored_hash, access_code)
        self.assertTrue(verify_access_code(access_code, stored_hash))
        self.assertNotEqual(stored_hash, hash_access_code(access_code))

    def test_qr_location_is_authoritative_over_submitted_field(self):
        response = self.client.post(
            "/complaint?location=PI+Block",
            data={"name": "Demo Student", "roll_number": "DEMO-001", "location": "Other",
                  "description": "Broken chair", "csrf_token": self.csrf_token("/complaint?location=PI+Block")},
        )
        self.assertEqual(response.status_code, 200)
        with app.app_context():
            from app import get_db
            location = get_db().execute("SELECT location FROM complaints").fetchone()[0]
        self.assertEqual(location, "PI Block")

    def test_blank_required_field_is_rejected_without_writing(self):
        response = self.create_complaint(name=" ")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Name is required", response.data)
        self.assertEqual(self.count_complaints(), 0)

    def test_each_field_length_limit_is_enforced_server_side(self):
        limits = {"name": 100, "roll_number": 50, "location": 150, "description": 2000}
        for field, max_length in limits.items():
            with self.subTest(field=field):
                response = self.create_complaint(**{field: "x" * (max_length + 1)})
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"characters or fewer", response.data)
                self.assertEqual(self.count_complaints(), 0)

    def test_qr_location_length_limit_is_enforced(self):
        response = self.client.post(
            "/complaint?location=" + "x" * 151,
            data={"name": "Demo Student", "roll_number": "DEMO-001", "description": "Broken chair",
                  "csrf_token": self.csrf_token("/complaint")},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Location must be 150 characters or fewer", response.data)
        self.assertEqual(self.count_complaints(), 0)

    def test_unknown_status_is_rejected_without_changing_record(self):
        self.create_complaint()
        with app.app_context():
            from app import get_db
            complaint_id = get_db().execute("SELECT complaint_id FROM complaints").fetchone()[0]
        response = self.client.post(f"/dashboard/complaint/{complaint_id}",
                                    data={"status": "Archived", "csrf_token": self.csrf_token(f"/dashboard/complaint/{complaint_id}")})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Choose a valid complaint status", response.data)
        with app.app_context():
            from app import get_db
            status = get_db().execute("SELECT status FROM complaints").fetchone()[0]
        self.assertEqual(status, "Pending")

    def submit_and_get_credentials(self, **overrides):
        response = self.create_complaint(**overrides)
        complaint_id = re.search(rb"YOUR COMPLAINT ID</span>\s*<strong>([^<]+)", response.data).group(1).decode()
        access_code = re.search(rb'id="access-code">([^<]+)', response.data).group(1).decode()
        return complaint_id, access_code

    def test_valid_access_code_shows_only_the_matching_complaint(self):
        complaint_id, access_code = self.submit_and_get_credentials()
        response = self.client.post(
            "/student",
            data={"complaint_id": complaint_id, "access_code": access_code,
                  "csrf_token": self.csrf_token("/student")},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn(complaint_id.encode(), response.data)
        self.assertIn(b"Current Status", response.data)
        self.assertIn(b"Pending", response.data)

    def test_invalid_access_code_does_not_reveal_complaint(self):
        with patch("app.secrets.randbelow", return_value=123):
            complaint_id, _ = self.submit_and_get_credentials(description="Private demo description")
        response = self.client.post(
            "/student",
            data={"complaint_id": complaint_id, "access_code": "00000456",
                  "csrf_token": self.csrf_token("/student")},
        )
        self.assertEqual(response.status_code, 404)
        self.assertIn(b"access code was not recognized", response.data)
        self.assertNotIn(b"Private demo description", response.data)

    def test_new_access_code_requires_exactly_eight_ascii_digits(self):
        with patch("app.secrets.randbelow", return_value=123):
            complaint_id, _ = self.submit_and_get_credentials()
        for invalid_code in ("1234567", "123456789", "1234abcd", "１２３４５６７８"):
            with self.subTest(access_code=invalid_code):
                response = self.client.post(
                    "/student",
                    data={"complaint_id": complaint_id, "access_code": invalid_code,
                          "csrf_token": self.csrf_token("/student")},
                )
                self.assertEqual(response.status_code, 404)
                self.assertIn(b"access code was not recognized", response.data)

    def test_previously_issued_long_access_codes_remain_valid(self):
        complaint_id, _ = self.submit_and_get_credentials(description="Legacy code demo issue")
        old_access_code = secrets.token_urlsafe(32)
        old_hash = hashlib.sha256(old_access_code.encode("utf-8")).hexdigest()
        with app.app_context():
            from app import get_db
            get_db().execute(
                "UPDATE complaints SET access_code_hash = ? WHERE complaint_id = ?",
                (old_hash, complaint_id),
            )
            get_db().commit()

        response = self.client.post(
            "/student",
            data={"complaint_id": complaint_id, "access_code": old_access_code,
                  "csrf_token": self.csrf_token("/student")},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(complaint_id.encode(), response.data)
        self.assertIn(b"Legacy code demo issue", response.data)

    def test_tracking_attempts_are_throttled_per_ip_and_complaint(self):
        with patch("app.secrets.randbelow", return_value=123):
            complaint_id, valid_code = self.submit_and_get_credentials()
        remote_ip = "198.51.100.77"
        for _ in range(5):
            response = self.client.post(
                "/student",
                data={"complaint_id": complaint_id, "access_code": "00000456",
                      "csrf_token": self.csrf_token("/student")},
                environ_base={"REMOTE_ADDR": remote_ip},
            )
            self.assertEqual(response.status_code, 404)

        throttled = self.client.post(
            "/student",
            data={"complaint_id": complaint_id, "access_code": valid_code,
                  "csrf_token": self.csrf_token("/student")},
            environ_base={"REMOTE_ADDR": remote_ip},
        )
        self.assertEqual(throttled.status_code, 429)
        self.assertEqual(throttled.headers["Cache-Control"], "no-store")
        self.assertIn(b"wait 15 minutes", throttled.data)

        another_ip = self.client.post(
            "/student",
            data={"complaint_id": complaint_id, "access_code": valid_code,
                  "csrf_token": self.csrf_token("/student")},
            environ_base={"REMOTE_ADDR": "198.51.100.78"},
        )
        self.assertEqual(another_ip.status_code, 200)

    def test_access_code_cannot_be_used_for_another_complaint(self):
        first_id, first_code = self.submit_and_get_credentials(description="First private issue")
        second_id, _ = self.submit_and_get_credentials(description="Second private issue")
        response = self.client.post(
            "/student",
            data={"complaint_id": second_id, "access_code": first_code,
                  "csrf_token": self.csrf_token("/student")},
        )
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(b"Second private issue", response.data)
        self.assertNotEqual(first_id, second_id)

    def test_roll_number_lookup_no_longer_exposes_complaints(self):
        complaint_id, _ = self.submit_and_get_credentials(description="Not available by roll number")
        get_response = self.client.get("/student?roll_number=DEMO-001")
        post_response = self.client.post(
            "/student", data={"roll_number": "DEMO-001", "csrf_token": self.csrf_token("/student")}
        )
        self.assertEqual(get_response.status_code, 200)
        self.assertNotIn(complaint_id.encode(), get_response.data)
        self.assertEqual(post_response.status_code, 404)
        self.assertNotIn(b"Not available by roll number", post_response.data)

    def test_public_success_url_does_not_retrieve_complaint(self):
        complaint_id, _ = self.submit_and_get_credentials(description="Private success detail")
        response = self.client.get(f"/success/{complaint_id}")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(b"Private success detail", response.data)

    def test_legacy_rows_survive_additive_migration_without_invented_codes(self):
        legacy_database = Path(self.temp_dir.name) / "legacy-complaints.db"
        with closing(sqlite3.connect(legacy_database)) as db:
            with db:
                db.execute(
                    """CREATE TABLE complaints (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        complaint_id TEXT NOT NULL UNIQUE,
                        name TEXT NOT NULL,
                        location TEXT NOT NULL,
                        category TEXT NOT NULL,
                        description TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'Pending',
                        created_at TEXT NOT NULL
                    )"""
                )
                db.execute(
                    """INSERT INTO complaints
                       (complaint_id, name, location, category, description, status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    ("CMP-LEGACY-000001", "Synthetic Legacy", "M Block", "Other",
                     "Legacy synthetic complaint", "In Progress", "2025-01-01T10:00:00"),
                )

        try:
            app.config["DATABASE"] = legacy_database
            upgrade_database(sqlite_url(legacy_database))
            with closing(sqlite3.connect(legacy_database)) as db:
                columns = {row[1] for row in db.execute("PRAGMA table_info(complaints)")}
                legacy_row = db.execute(
                    "SELECT complaint_id, name, description, status, access_code_hash FROM complaints"
                ).fetchone()
            self.assertIn("access_code_hash", columns)
            self.assertEqual(legacy_row, (
                "CMP-LEGACY-000001", "Synthetic Legacy", "Legacy synthetic complaint", "In Progress", None,
            ))

            response = self.client.post(
                "/student", data={"complaint_id": "CMP-LEGACY-000001", "access_code": "anything",
                                  "csrf_token": self.csrf_token("/student")},
            )
            self.assertEqual(response.status_code, 404)
            self.assertNotIn(b"Legacy synthetic complaint", response.data)
        finally:
            app.config["DATABASE"] = Path(self.temp_dir.name) / "test-complaints.db"

    def test_dashboard_and_analytics_render_with_isolated_database(self):
        self.assertEqual(self.client.get("/dashboard").status_code, 200)
        self.assertEqual(self.client.get("/admin").status_code, 200)
        self.assertEqual(self.client.get("/student").status_code, 200)

    def test_staff_routes_require_login(self):
        anonymous = app.test_client()
        for path in ("/dashboard", "/admin", "/dashboard/complaint/unknown"):
            with self.subTest(path=path):
                response = anonymous.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/staff/login", response.headers["Location"])
        with anonymous.session_transaction() as cookie_session:
            cookie_session["is_staff"] = True
        self.assertEqual(anonymous.get("/dashboard").status_code, 302)

    def test_department_staff_only_sees_assigned_category_and_cannot_escalate(self):
        electrical = self.create_complaint(
            name="Electrical Synthetic", category="Electrical", description="Broken light",
        )
        plumbing = self.create_complaint(
            name="Plumbing Synthetic", category="Plumbing", description="Leaking tap",
        )
        self.assertEqual(electrical.status_code, 200)
        self.assertEqual(plumbing.status_code, 200)

        engine = engine_for_url(sqlite_url(app.config["DATABASE"]))
        try:
            from sqlalchemy import select
            from campuscare.models import Department
            with session_factory(engine)() as db:
                now = utc_now()
                department_id = db.scalar(select(Department.department_id).where(
                    Department.department_code == "ELECTRICAL"
                ))
                technician = User(
                    account_kind="staff", email="tech@iiitdwd.example",
                    normalized_email="tech@iiitdwd.example", display_name="Synthetic Technician",
                    password_hash=hash_password("synthetic-tech-password"), status="active",
                    email_verified_at=now, created_at=now, updated_at=now,
                )
                db.add(technician)
                db.flush()
                db.add(StaffRoleAssignment(
                    user_id=technician.user_id, role_key="TECHNICIAN", department_id=department_id,
                    assigned_by_user_id=self.admin_user_id, assigned_at=now,
                ))
                db.commit()
        finally:
            engine.dispose()

        tech_client = app.test_client()
        login_token = re.search(
            rb'name="csrf_token" value="([^"]+)"', tech_client.get("/staff/login").data,
        ).group(1).decode("ascii")
        login = tech_client.post("/staff/login", data={
            "email": "tech@iiitdwd.example", "password": "synthetic-tech-password",
            "csrf_token": login_token,
        })
        self.assertEqual(login.status_code, 302)
        dashboard_response = tech_client.get("/dashboard")
        self.assertIn(b"Electrical Synthetic", dashboard_response.data)
        self.assertNotIn(b"Plumbing Synthetic", dashboard_response.data)

        with app.app_context():
            from app import get_db
            plumbing_id = get_db().execute(
                "SELECT complaint_id FROM complaints WHERE name = ?", ("Plumbing Synthetic",)
            ).fetchone()[0]
            electrical_id = get_db().execute(
                "SELECT complaint_id FROM complaints WHERE name = ?", ("Electrical Synthetic",)
            ).fetchone()[0]
        denied_history = tech_client.get(f"/dashboard/complaint/{plumbing_id}")
        self.assertEqual(denied_history.status_code, 404)
        self.assertNotIn(b"Complaint activity", denied_history.data)
        detail_path = f"/dashboard/complaint/{electrical_id}"
        detail_page = tech_client.get(detail_path)
        token = re.search(rb'name="csrf_token" value="([^"]+)"', detail_page.data).group(1).decode()
        denied = tech_client.post(detail_path, data={
            "status": "In Progress", "staff_remarks": "Not permitted", "csrf_token": token,
        })
        self.assertIn(b"do not have permission", denied.data)
        with app.app_context():
            from app import get_db
            status = get_db().execute(
                "SELECT status FROM complaints WHERE complaint_id = ?", (electrical_id,)
            ).fetchone()[0]
        self.assertEqual(status, "Pending")

    def test_staff_login_checks_password_and_logout_clears_session(self):
        client = app.test_client()
        page = client.get("/staff/login")
        token = re.search(rb'name="csrf_token" value="([^"]+)"', page.data).group(1).decode("ascii")
        denied = client.post("/staff/login", data={
            "email": "admin@iiitdwd.example", "password": "wrong", "csrf_token": token,
        })
        self.assertEqual(denied.status_code, 200)
        self.assertIn(b"Email or password was not accepted", denied.data)
        accepted = self.sign_in(client)
        self.assertEqual(accepted.status_code, 302)
        self.assertEqual(client.get("/admin").status_code, 200)
        logout_page = client.get("/admin")
        logout_token = re.search(rb'name="csrf_token" value="([^"]+)"', logout_page.data).group(1).decode("ascii")
        self.assertEqual(client.post("/staff/logout", data={"csrf_token": logout_token}).status_code, 302)
        self.assertEqual(client.get("/admin").status_code, 302)

    def test_post_without_csrf_token_is_rejected(self):
        response = self.client.post("/complaint", data={})
        self.assertEqual(response.status_code, 400)

    def test_qr_gallery_and_svg_endpoint_render(self):
        gallery = self.client.get("/qr-codes")
        svg = self.client.get("/qr-code/M%20Block.svg?download=1")
        self.assertEqual(gallery.status_code, 200)
        self.assertIn(b"M Block", gallery.data)
        self.assertEqual(svg.status_code, 200)
        self.assertEqual(svg.mimetype, "image/svg+xml")
        self.assertIn("attachment;", svg.headers["Content-Disposition"])

    def test_qr_to_resolution_workflow_with_synthetic_data(self):
        from html import unescape
        from urllib.parse import parse_qs, quote, urlsplit
        from app import QR_LOCATIONS

        gallery = self.client.get("/qr-codes")
        targets = re.findall(rb"<code>(https?://[^<]+)</code>", gallery.data)
        self.assertEqual(len(targets), len(QR_LOCATIONS))
        self.assertEqual(self.client.get("/qr-codes").status_code, 200)

        target_paths = []
        for target in targets:
            parsed = urlsplit(unescape(target.decode("utf-8")))
            path = parsed.path + ("?" + parsed.query if parsed.query else "")
            location = parse_qs(parsed.query)["location"][0]
            svg = self.client.get(f"/qr-code/{quote(location, safe='')}.svg")
            self.assertEqual(svg.status_code, 200)
            self.assertEqual(svg.mimetype, "image/svg+xml")
            form_page = self.client.get(path)
            self.assertEqual(form_page.status_code, 200)
            location_match = re.search(rb'id="location"[^>]*value="([^"]+)"[^>]*readonly', form_page.data)
            self.assertIsNotNone(location_match)
            target_paths.append(path)

        qr_path = next(path for path in target_paths if "Girls+Hostel" in path)
        token = self.csrf_token(qr_path)
        submitted = self.client.post(qr_path, data={
            "name": "Demo Student", "roll_number": "DEMO-QR-001",
            "location": "Forged location", "description": "Exposed wire near the projector",
            "csrf_token": token,
        })
        self.assertEqual(submitted.status_code, 200)
        self.assertIn(b"Girls Hostel", submitted.data)
        self.assertIn(b"Electrical", submitted.data)
        self.assertIn(b"High", submitted.data)
        complaint_id = re.search(rb"YOUR COMPLAINT ID</span>\s*<strong>([^<]+)", submitted.data).group(1).decode()
        access_code = re.search(rb'id="access-code">([^<]+)', submitted.data).group(1).decode()

        student_client = app.test_client()
        tracking_page = student_client.get("/student")
        tracking_token = re.search(
            rb'name="csrf_token" value="([^"]+)"', tracking_page.data
        ).group(1).decode()
        tracking = student_client.post("/student", data={
            "complaint_id": complaint_id, "access_code": access_code,
            "csrf_token": tracking_token,
        })
        self.assertEqual(tracking.status_code, 200)
        self.assertIn(b"Pending", tracking.data)

        staff_client = app.test_client()
        self.assertEqual(self.sign_in(staff_client).status_code, 302)
        detail_path = f"/dashboard/complaint/{complaint_id}"
        detail_page = staff_client.get(detail_path)
        update_token = re.search(rb'name="csrf_token" value="([^"]+)"', detail_page.data).group(1).decode()
        updated = staff_client.post(detail_path, data={
            "status": "Resolved", "staff_remarks": "Synthetic repair complete.",
            "csrf_token": update_token,
        })
        self.assertEqual(updated.status_code, 302)

        refreshed_tracking = student_client.post("/student", data={
            "complaint_id": complaint_id, "access_code": access_code,
            "csrf_token": tracking_token,
        })
        self.assertIn(b"Resolved", refreshed_tracking.data)
        self.assertIn(b"Synthetic repair complete.", refreshed_tracking.data)
        self.assertIn(b"Complaint submitted", refreshed_tracking.data)
        self.assertIn(b"Complaint resolved", refreshed_tracking.data)
        self.assertIn(b"Complaint activity", staff_client.get(detail_path).data)
        self.assertIn(b"Last updated", refreshed_tracking.data)
        analytics = staff_client.get("/admin")
        self.assertIn(b"High Priority", analytics.data)
        self.assertIn(b"Girls Hostel", analytics.data)
        self.assertRegex(analytics.data, rb"Total Complaints</span><strong>1</strong>")
        self.assertRegex(analytics.data, rb"Pending</span><strong>0</strong>")
        self.assertRegex(analytics.data, rb"Resolved</span><strong>1</strong>")
        self.assertRegex(analytics.data, rb"High Priority</span><strong>1</strong>")
        with app.app_context():
            from app import get_db
            saved = get_db().execute(
                "SELECT location, category, priority, status, staff_remarks FROM complaints WHERE complaint_id = ?",
                (complaint_id,),
            ).fetchone()
        self.assertEqual(tuple(saved), ("Girls Hostel", "Electrical", "High", "Resolved", "Synthetic repair complete."))

    def test_home_submit_track_staff_progress_and_resolution_workflow(self):
        from html import unescape
        from urllib.parse import parse_qs, urlsplit

        self.assertEqual(self.client.get("/").status_code, 200)
        form_page = self.client.get("/complaint")
        self.assertEqual(form_page.status_code, 200)
        submitted = self.client.post("/complaint", data={
            "csrf_token": self.csrf_token("/complaint"),
            "name": "Workflow Synthetic Student",
            "roll_number": "TEST-WORKFLOW-01",
            "location": "Synthetic Lab",
            "category": "Electrical",
            "description": "Synthetic loose outlet cover",
        })
        self.assertEqual(submitted.status_code, 200)
        complaint_id = re.search(
            rb"YOUR COMPLAINT ID</span>\s*<strong>([^<]+)", submitted.data,
        ).group(1).decode("ascii")
        link_match = re.search(rb'data-copy-value="([^"]+)"', submitted.data)
        self.assertIsNotNone(link_match)
        tracking_link = unescape(link_match.group(1).decode("utf-8"))
        parsed_link = urlsplit(tracking_link)
        self.assertEqual(parsed_link.path, "/student")
        self.assertEqual(parsed_link.query, "")
        credentials = parse_qs(parsed_link.fragment)
        self.assertEqual(credentials["complaint_id"], [complaint_id])
        access_code = credentials["access_code"][0]

        student_client = app.test_client()
        self.assertEqual(student_client.get("/").status_code, 200)
        tracking_token = re.search(
            rb'name="csrf_token" value="([^"]+)"', student_client.get("/student").data,
        ).group(1).decode("ascii")
        tracked = student_client.post("/student", data={
            "complaint_id": complaint_id, "access_code": access_code,
            "csrf_token": tracking_token,
        })
        self.assertEqual(tracked.status_code, 200)
        self.assertIn(b"Pending", tracked.data)

        staff_client = app.test_client()
        self.assertEqual(self.sign_in(staff_client).status_code, 302)
        dashboard = staff_client.get(f"/dashboard?q={complaint_id}")
        self.assertIn(complaint_id.encode("ascii"), dashboard.data)
        self.assertNotIn(access_code.encode("ascii"), dashboard.data)
        detail_path = f"/dashboard/complaint/{complaint_id}"
        detail = staff_client.get(detail_path)
        token = re.search(rb'name="csrf_token" value="([^"]+)"', detail.data).group(1).decode()
        in_progress = staff_client.post(detail_path, data={
            "status": "In Progress", "staff_remarks": "Synthetic work has started.",
            "csrf_token": token,
        })
        self.assertEqual(in_progress.status_code, 302)
        tracked = student_client.post("/student", data={
            "complaint_id": complaint_id, "access_code": access_code,
            "csrf_token": tracking_token,
        })
        self.assertIn(b"In Progress", tracked.data)
        self.assertIn(b"Synthetic work has started.", tracked.data)
        self.assertIn(b"Complaint submitted", tracked.data)
        self.assertIn(b"Status changed", tracked.data)
        self.assertIn(b"Staff update", tracked.data)

        detail = staff_client.get(detail_path)
        token = re.search(rb'name="csrf_token" value="([^"]+)"', detail.data).group(1).decode()
        resolved = staff_client.post(detail_path, data={
            "status": "Resolved", "staff_remarks": "Synthetic repair verified complete.",
            "csrf_token": token,
        })
        self.assertEqual(resolved.status_code, 302)
        staff_history = staff_client.get(detail_path)
        self.assertIn(b"Synthetic work has started.", staff_history.data)
        self.assertIn(b"Synthetic repair verified complete.", staff_history.data)
        tracked = student_client.post("/student", data={
            "complaint_id": complaint_id, "access_code": access_code,
            "csrf_token": tracking_token,
        })
        self.assertIn(b"Resolved", tracked.data)
        self.assertIn(b"Synthetic repair verified complete.", tracked.data)
        self.assertIn(b"Synthetic work has started.", tracked.data)
        self.assertIn(b"Complaint resolved", tracked.data)

    def test_revoked_staff_session_cannot_open_dashboard(self):
        with app.app_context():
            from app import get_identity_db
            self.assertTrue(revoke_sessions(get_identity_db(), self.admin_user_id))
        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/staff/login", response.headers["Location"])

    def test_valid_status_update_is_persisted(self):
        self.create_complaint()
        with app.app_context():
            from app import get_db
            complaint_id = get_db().execute("SELECT complaint_id FROM complaints").fetchone()[0]
        response = self.client.post(f"/dashboard/complaint/{complaint_id}",
                                    data={"status": "Resolved", "csrf_token": self.csrf_token(f"/dashboard/complaint/{complaint_id}")})
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            from app import get_db
            status = get_db().execute("SELECT status FROM complaints").fetchone()[0]
        self.assertEqual(status, "Resolved")

    def test_submission_creates_submitted_history_in_same_transaction(self):
        response = self.create_complaint()
        complaint_id = re.search(
            rb"YOUR COMPLAINT ID</span>\s*<strong>([^<]+)", response.data,
        ).group(1).decode()
        with app.app_context():
            from app import get_identity_db
            history = get_identity_db().query(ComplaintHistory).filter_by(
                complaint_id=complaint_id
            ).one()
            self.assertEqual(history.event_type, "submitted")
            self.assertEqual(history.new_status, "Pending")
            self.assertIsNotNone(history.created_at.tzinfo)
            self.assertIsNone(history.actor_user_id)

    def test_failed_submission_history_insert_rolls_back_complaint(self):
        with app.app_context():
            from app import get_db
            get_db().execute(
                """CREATE TRIGGER fail_submission_history BEFORE INSERT ON complaint_history
                   WHEN NEW.event_type = 'submitted'
                   BEGIN SELECT RAISE(ABORT, 'synthetic history failure'); END"""
            )
            get_db().commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.create_complaint()
        self.assertEqual(self.count_complaints(), 0)
        with app.app_context():
            from app import get_db
            self.assertEqual(
                get_db().execute("SELECT COUNT(*) FROM complaint_history").fetchone()[0], 0
            )

    def test_remark_without_status_change_creates_staff_update_event(self):
        self.create_complaint()
        with app.app_context():
            from app import get_db, get_identity_db
            from campuscare.complaints import update_complaint_status
            complaint_id = get_db().execute(
                "SELECT complaint_id FROM complaints"
            ).fetchone()[0]
            db = get_identity_db()
            update_complaint_status(
                db, db.get(User, self.admin_user_id), complaint_id,
                "Pending", "Synthetic scheduling note", "remark-only-key",
            )
            events = db.query(ComplaintHistory).filter_by(complaint_id=complaint_id).order_by(
                ComplaintHistory.id
            ).all()
            self.assertEqual([event.event_type for event in events], ["submitted", "staff_update"])
            self.assertIsNone(events[1].previous_status)
            self.assertIsNone(events[1].new_status)
            self.assertEqual(events[1].remarks, "Synthetic scheduling note")

    def test_history_is_retained_and_duplicate_update_key_is_idempotent(self):
        self.create_complaint()
        with app.app_context():
            from app import get_db, get_identity_db
            from campuscare.complaints import update_complaint_status
            complaint_id = get_db().execute(
                "SELECT complaint_id FROM complaints"
            ).fetchone()[0]
            db = get_identity_db()
            user = db.get(User, self.admin_user_id)
            update_complaint_status(
                db, user, complaint_id, "In Progress", "First synthetic update", "retry-key-001",
            )
            update_complaint_status(
                db, user, complaint_id, "In Progress", "First synthetic update", "retry-key-001",
            )
            events = db.query(ComplaintHistory).filter_by(complaint_id=complaint_id).order_by(
                ComplaintHistory.id
            ).all()
            self.assertEqual(
                [event.event_type for event in events],
                ["submitted", "status_changed", "staff_update"],
            )
            self.assertEqual(events[2].remarks, "First synthetic update")
            self.assertEqual(events[1].previous_status, "Pending")
            self.assertEqual(events[1].new_status, "In Progress")

    def test_failed_history_insert_rolls_back_complaint_update(self):
        self.create_complaint()
        with app.app_context():
            from app import get_db
            complaint_id = get_db().execute(
                "SELECT complaint_id FROM complaints"
            ).fetchone()[0]
            get_db().execute(
                """CREATE TRIGGER fail_history_insert BEFORE INSERT ON complaint_history
                   WHEN NEW.request_key = 'failed-request-key:status'
                   BEGIN SELECT RAISE(ABORT, 'synthetic history failure'); END"""
            )
            get_db().commit()

        with app.app_context():
            from app import get_identity_db
            from campuscare.complaints import update_complaint_status
            from sqlalchemy.exc import IntegrityError
            db = get_identity_db()
            user = db.get(User, self.admin_user_id)
            with self.assertRaises(IntegrityError):
                update_complaint_status(
                    db, user, complaint_id, "In Progress", "Must roll back", "failed-request-key",
                )
            db.rollback()
            saved = db.execute(
                __import__("sqlalchemy").text(
                    "SELECT status, staff_remarks FROM complaints WHERE complaint_id=:id"
                ), {"id": complaint_id},
            ).one()
            events = db.query(ComplaintHistory).filter_by(complaint_id=complaint_id).all()
            self.assertEqual(tuple(saved), ("Pending", None))
            self.assertEqual([event.event_type for event in events], ["submitted"])

    def test_student_tracking_rejects_invalid_credentials_before_history(self):
        response = self.create_complaint()
        complaint_id = re.search(
            rb"YOUR COMPLAINT ID</span>\s*<strong>([^<]+)", response.data,
        ).group(1).decode()
        student = app.test_client()
        page = student.get("/student")
        token = re.search(rb'name="csrf_token" value="([^"]+)"', page.data).group(1).decode()
        denied = student.post("/student", data={
            "complaint_id": complaint_id, "access_code": "00000000", "csrf_token": token,
        })
        self.assertEqual(denied.status_code, 404)
        self.assertNotIn(b"Complaint activity", denied.data)
        self.assertNotIn(b"Complaint submitted", denied.data)

    def test_legacy_remark_is_visible_without_fabricated_timestamp(self):
        response = self.create_complaint()
        complaint_id = re.search(
            rb"YOUR COMPLAINT ID</span>\s*<strong>([^<]+)", response.data,
        ).group(1).decode()
        access_code = re.search(rb'id="access-code">([^<]+)', response.data).group(1).decode()
        with app.app_context():
            from app import get_db
            get_db().execute("DELETE FROM complaint_history WHERE complaint_id = ?", (complaint_id,))
            get_db().execute(
                "UPDATE complaints SET staff_remarks = ? WHERE complaint_id = ?",
                ("Legacy synthetic note", complaint_id),
            )
            get_db().commit()
        student = app.test_client()
        token = re.search(
            rb'name="csrf_token" value="([^"]+)"', student.get("/student").data,
        ).group(1).decode()
        tracked = student.post("/student", data={
            "complaint_id": complaint_id, "access_code": access_code, "csrf_token": token,
        })
        self.assertIn(b"Legacy staff update", tracked.data)
        self.assertIn(b"Original update time unavailable", tracked.data)
        self.assertIn(b"Legacy synthetic note", tracked.data)


if __name__ == "__main__":
    unittest.main()
