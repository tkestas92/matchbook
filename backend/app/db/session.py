from functools import lru_cache

from app.config import get_settings
from sqlalchemy import Engine, create_engine


@lru_cache
def get_engine() -> Engine:
    return create_engine(get_settings().database_url)
