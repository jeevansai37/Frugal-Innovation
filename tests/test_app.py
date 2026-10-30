import re
from contextlib import closing
import hashlib
import secrets
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import app, hash_access_code, init_db, verify_access_code


class AppFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database = app.config["DATABASE"]
        self.original_admin_password = app.config["ADMIN_PASSWORD"]
        app.config["DATABASE"] = Path(self.temp_dir.name) / "test-complaints.db"
        app.config["ADMIN_PASSWORD"] = "test-staff-password"
        app.config["TESTING"] = True
        init_db()
        self.client = app.test_client()
        self.sign_in()

    def tearDown(self):
        app.config["DATABASE"] = self.original_database
        app.config["ADMIN_PASSWORD"] = self.original_admin_password
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
        return client.post("/staff/login", data={"password": "test-staff-password", "csrf_token": token})

    def test_valid_submission_is_saved_and_shows_credentials_once(self):
        with patch("app.secrets.randbelow", return_value=123):
            response = self.create_complaint()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
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
        self.assertIn(b"Choose a valid status", response.data)
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
            init_db()
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

    def test_staff_login_checks_password_and_logout_clears_session(self):
        client = app.test_client()
        page = client.get("/staff/login")
        token = re.search(rb'name="csrf_token" value="([^"]+)"', page.data).group(1).decode("ascii")
        denied = client.post("/staff/login", data={"password": "wrong", "csrf_token": token})
        self.assertEqual(denied.status_code, 200)
        self.assertIn(b"password was not accepted", denied.data)
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
            "status": "Resolved", "csrf_token": update_token,
        })
        self.assertEqual(updated.status_code, 302)

        refreshed_tracking = student_client.post("/student", data={
            "complaint_id": complaint_id, "access_code": access_code,
            "csrf_token": tracking_token,
        })
        self.assertIn(b"Resolved", refreshed_tracking.data)
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
                "SELECT location, category, priority, status FROM complaints WHERE complaint_id = ?",
                (complaint_id,),
            ).fetchone()
        self.assertEqual(tuple(saved), ("Girls Hostel", "Electrical", "High", "Resolved"))

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


if __name__ == "__main__":
    unittest.main()
