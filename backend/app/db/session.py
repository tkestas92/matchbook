from functools import lru_cache

from app.config import Settings, get_settings
from app.db.roles import MissingRolePasswordError
from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL, make_url


@lru_cache
def get_engine() -> Engine:
    return create_engine(get_settings().database_url)


def etl_database_url(settings: Settings) -> URL:
    """`DATABASE_URL` logged in as the least-privilege `etl` role instead of the owner."""
    if settings.etl_db_password is None:
        raise MissingRolePasswordError("Set ETL_DB_PASSWORD in .env (see .env.example)")
    password = settings.etl_db_password.get_secret_value()
    return make_url(settings.database_url).set(username="etl", password=password)


def create_etl_engine() -> Engine:
    return create_engine(etl_database_url(get_settings()))
