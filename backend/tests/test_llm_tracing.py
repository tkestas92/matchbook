"""Langfuse tracer against httpx.MockTransport: what leaves the process, and failures that never matter."""

import base64
import json
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from app.config import ModelPrice, Settings
from app.llm import tracing
from app.llm.client import LLMClient, LLMLimits, create_llm_client, new_run_ledger
from app.llm.cost import CostLedger
from app.llm.fake import FakeProvider
from app.llm.tracing import (
    LangfuseTracer,
    LLMCallEvent,
    NoopTracer,
    create_tracer,
    rejected_count,
)
from pydantic import BaseModel, ConfigDict, SecretStr

# Captured before the autouse fixture replaces it, so its own behaviour can be tested.
REAL_DEFAULT_HTTP_CLIENT = tracing.default_http_client

PUBLIC = "pk-lf-test-0000"
SECRET = "sk-lf-test-not-real-0000"
HOST = "https://langfuse.test"
RUN_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
NOW = datetime(2026, 11, 12, 3, 0, 0, tzinfo=UTC)
DB_URL = "postgresql+psycopg://u:p@localhost:5432/db"
EVENT = LLMCallEvent(
    task="invoice_extraction",
    prompt_id="extract_invoice_v1",
    prompt_hash="abc123",
    model="gemini-3.1-flash-lite",
    status="ok",
    attempts=1,
    input_tokens=1200,
    output_tokens=300,
    cost_eur=Decimal("0.00075"),
    latency_s=1.5,
)
FAILED_EVENT = LLMCallEvent(**{**EVENT.__dict__, "status": "LLMOutputInvalid", "attempts": 2})

Handler = Callable[[httpx.Request], httpx.Response]


class Recorder:
    def __init__(self, response: httpx.Response | Exception | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.response = response if response is not None else httpx.Response(207, json={})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]


def make_tracer(handler: Handler, max_failures: int = 3) -> LangfuseTracer:
    return LangfuseTracer(
        public_key=PUBLIC,
        secret_key=SecretStr(SECRET),
        host=HOST + "/",
        trace_id=RUN_ID,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_failures=max_failures,
        now=lambda: NOW,
    )


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": DB_URL,
        "langfuse_public_key": PUBLIC,
        "langfuse_secret_key": SECRET,
        "langfuse_host": HOST,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def mock_client() -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(Recorder()))


# --- settings and the enable rule -----------------------------------------------------------


def test_tracing_settings_default_to_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    defaults = Settings(_env_file=None)
    assert defaults.langfuse_public_key is None
    assert defaults.langfuse_secret_key is None
    assert defaults.langfuse_host is None
    assert defaults.langfuse_timeout_seconds == 2.0
    assert defaults.langfuse_max_consecutive_failures == 3
    assert isinstance(create_tracer(defaults, RUN_ID), NoopTracer)


def test_secret_key_is_hidden_in_settings() -> None:
    configured = settings()
    assert SECRET not in repr(configured)
    assert SECRET not in str(configured.langfuse_secret_key)


@pytest.mark.parametrize(
    "missing",
    [
        {"langfuse_public_key": None},
        {"langfuse_public_key": "  "},
        {"langfuse_secret_key": None},
        {"langfuse_secret_key": ""},
        {"langfuse_host": None},
        {"langfuse_host": " "},
    ],
)
def test_tracing_is_off_unless_keys_and_host_are_all_set(missing: dict[str, object]) -> None:
    assert isinstance(create_tracer(settings(**missing), RUN_ID), NoopTracer)


def test_non_https_host_keeps_tracing_off(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        tracer = create_tracer(settings(langfuse_host="http://langfuse.test"), RUN_ID)
    assert isinstance(tracer, NoopTracer)
    assert "not an https URL" in caplog.text
    assert SECRET not in caplog.text


def test_configured_settings_build_a_langfuse_tracer() -> None:
    tracer = create_tracer(settings(langfuse_max_consecutive_failures=5), RUN_ID, mock_client())
    assert isinstance(tracer, LangfuseTracer)


def test_tests_cannot_build_a_real_langfuse_client() -> None:
    with pytest.raises(AssertionError, match="real Langfuse network client"):
        create_tracer(settings(), RUN_ID)


def test_default_http_client_has_a_short_bounded_timeout() -> None:
    client = REAL_DEFAULT_HTTP_CLIENT(2.0)
    try:
        assert client.timeout.read == 2.0
        assert client.timeout.connect == 1.0
    finally:
        client.close()


def test_llm_client_is_traced_under_the_run_only_when_given_a_run_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[float] = []

    def fake_default(timeout_seconds: float) -> httpx.Client:
        built.append(timeout_seconds)
        return mock_client()

    monkeypatch.setattr(tracing, "default_http_client", fake_default)
    configured = settings(gemini_api_key="fake-key-for-test", langfuse_timeout_seconds=1.5)
    gemini_http = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    ledger = new_run_ledger(configured, Decimal(0))

    traced = create_llm_client(configured, ledger, http_client=gemini_http, run_id=RUN_ID)
    untraced = create_llm_client(configured, ledger, http_client=gemini_http)

    assert isinstance(traced.tracer, LangfuseTracer)
    assert isinstance(untraced.tracer, NoopTracer)
    assert built == [1.5]


# --- what is sent ---------------------------------------------------------------------------


def test_first_call_sends_the_trace_and_a_metadata_only_generation() -> None:
    recorder = Recorder()
    make_tracer(recorder).on_call(EVENT)
    request = recorder.requests[0]
    body = recorder.bodies()[0]

    assert request.method == "POST"
    assert str(request.url) == f"{HOST}/api/public/ingestion"
    assert set(body) == {"batch"}
    trace, generation = body["batch"]
    assert [trace["type"], generation["type"]] == ["trace-create", "generation-create"]
    for item in body["batch"]:
        assert set(item) == {"id", "timestamp", "type", "body"}

    assert trace["body"] == {
        "id": str(RUN_ID),
        "name": "pipeline-run",
        "timestamp": NOW.isoformat(),
    }
    sent = generation["body"]
    assert set(sent) == {
        "id",
        "traceId",
        "name",
        "model",
        "startTime",
        "endTime",
        "usageDetails",
        "costDetails",
        "level",
        "metadata",
    }
    assert sent["traceId"] == str(RUN_ID)
    assert sent["name"] == "invoice_extraction"
    assert sent["model"] == "gemini-3.1-flash-lite"
    assert sent["startTime"] == "2026-11-12T02:59:58.500000+00:00"
    assert sent["endTime"] == NOW.isoformat()
    assert sent["usageDetails"] == {"input": 1200, "output": 300}
    assert sent["costDetails"] == {"total": 0.00075}
    assert sent["level"] == "DEFAULT"
    assert sent["metadata"] == {
        "prompt_id": "extract_invoice_v1",
        "prompt_hash": "abc123",
        "attempts": 1,
        "status": "ok",
        "cost_eur": "0.00075",
    }


def test_failed_call_is_an_error_level_generation_with_the_class_name() -> None:
    recorder = Recorder()
    make_tracer(recorder).on_call(FAILED_EVENT)
    sent = recorder.bodies()[0]["batch"][-1]["body"]
    assert sent["level"] == "ERROR"
    assert sent["statusMessage"] == "LLMOutputInvalid"
    assert sent["metadata"]["attempts"] == 2


def test_trace_is_created_once_per_tracer() -> None:
    recorder = Recorder()
    tracer = make_tracer(recorder)
    tracer.on_call(EVENT)
    tracer.on_call(EVENT)
    first, second = recorder.bodies()
    assert [item["type"] for item in first["batch"]] == ["trace-create", "generation-create"]
    assert [item["type"] for item in second["batch"]] == ["generation-create"]
    assert tracer.stats.sent == 2


def test_keys_travel_only_as_basic_auth(caplog: pytest.LogCaptureFixture) -> None:
    recorder = Recorder()
    with caplog.at_level(logging.DEBUG):
        make_tracer(recorder).on_call(EVENT)
    request = recorder.requests[0]
    expected = base64.b64encode(f"{PUBLIC}:{SECRET}".encode()).decode()
    assert request.headers["Authorization"] == f"Basic {expected}"
    assert SECRET not in str(request.url)
    assert SECRET not in request.content.decode()
    assert SECRET not in caplog.text


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total: str


def test_no_document_prompt_or_output_text_reaches_langfuse(tmp_path: Path) -> None:
    """End to end: a PII-laden document through LLMClient; the Langfuse body holds none of it."""
    iban, email, phone = "LT12 1000 0111 0100 1000", "billing@vendor.example", "+44 20 7946 0123"
    prompt = "Return the total as JSON."
    (tmp_path / "extract_v1.md").write_text(prompt, encoding="utf-8")
    text = f"Invoice INV-001 total 121.00 EUR. Pay to {iban}. Contact {email}, {phone}."
    output = f'{{"total": "121.00 {email}"}}'
    recorder = Recorder()
    price = ModelPrice(input_eur_per_mtok=Decimal("1"), output_eur_per_mtok=Decimal("2"))
    client = LLMClient(
        provider=FakeProvider([output]),
        limits=LLMLimits(model="test-model", prices={"test-model": price}),
        ledger=CostLedger(run_cap=Decimal("1"), monthly_cap=Decimal("10")),
        tracer=make_tracer(recorder),
        prompts_dir=tmp_path,
    )
    client.complete_json("invoice_extraction", "extract_v1", text, Answer)

    raw = recorder.requests[0].content.decode()
    for forbidden in (iban, email, phone, "INV-001", "121.00", prompt, "[IBAN_1]", "[EMAIL_1]"):
        assert forbidden not in raw
    for item in recorder.bodies()[0]["batch"]:
        assert not {"input", "output"} & set(item["body"])


# --- failures never matter ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "logged"),
    [
        (httpx.Response(500), "HTTP 500"),
        (httpx.Response(401), "HTTP 401"),
        (httpx.ReadTimeout("slow"), "ReadTimeout"),
        (httpx.ConnectError("down"), "ConnectError"),
    ],
)
def test_failures_are_swallowed_counted_and_logged_without_detail(
    caplog: pytest.LogCaptureFixture, response: httpx.Response | Exception, logged: str
) -> None:
    tracer = make_tracer(Recorder(response))
    with caplog.at_level(logging.WARNING):
        tracer.on_call(EVENT)
    assert tracer.stats.failed == 1
    assert tracer.stats.sent == 0
    assert f"Langfuse tracing failed: {logged}" in caplog.text
    assert SECRET not in caplog.text
    assert "slow" not in caplog.text and "down" not in caplog.text


def test_trace_is_resent_after_a_failed_first_call() -> None:
    recorder = Recorder(httpx.Response(503))
    tracer = make_tracer(recorder)
    tracer.on_call(EVENT)
    recorder.response = httpx.Response(207, json={"successes": [], "errors": []})
    tracer.on_call(EVENT)
    assert recorder.bodies()[1]["batch"][0]["type"] == "trace-create"


def test_breaker_stops_sending_after_consecutive_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    recorder = Recorder(httpx.ConnectError("down"))
    tracer = make_tracer(recorder, max_failures=3)
    with caplog.at_level(logging.WARNING):
        for _ in range(10):
            tracer.on_call(EVENT)
    assert len(recorder.requests) == 3
    assert (tracer.stats.failed, tracer.stats.skipped) == (3, 7)
    assert caplog.text.count("off for the rest of this run") == 1


def test_a_success_resets_the_failure_count() -> None:
    recorder = Recorder(httpx.Response(500))
    tracer = make_tracer(recorder, max_failures=2)
    tracer.on_call(EVENT)
    recorder.response = httpx.Response(200, json={})
    tracer.on_call(EVENT)
    recorder.response = httpx.Response(500)
    tracer.on_call(EVENT)
    tracer.on_call(EVENT)
    assert len(recorder.requests) == 4
    assert tracer.stats.skipped == 0


def test_events_langfuse_refuses_are_counted_as_rejected() -> None:
    errors = [{"id": "x", "status": 400, "message": "invalid"}]
    tracer = make_tracer(Recorder(httpx.Response(207, json={"successes": [], "errors": errors})))
    tracer.on_call(EVENT)
    assert (tracer.stats.sent, tracer.stats.rejected) == (1, 1)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"errors": [{"id": "x"}]}),
        httpx.Response(207, content=b"not json"),
        httpx.Response(207, json=[1, 2]),
        httpx.Response(207, json={"errors": "nope"}),
    ],
)
def test_rejected_count_is_zero_unless_a_207_lists_errors(response: httpx.Response) -> None:
    assert rejected_count(response) == 0
