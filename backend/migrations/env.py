from logging.config import fileConfig

import app.db.models  # noqa: F401  (registers all tables on Base.metadata)
from alembic import context
from app.config import get_settings
from app.db.base import Base
from sqlalchemy import create_engine, pool

SCHEMAS = {"raw", "core", "app", "eval"}

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def database_url() -> str:
    """Explicit `sqlalchemy.url` (set by tests) wins over DATABASE_URL."""
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def include_name(name: str | None, type_: str, parent_names: dict) -> bool:
    if type_ == "schema":
        return name in SCHEMAS
    return True


def configure(**kwargs: object) -> None:
    context.configure(
        target_metadata=target_metadata,
        include_schemas=True,
        include_name=include_name,
        compare_type=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    configure(url=database_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
