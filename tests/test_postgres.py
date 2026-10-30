"""Optional PostgreSQL smoke suite; requires a dedicated synthetic test database."""
import os
import re
import secrets
import unittest
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

POSTGRES_TEST_URL = os.environ.get("POSTGRES_TEST_DATABASE_URL", "").strip()


@unittest.skipUnless(POSTGRES_TEST_URL, "Set POSTGRES_TEST_DATABASE_URL to a dedicated PostgreSQL test database")
class PostgreSQLFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from app import app, init_db

        cls.app = app
        cls.original_database_url = app.config["DATABASE_URL"]
        cls.original_admin_password = app.config["ADMIN_PASSWORD"]
        cls.schema = "test_" + secrets.token_hex(6)
        with psycopg.connect(POSTGRES_TEST_URL, autocommit=True) as connection:
            connection.execute(f'CREATE SCHEMA "{cls.schema}"')

        parsed = urlsplit(POSTGRES_TEST_URL)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["options"] = f"-csearch_path={cls.schema}"
        app.config["DATABASE_URL"] = urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
        )
        app.config["ADMIN_PASSWORD"] = "synthetic-postgres-staff-password"
        app.config["TESTING"] = True
        init_db()
        cls.client = app.test_client()

    @classmethod
    def tearDownClass(cls):
        from app import _postgres_connection_pool
        import psycopg

        if _postgres_connection_pool is not None:
            _postgres_connection_pool.close()
        with psycopg.connect(POSTGRES_TEST_URL, autocommit=True) as connection:
            connection.execute(f'DROP SCHEMA "{cls.schema}" CASCADE')
        cls.app.config["DATABASE_URL"] = cls.original_database_url
        cls.app.config["ADMIN_PASSWORD"] = cls.original_admin_password
        cls.app.config.pop("TESTING", None)

    def csrf_token(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
        self.assertIsNotNone(match)
        return match.group(1).decode("ascii")

    def sign_in(self):
        response = self.client.post(
            "/staff/login",
            data={"csrf_token": self.csrf_token("/staff/login"),
                  "password": "synthetic-postgres-staff-password"},
        )
        self.assertEqual(response.status_code, 302)

    def test_complaint_tracking_staff_status_analytics_and_qr(self):
        from app import get_db

        qr_page = self.client.get("/qr-codes")
        self.assertEqual(qr_page.status_code, 200)
        self.assertEqual(self.client.get("/qr-code/M%20Block.svg").status_code, 200)

        response = self.client.post("/complaint?location=M%20Block", data={
            "csrf_token": self.csrf_token("/complaint"), "name": "Synthetic Student",
            "roll_number": "TEST-001", "location": "Tampered Location",
            "description": "Exposed wire near the projector",
        })
        self.assertEqual(response.status_code, 200)
        match_id = re.search(rb"CMP-[0-9]{8}-[A-F0-9]{6}", response.data)
        match_code = re.search(rb'id="access-code">([0-9]{8})', response.data)
        self.assertIsNotNone(match_id)
        self.assertIsNotNone(match_code)
        complaint_id = match_id.group().decode("ascii")
        access_code = match_code.group(1).decode("ascii")

        with self.app.test_request_context():
            complaint = get_db().execute(
                "SELECT location, category, priority FROM complaints WHERE complaint_id = ?",
                (complaint_id,),
            ).fetchone()
            self.assertEqual(complaint["location"], "M Block")
            self.assertEqual(complaint["category"], "Electrical")
            self.assertEqual(complaint["priority"], "High")

        tracked = self.client.post("/student", data={
            "csrf_token": self.csrf_token("/student"), "complaint_id": complaint_id,
            "access_code": access_code,
        })
        self.assertEqual(tracked.status_code, 200)

        self.sign_in()
        detail_path = f"/dashboard/complaint/{complaint_id}"
        updated = self.client.post(detail_path, data={
            "csrf_token": self.csrf_token(detail_path), "status": "Resolved",
        })
        self.assertEqual(updated.status_code, 302)
        self.assertEqual(self.client.get("/admin").status_code, 200)
        self.assertIn(b"Resolved", self.client.get("/admin").data)


if __name__ == "__main__":
    unittest.main()
