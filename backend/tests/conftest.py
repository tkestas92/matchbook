"""Shared pytest fixtures."""

from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from alembic import command
from app.config import Settings, get_settings
from app.db.roles import MissingRolePasswordError, ensure_roles, role_passwords
from app.db.session import app_database_url, etl_database_url
from app.llm import gemini
from pydantic import ValidationError
from sqlalchemy import Engine, create_engine, text
from tests.test_migrations import alembic_config, temporary_database

PIPELINE_TABLES = (
    "raw.meta_responses",
    "raw.google_responses",
    "raw.tiktok_responses",
    "app.issues",
    "core.daily_delivery",
    "core.campaign_aliases",
    "core.campaigns",
    "core.ad_accounts",
    "core.platforms",
    "core.clients",
)


@dataclass(frozen=True)
class PipelineDb:
    owner: Engine
    etl: Engine
    app: Engine


@pytest.fixture(autouse=True)
def no_real_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test sees a real LLM key or can build a real network client for a provider."""

    def refuse(timeout_seconds: float) -> None:
        raise AssertionError("real LLM network client requested in a test")

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(gemini, "default_http_client", refuse)


@pytest.fixture(scope="session")
def database_url() -> str:
    """DATABASE_URL from the environment (Settings / .env)."""
    try:
        return get_settings().database_url
    except ValidationError:
        pytest.skip("DATABASE_URL is not set")


@pytest.fixture(scope="module")
def module_pipeline_db(database_url: str) -> Iterator[PipelineDb]:
    """A throwaway migrated database with one engine per role the pipeline logs in as."""
    try:
        passwords = role_passwords(get_settings())
    except MissingRolePasswordError as exc:
        pytest.fail(str(exc))
    with temporary_database(database_url) as url:
        owner = create_engine(url)
        with owner.begin() as conn:
            ensure_roles(conn, passwords)
        command.upgrade(alembic_config(url), "head")
        settings = Settings(
            _env_file=None,
            database_url=url.render_as_string(hide_password=False),
            etl_db_password=passwords["etl"],
            app_db_password=passwords["app"],
        )
        etl = create_engine(etl_database_url(settings))
        app = create_engine(app_database_url(settings))
        try:
            yield PipelineDb(owner=owner, etl=etl, app=app)
        finally:
            app.dispose()
            etl.dispose()
            owner.dispose()


@pytest.fixture
def pipeline_db(module_pipeline_db: PipelineDb) -> PipelineDb:
    with module_pipeline_db.owner.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(PIPELINE_TABLES)} RESTART IDENTITY CASCADE"))
    return module_pipeline_db
