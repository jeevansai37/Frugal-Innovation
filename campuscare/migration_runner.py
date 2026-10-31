from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from .database import engine_for_url, sqlite_url


ROOT = Path(__file__).resolve().parent.parent


def configured_database_url(app_config):
    if app_config.get("DATABASE_URL"):
        return app_config["DATABASE_URL"]
    return sqlite_url(app_config["DATABASE"])


def upgrade_database(url, *, allow_postgres=False):
    """Run Alembic explicitly on a caller-selected database URL."""
    if make_url(url).get_backend_name() == "postgresql" and not allow_postgres:
        raise RuntimeError(
            "PostgreSQL migrations require an explicit allow_postgres=True after an isolated rehearsal."
        )
    engine = engine_for_url(url)
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["allow_postgres"] = allow_postgres
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    finally:
        engine.dispose()

