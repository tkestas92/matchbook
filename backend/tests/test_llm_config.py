"""LLM settings and the production client factory (decision 021: tests ignore the outer env)."""

from decimal import Decimal

import httpx
import pytest
from app.config import Settings
from app.llm.client import MissingApiKeyError, create_llm_client, new_run_ledger
from app.llm.gemini import GeminiProvider

DB_URL = "postgresql+psycopg://u:p@localhost:5432/db"
LLM_ENV = (
    "GEMINI_API_KEY",
    "LLM_MODEL",
    "LLM_RUN_CAP_EUR",
    "LLM_MONTHLY_CAP_EUR",
    "LLM_PRICES_EUR_PER_MTOK",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    for name in LLM_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_llm_defaults(clean_env: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None)
    assert settings.llm_provider == "gemini"
    assert settings.llm_model == "gemini-2.5-flash-lite"
    assert settings.gemini_api_key is None
    assert settings.llm_run_cap_eur == Decimal("0.20")
    assert settings.llm_monthly_cap_eur == Decimal("1")
    price = settings.llm_prices_eur_per_mtok["gemini-2.5-flash-lite"]
    assert isinstance(price.input_eur_per_mtok, Decimal)


def test_model_and_caps_come_from_the_environment(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("LLM_MODEL", "gemini-other")
    clean_env.setenv("LLM_RUN_CAP_EUR", "0.05")
    clean_env.setenv(
        "LLM_PRICES_EUR_PER_MTOK",
        '{"gemini-other": {"input_eur_per_mtok": "0.3", "output_eur_per_mtok": "2.5"}}',
    )
    settings = Settings(_env_file=None)
    assert settings.llm_model == "gemini-other"
    assert settings.llm_run_cap_eur == Decimal("0.05")
    assert settings.llm_prices_eur_per_mtok["gemini-other"].output_eur_per_mtok == Decimal("2.5")


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_key_is_a_clear_error(clean_env: pytest.MonkeyPatch, value: str | None) -> None:
    if value is not None:
        clean_env.setenv("GEMINI_API_KEY", value)
    settings = Settings(_env_file=None)
    with pytest.raises(MissingApiKeyError, match="GEMINI_API_KEY is not set"):
        create_llm_client(settings, new_run_ledger(settings, Decimal(0)))


def test_key_is_hidden_and_used_only_by_the_provider(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("GEMINI_API_KEY", "fake-key-for-test")
    settings = Settings(_env_file=None)
    assert "fake-key-for-test" not in repr(settings)
    assert "fake-key-for-test" not in str(settings.gemini_api_key)
    mock = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    client = create_llm_client(settings, new_run_ledger(settings, Decimal(0)), http_client=mock)
    assert isinstance(client.provider, GeminiProvider)
    assert client.limits.model == "gemini-2.5-flash-lite"


def test_tests_cannot_build_a_real_network_client(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("GEMINI_API_KEY", "fake-key-for-test")
    settings = Settings(_env_file=None)
    with pytest.raises(AssertionError, match="real LLM network client"):
        create_llm_client(settings, new_run_ledger(settings, Decimal(0)))


def test_run_ledger_uses_the_configured_caps(clean_env: pytest.MonkeyPatch) -> None:
    ledger = new_run_ledger(Settings(_env_file=None), Decimal("0.4"))
    assert (ledger.run_cap, ledger.monthly_cap) == (Decimal("0.20"), Decimal("1"))
    assert ledger.spent_this_month_before_run == Decimal("0.4")
