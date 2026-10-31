"""Explicit local/hosted schema migration entry point: python -m campuscare.migrate"""
from app import app

from .migration_runner import configured_database_url, upgrade_database


def main():
    upgrade_database(configured_database_url(app.config))
    print("Database migrations are up to date.")


if __name__ == "__main__":
    main()
