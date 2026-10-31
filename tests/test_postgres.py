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
        from app import app
        from campuscare.migration_runner import upgrade_database
        from campuscare.database import engine_for_url, session_factory
        from campuscare.identity import hash_password
        from campuscare.models import StaffRoleAssignment, User, utc_now

        cls.app = app
        cls.original_database_url = app.config["DATABASE_URL"]
        cls.original_rate_limit_secret = app.config["LOGIN_RATE_LIMIT_SECRET"]
        cls.schema = "test_" + secrets.token_hex(6)
        with psycopg.connect(POSTGRES_TEST_URL, autocommit=True) as connection:
            connection.execute(f'CREATE SCHEMA "{cls.schema}"')

        parsed = urlsplit(POSTGRES_TEST_URL)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["options"] = f"-csearch_path={cls.schema}"
        app.config["DATABASE_URL"] = urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
        )
        app.config["LOGIN_RATE_LIMIT_SECRET"] = "synthetic-postgres-rate-limit-secret"
        app.config["TESTING"] = True
        upgrade_database(app.config["DATABASE_URL"], allow_postgres=True)
        engine = engine_for_url(app.config["DATABASE_URL"])
        try:
            with session_factory(engine)() as db:
                now = utc_now()
                admin = User(
                    account_kind="staff", email="admin@iiitdwd.example",
                    normalized_email="admin@iiitdwd.example", display_name="Synthetic Admin",
                    password_hash=hash_password("synthetic-postgres-admin-password"), status="active",
                    email_verified_at=now, created_at=now, updated_at=now,
                )
                db.add(admin)
                db.flush()
                db.add(StaffRoleAssignment(
                    user_id=admin.user_id, role_key="SUPER_ADMIN", assigned_by_user_id=admin.user_id,
                    department_id=None, assigned_at=now,
                ))
                db.commit()
        finally:
            engine.dispose()
        cls.client = app.test_client()

    def database_url_for_schema(self, schema):
        parsed = urlsplit(POSTGRES_TEST_URL)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["options"] = f"-csearch_path={schema}"
        return urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
        )

    @classmethod
    def tearDownClass(cls):
        from app import _postgres_connection_pool
        import psycopg

        if _postgres_connection_pool is not None:
            _postgres_connection_pool.close()
        with psycopg.connect(POSTGRES_TEST_URL, autocommit=True) as connection:
            connection.execute(f'DROP SCHEMA "{cls.schema}" CASCADE')
        cls.app.config["DATABASE_URL"] = cls.original_database_url
        cls.app.config["LOGIN_RATE_LIMIT_SECRET"] = cls.original_rate_limit_secret
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
                  "email": "admin@iiitdwd.example",
                  "password": "synthetic-postgres-admin-password"},
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

    def test_migration_preserves_synthetic_legacy_complaint(self):
        import psycopg
        from campuscare.migration_runner import upgrade_database

        schema = "legacy_" + secrets.token_hex(6)
        with psycopg.connect(POSTGRES_TEST_URL, autocommit=True) as connection:
            connection.execute(f'CREATE SCHEMA "{schema}"')
        try:
            legacy_url = self.database_url_for_schema(schema)
            synthetic_hash = "a" * 64
            with psycopg.connect(legacy_url) as connection:
                connection.execute("""CREATE TABLE complaints (
                    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
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
                )""")
                connection.execute("""INSERT INTO complaints
                    (id, complaint_id, name, roll_number, location, category, description,
                     status, priority, created_at, access_code_hash)
                    VALUES (77, 'CMP-SYNTHETIC-PG-000077', 'Synthetic Reporter', 'TEST-77',
                            'Synthetic Block', 'Electrical', 'Synthetic issue', 'In Progress',
                            'High', '2026-01-02T03:04:05', %s)""", (synthetic_hash,))
                before = connection.execute(
                    "SELECT id, complaint_id, status, access_code_hash FROM complaints WHERE id=77"
                ).fetchone()

            upgrade_database(legacy_url, allow_postgres=True)

            with psycopg.connect(legacy_url) as connection:
                after = connection.execute(
                    "SELECT id, complaint_id, status, access_code_hash FROM complaints WHERE id=77"
                ).fetchone()
                columns = {row[0] for row in connection.execute("""SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = 'complaints'""", (schema,))}
            self.assertEqual(after, before)
            self.assertEqual(after, (77, "CMP-SYNTHETIC-PG-000077", "In Progress", synthetic_hash))
            self.assertTrue({"last_updated_at", "staff_remarks"}.issubset(columns))
        finally:
            with psycopg.connect(POSTGRES_TEST_URL, autocommit=True) as connection:
                connection.execute(f'DROP SCHEMA "{schema}" CASCADE')


if __name__ == "__main__":
    unittest.main()
