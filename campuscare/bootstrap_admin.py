"""Operator-only initial administrator bootstrap; no public registration route."""
import getpass

from app import app

from .database import engine_for_url, session_factory
from .identity import bootstrap_first_super_admin, configured_institutional_email_domains
from .migration_runner import configured_database_url


def main():
    domains = configured_institutional_email_domains()
    if not domains:
        raise SystemExit("Set INSTITUTIONAL_EMAIL_DOMAINS before bootstrapping the first administrator.")
    email = input("Verified institutional email: ").strip()
    password = getpass.getpass("New password (12-128 characters): ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("Passwords did not match.")

    engine = engine_for_url(configured_database_url(app.config))
    try:
        with session_factory(engine)() as db:
            bootstrap_first_super_admin(
                db,
                email=email,
                password=password,
                allowed_domains=domains,
            )
    finally:
        engine.dispose()
    print("Initial Super Admin created. Keep credentials private and remove bootstrap access to this environment.")


if __name__ == "__main__":
    main()
