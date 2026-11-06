"""Shared pytest fixtures."""

import pytest
from app.config import get_settings
from pydantic import ValidationError


@pytest.fixture(scope="session")
def database_url() -> str:
    """DATABASE_URL from the environment (Settings / .env)."""
    try:
        return get_settings().database_url
    except ValidationError:
        pytest.skip("DATABASE_URL is not set")
