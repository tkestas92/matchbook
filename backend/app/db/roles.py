"""Create or update the least-privilege login roles (`make roles`).

Roles are cluster-wide and their passwords are per-environment secrets, so they live
here, not in Alembic. Grants, views and RLS are database objects and live in migrations.
"""

import sys

from app.config import Settings, get_settings
from sqlalchemy import Connection, create_engine, text

LOGIN_ROLES = ("etl", "app", "agent_ro", "eval_runner", "demo_ro")

ROLE_ATTRIBUTES = "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT"


class MissingRolePasswordError(RuntimeError):
    pass


def role_passwords(settings: Settings) -> dict[str, str]:
    """Map each login role to its password from settings; all must be set."""
    passwords = {role: getattr(settings, f"{role}_db_password") for role in LOGIN_ROLES}
    missing = [role for role, secret in passwords.items() if secret is None]
    if missing:
        names = ", ".join(f"{role.upper()}_DB_PASSWORD" for role in missing)
        raise MissingRolePasswordError(f"Set {names} in .env (see .env.example)")
    return {role: secret.get_secret_value() for role, secret in passwords.items()}


def ensure_role(conn: Connection, role: str, password: str) -> None:
    """Idempotent: create the role if missing, then (re)set attributes and password."""
    exists = conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).scalar()
    if not exists:
        conn.execute(text(f'CREATE ROLE "{role}"'))
    # Quote server-side with %I / %L so the password is never concatenated into SQL.
    statement = conn.execute(
        text(
            f"SELECT format('ALTER ROLE %I WITH {ROLE_ATTRIBUTES} PASSWORD %L', CAST(:r AS text), CAST(:p AS text))"
        ),
        {"r": role, "p": password},
    ).scalar_one()
    conn.execute(text(statement))


def ensure_roles(conn: Connection, passwords: dict[str, str]) -> None:
    for role in LOGIN_ROLES:
        ensure_role(conn, role, passwords[role])


def main() -> int:
    settings = get_settings()
    try:
        passwords = role_passwords(settings)
    except MissingRolePasswordError as exc:
        print(exc, file=sys.stderr)
        return 1
    engine = create_engine(settings.database_url)
    try:
        with engine.begin() as conn:
            ensure_roles(conn, passwords)
    finally:
        engine.dispose()
    print(f"Roles ready: {', '.join(LOGIN_ROLES)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
