"""The demo's database login: `demo_ro`, read-only, with a short statement timeout."""

from app.config import Settings, get_settings
from app.db.roles import MissingRolePasswordError
from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL, make_url

STATEMENT_TIMEOUT_MS = 3000
POOL_SIZE = 5
POOL_TIMEOUT_S = 5

# Defence in depth: the role has the same defaults in the database (migration 0005).
SESSION_OPTIONS = f"-c default_transaction_read_only=on -c statement_timeout={STATEMENT_TIMEOUT_MS}"


def demo_database_url(settings: Settings) -> URL:
    """`DATABASE_URL` logged in as the read-only `demo_ro` role instead of the owner."""
    if settings.demo_ro_db_password is None:
        raise MissingRolePasswordError("Set DEMO_RO_DB_PASSWORD in .env (see .env.example)")
    password = settings.demo_ro_db_password.get_secret_value()
    return make_url(settings.database_url).set(username="demo_ro", password=password)


def create_demo_engine(url: URL | None = None) -> Engine:
    return create_engine(
        url or demo_database_url(get_settings()),
        connect_args={"options": SESSION_OPTIONS},
        pool_size=POOL_SIZE,
        max_overflow=0,
        pool_timeout=POOL_TIMEOUT_S,
        pool_pre_ping=True,
    )
