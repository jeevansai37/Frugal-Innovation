from contextlib import closing
from pathlib import Path
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateIndex, CreateTable

from campuscare.database import engine_for_url, sqlite_url
from campuscare.migration_runner import upgrade_database
from campuscare.models import Base


class MigrationFoundationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "migration-fixture.db"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_fresh_migration_creates_complaints_and_identity_tables(self):
        upgrade_database(sqlite_url(self.database_path))
        engine = engine_for_url(sqlite_url(self.database_path))
        try:
            inspector = inspect(engine)
            self.assertTrue({
                "complaints", "users", "roles", "permissions", "role_permissions",
                "departments", "staff_role_assignments", "auth_tokens", "audit_logs",
                "login_attempt_buckets", "complaint_history", "alembic_version",
            }.issubset(set(inspector.get_table_names())))
            columns = {column["name"]: column for column in inspector.get_columns("complaints")}
            self.assertEqual(columns["id"]["type"].__class__.__name__, "INTEGER")
            self.assertTrue(columns["id"]["primary_key"])
            self.assertTrue({"last_updated_at", "staff_remarks"}.issubset(columns))
            with engine.connect() as connection:
                self.assertEqual(connection.exec_driver_sql("SELECT COUNT(*) FROM complaints").scalar_one(), 0)
                self.assertEqual(connection.exec_driver_sql("SELECT COUNT(*) FROM departments").scalar_one(), 6)
            history_fks = inspector.get_foreign_keys("complaint_history")
            self.assertEqual(
                {fk["referred_table"] for fk in history_fks}, {"complaints", "users"}
            )
            self.assertTrue(any(
                index["name"] == "ix_complaint_history_complaint_time"
                for index in inspector.get_indexes("complaint_history")
            ))
        finally:
            engine.dispose()

    def test_existing_complaint_record_id_and_access_hash_are_unchanged(self):
        synthetic_hash = "scrypt$16384$8$1$00112233445566778899aabbccddeeff$" + "a" * 64
        with closing(sqlite3.connect(self.database_path)) as db:
            db.execute(
                """CREATE TABLE complaints (
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
            db.execute(
                """INSERT INTO complaints
                   (id, complaint_id, name, roll_number, location, category, description,
                    status, priority, created_at, access_code_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (41, "CMP-SYNTHETIC-000041", "Synthetic Reporter", "TEST-000041",
                 "Synthetic Block", "Electrical", "Synthetic fixture only", "In Progress",
                 "High", "2026-01-02T03:04:05", synthetic_hash),
            )
            db.commit()
            before = db.execute("SELECT * FROM complaints WHERE id=41").fetchone()

        upgrade_database(sqlite_url(self.database_path))

        with closing(sqlite3.connect(self.database_path)) as db:
            after = db.execute("SELECT * FROM complaints WHERE id=41").fetchone()
            self.assertEqual(after[:len(before)], before)
            self.assertEqual(after[0], 41)
            self.assertEqual(after[10], synthetic_hash)
            self.assertIsNone(db.execute(
                "SELECT staff_remarks FROM complaints WHERE id=41"
            ).fetchone()[0])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM complaint_history").fetchone()[0], 0)

    def test_history_migration_preserves_legacy_remark_without_backfill(self):
        with closing(sqlite3.connect(self.database_path)) as db:
            db.execute(
                """CREATE TABLE complaints (
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
                    access_code_hash TEXT,
                    last_updated_at TEXT,
                    staff_remarks TEXT
                )"""
            )
            db.execute("CREATE TABLE users (user_id VARCHAR(36) PRIMARY KEY)")
            db.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
            db.execute(
                "INSERT INTO alembic_version(version_num) VALUES ('0002_complaint_staff_updates')"
            )
            db.execute(
                """INSERT INTO complaints
                   (id, complaint_id, name, location, category, description, status,
                    created_at, access_code_hash, last_updated_at, staff_remarks)
                   VALUES (73, 'CMP-SYNTHETIC-000073', 'Synthetic Legacy', 'Test Area',
                    'Other', 'Synthetic legacy record', 'In Progress',
                    '2025-02-03T04:05:06', 'synthetic-hash', '2025-02-04T04:05:06',
                    'Synthetic old remark')"""
            )
            db.commit()
            before = db.execute("SELECT * FROM complaints WHERE id=73").fetchone()

        upgrade_database(sqlite_url(self.database_path))

        with closing(sqlite3.connect(self.database_path)) as db:
            after = db.execute("SELECT * FROM complaints WHERE id=73").fetchone()
            self.assertEqual(after, before)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM complaint_history").fetchone()[0], 0)

    def test_older_complaint_schema_gets_only_additive_columns(self):
        with closing(sqlite3.connect(self.database_path)) as db:
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
                   (id, complaint_id, name, location, category, description, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (17, "CMP-SYNTHETIC-000017", "Synthetic Older", "Synthetic Area", "Other",
                 "Synthetic legacy fixture", "Resolved", "2025-03-04T05:06:07"),
            )
            db.commit()
            before = db.execute(
                "SELECT id, complaint_id, name, location, category, description, status, created_at FROM complaints"
            ).fetchone()

        upgrade_database(sqlite_url(self.database_path))

        with closing(sqlite3.connect(self.database_path)) as db:
            after = db.execute(
                "SELECT id, complaint_id, name, location, category, description, status, created_at FROM complaints"
            ).fetchone()
            self.assertEqual(after, before)
            columns = {row[1] for row in db.execute("PRAGMA table_info(complaints)")}
            self.assertTrue({"roll_number", "priority", "access_code_hash"}.issubset(columns))
            self.assertEqual(db.execute("SELECT roll_number, priority, access_code_hash FROM complaints").fetchone(),
                             ("", "Medium", None))

    def test_application_import_does_not_create_a_database_or_run_ddl(self):
        probe_dir = Path(self.temp_dir.name) / "import-probe"
        probe_dir.mkdir()
        repo_root = Path(__file__).resolve().parents[1]
        shutil.copy2(repo_root / "app.py", probe_dir / "app.py")
        shutil.copytree(repo_root / "campuscare", probe_dir / "campuscare")
        env = os.environ.copy()
        env.pop("VERCEL", None)
        result = subprocess.run(
            [sys.executable, "-c", "import app"],
            cwd=probe_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((probe_dir / "complaints.db").exists())

    def test_models_compile_for_sqlite_and_postgresql_dialects(self):
        for dialect in (sqlite.dialect(), postgresql.dialect()):
            for table in Base.metadata.sorted_tables:
                with self.subTest(dialect=dialect.name, table=table.name):
                    ddl = str(CreateTable(table).compile(dialect=dialect))
                    self.assertIn(table.name, ddl)
            for table in Base.metadata.sorted_tables:
                for index in table.indexes:
                    with self.subTest(dialect=dialect.name, index=index.name):
                        self.assertIn(index.name, str(CreateIndex(index).compile(dialect=dialect)))

    def test_postgresql_migrations_require_explicit_opt_in(self):
        with self.assertRaisesRegex(RuntimeError, "explicit allow_postgres=True"):
            upgrade_database("postgresql://example.invalid/campuscare-test")

    def test_direct_alembic_cli_cannot_bypass_postgresql_opt_in(self):
        repo_root = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env["DATABASE_URL"] = "postgresql://example.invalid/campuscare-test"
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", str(repo_root / "alembic.ini"), "upgrade", "head"],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PostgreSQL migrations require an explicit allow_postgres=True", result.stderr)

if __name__ == "__main__":
    unittest.main()
