from pathlib import Path

from sqlalchemy import URL, create_engine, event
from sqlalchemy.orm import sessionmaker


def sqlite_url(path):
    return URL.create("sqlite", database=str(Path(path).resolve()))


def engine_for_url(url, *, pool_size=2):
    is_sqlite = str(url).startswith("sqlite:")
    options = {"pool_pre_ping": True}
    if is_sqlite:
        options["connect_args"] = {"check_same_thread": False, "timeout": 15}
    else:
        options.update(pool_size=pool_size, max_overflow=0)
    engine = create_engine(url, **options)
    if is_sqlite:
        @event.listens_for(engine, "connect")
        def enable_sqlite_foreign_keys(connection, _record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return engine


def session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)

