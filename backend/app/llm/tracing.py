"""Tracer hook for LLM calls, with a metadata-only Langfuse adapter over plain httpx (decision 048).

A tracer only ever sees an `LLMCallEvent`, which has no text field: no prompt, document or model
output can reach a trace, masked or not (threat model T2). Tracing is off unless the Langfuse keys
and host are all configured, and a tracing failure never fails or retries a call. Spans go to
Langfuse's OTLP/HTTP JSON endpoint; the legacy ingestion API is gone from 2026-11-16.
"""

import json
import logging
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

import httpx
from app.config import Settings
from pydantic import SecretStr

logger = logging.getLogger(__name__)

OTLP_TRACES_PATH = "/api/public/otel/v1/traces"
INGESTION_VERSION_HEADER = {"x-langfuse-ingestion-version": "4"}
SCOPE_NAME = "matchbook.llm"
SPAN_KIND_INTERNAL = 1
TRACE_NAME = "pipeline-run"
OK_STATUS = "ok"
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class LLMCallEvent:
    task: str
    prompt_id: str
    prompt_hash: str
    model: str
    status: str  # "ok" or the error class name
    attempts: int
    input_tokens: int
    output_tokens: int
    cost_eur: Decimal
    latency_s: float


class Tracer(Protocol):
    def on_call(self, event: LLMCallEvent) -> None: ...


class NoopTracer:
    def on_call(self, event: LLMCallEvent) -> None:
        return None


def safe_trace(tracer: Tracer, event: LLMCallEvent) -> None:
    """A tracing failure never fails a call; the log line names the error type only."""
    try:
        tracer.on_call(event)
    except Exception as exc:
        logger.warning("LLM tracer failed: %s", type(exc).__name__)


@dataclass
class TracerStats:
    sent: int = 0
    rejected: int = 0
    failed: int = 0
    skipped: int = 0


def default_http_client(timeout_seconds: float) -> httpx.Client:
    """The only place a real Langfuse client is built. Tests replace this function."""
    return httpx.Client(timeout=httpx.Timeout(timeout_seconds, connect=min(1.0, timeout_seconds)))


class LangfuseTracer:
    """One OTLP span per call, no retry; after `max_failures` failures in a row it stops sending.

    A refused span (OTLP partial success) counts toward the breaker too: nothing was stored.
    """

    def __init__(
        self,
        public_key: str,
        secret_key: SecretStr,
        host: str,
        trace_id: uuid.UUID,
        client: httpx.Client,
        max_failures: int = 3,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._public_key = public_key
        self._secret_key = secret_key
        self._url = host.rstrip("/") + OTLP_TRACES_PATH
        self._trace_id = trace_id.hex
        self._client = client
        self._max_failures = max_failures
        self._now = now
        self._failures_in_row = 0
        self._rejection_logged = False
        self.stats = TracerStats()

    def on_call(self, event: LLMCallEvent) -> None:
        if self._failures_in_row >= self._max_failures:
            self.stats.skipped += 1
            return
        try:
            auth = httpx.BasicAuth(self._public_key, self._secret_key.get_secret_value())
            response = self._client.post(
                self._url, auth=auth, headers=INGESTION_VERSION_HEADER, json=self._payload(event)
            )
        except Exception as exc:
            self._fail(type(exc).__name__)
            return
        if not response.is_success:
            self._fail(f"HTTP {response.status_code}")
            return
        rejected = rejected_spans(response)
        if rejected:
            self._reject(rejected)
            return
        self._failures_in_row = 0
        self.stats.sent += 1

    def _fail(self, reason: str) -> None:
        self.stats.failed += 1
        logger.warning("Langfuse tracing failed: %s", reason)
        self._count_toward_breaker()

    def _reject(self, count: int) -> None:
        """Logs counts only, once per tracer; Langfuse's error message is never logged."""
        self.stats.rejected += count
        if not self._rejection_logged:
            self._rejection_logged = True
            logger.warning(
                "Langfuse rejected %d span(s) (OTLP partial success); "
                "further rejections are counted only",
                count,
            )
        self._count_toward_breaker()

    def _count_toward_breaker(self) -> None:
        self._failures_in_row += 1
        if self._failures_in_row == self._max_failures:
            logger.warning(
                "Langfuse tracing is off for the rest of this run after %d failures in a row",
                self._max_failures,
            )

    def _payload(self, event: LLMCallEvent) -> dict[str, Any]:
        scope_spans = {"scope": {"name": SCOPE_NAME}, "spans": [self._span(event)]}
        return {"resourceSpans": [{"resource": {"attributes": []}, "scopeSpans": [scope_spans]}]}

    def _span(self, event: LLMCallEvent) -> dict[str, Any]:
        """A parentless generation span; all of a run's spans share the run's trace id."""
        end = self._now()
        return {
            "traceId": self._trace_id,
            "spanId": secrets.token_hex(8),
            "name": event.task,
            "kind": SPAN_KIND_INTERNAL,
            "startTimeUnixNano": unix_nano(end - timedelta(seconds=event.latency_s)),
            "endTimeUnixNano": unix_nano(end),
            "attributes": generation_attributes(event),
        }


def generation_attributes(event: LLMCallEvent) -> list[dict[str, Any]]:
    """The whole attribute set; tests pin these keys. No input or output attribute, ever."""
    failed = event.status != OK_STATUS
    usage = {"input": event.input_tokens, "output": event.output_tokens}
    # Display only, for Langfuse's cost totals; the exact Decimal is in metadata.
    cost = {"total": float(event.cost_eur)}
    values = {
        "langfuse.trace.name": TRACE_NAME,
        "langfuse.observation.type": "generation",
        "langfuse.observation.model.name": event.model,
        "langfuse.observation.usage_details": json.dumps(usage),
        "langfuse.observation.cost_details": json.dumps(cost),
        "langfuse.observation.level": "ERROR" if failed else "DEFAULT",
        "langfuse.observation.metadata.prompt_id": event.prompt_id,
        "langfuse.observation.metadata.prompt_hash": event.prompt_hash,
        "langfuse.observation.metadata.attempts": str(event.attempts),
        "langfuse.observation.metadata.status": event.status,
        "langfuse.observation.metadata.cost_eur": str(event.cost_eur),
    }
    if failed:
        values["langfuse.observation.status_message"] = event.status
    return [{"key": key, "value": {"stringValue": value}} for key, value in values.items()]


def unix_nano(at: datetime) -> str:
    """OTLP JSON encodes 64-bit integers as decimal strings; integer math, no float rounding."""
    return str((at - EPOCH) // timedelta(microseconds=1) * 1000)


def rejected_spans(response: httpx.Response) -> int:
    """`partialSuccess.rejectedSpans` of a 200 answer (int64, so a number or a string), else 0."""
    if response.status_code != 200:
        return 0
    try:
        partial = response.json().get("partialSuccess")
        count = int(partial.get("rejectedSpans", 0))
    except (ValueError, TypeError, AttributeError):
        return 0
    return max(count, 0)


def create_tracer(
    settings: Settings, run_id: uuid.UUID, http_client: httpx.Client | None = None
) -> Tracer:
    """Langfuse only when public key, secret key and an https host are all set; otherwise a no-op."""
    public_key = (settings.langfuse_public_key or "").strip()
    secret_key = settings.langfuse_secret_key or SecretStr("")
    host = (settings.langfuse_host or "").strip()
    if not (public_key and secret_key.get_secret_value().strip() and host):
        return NoopTracer()
    if not host.startswith("https://"):
        logger.warning("LANGFUSE_HOST is not an https URL; LLM tracing is off")
        return NoopTracer()
    # Looked up on the module so the test suite can block real network clients in one place.
    return LangfuseTracer(
        public_key=public_key,
        secret_key=secret_key,
        host=host,
        trace_id=run_id,
        client=http_client or default_http_client(settings.langfuse_timeout_seconds),
        max_failures=settings.langfuse_max_consecutive_failures,
    )
