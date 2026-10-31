from logging.config import fileConfig
import os

from alembic import context
from sqlalchemy.engine import make_url
from campuscare.models import Base


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def require_postgres_opt_in(url):
    if (make_url(url).get_backend_name() == "postgresql"
            and not config.attributes.get("allow_postgres", False)):
        raise RuntimeError(
            "PostgreSQL migrations require an explicit allow_postgres=True after an isolated rehearsal."
        )


def include_object(object_, name, type_, reflected, compare_to):
    # The legacy complaints table remains managed by the Flask compatibility layer
    # until its dedicated UUID-link migration is approved.
    if type_ == "table" and name == "complaints":
        return False
    return True


def run_with_connection(connection):
    require_postgres_opt_in(connection.engine.url)
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_object=include_object,
        render_as_batch=connection.dialect.name == "sqlite",
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    supplied_connection = config.attributes.get("connection")
    if supplied_connection is not None:
        run_with_connection(supplied_connection)
        return

    from app import app
    url = app.config.get("DATABASE_URL") or config.get_main_option("sqlalchemy.url")
    if not app.config.get("DATABASE_URL"):
        from campuscare.database import sqlite_url
        url = sqlite_url(app.config["DATABASE"])
    require_postgres_opt_in(url)
    from campuscare.database import engine_for_url
    connectable = engine_for_url(url, pool_size=1)
    with connectable.connect() as connection:
        run_with_connection(connection)


if context.is_offline_mode():
    url = os.environ.get("DATABASE_URL") or config.get_main_option("sqlalchemy.url")
    require_postgres_opt_in(url)
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()
else:
    run_migrations_online()
