"""Tracer hook for LLM calls, with a metadata-only Langfuse adapter over plain httpx (decision 048).

A tracer only ever sees an `LLMCallEvent`, which has no text field: no prompt, document or model
output can reach a trace, masked or not (threat model T2). Tracing is off unless the Langfuse keys
and host are all configured, and a tracing failure never fails or retries a call.
"""

import logging
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

INGESTION_PATH = "/api/public/ingestion"
TRACE_NAME = "pipeline-run"
OK_STATUS = "ok"
MULTI_STATUS = 207


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
    """One POST per call, no retry; after `max_failures` failures in a row it stops sending."""

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
        self._url = host.rstrip("/") + INGESTION_PATH
        self._trace_id = str(trace_id)
        self._client = client
        self._max_failures = max_failures
        self._now = now
        self._failures_in_row = 0
        self._trace_sent = False
        self.stats = TracerStats()

    def on_call(self, event: LLMCallEvent) -> None:
        if self._failures_in_row >= self._max_failures:
            self.stats.skipped += 1
            return
        try:
            auth = httpx.BasicAuth(self._public_key, self._secret_key.get_secret_value())
            response = self._client.post(self._url, auth=auth, json=self._batch(event))
        except Exception as exc:
            self._fail(type(exc).__name__)
            return
        if not response.is_success:
            self._fail(f"HTTP {response.status_code}")
            return
        self._failures_in_row = 0
        self._trace_sent = True
        self.stats.sent += 1
        self.stats.rejected += rejected_count(response)

    def _fail(self, reason: str) -> None:
        self.stats.failed += 1
        self._failures_in_row += 1
        logger.warning("Langfuse tracing failed: %s", reason)
        if self._failures_in_row == self._max_failures:
            logger.warning(
                "Langfuse tracing is off for the rest of this run after %d failures in a row",
                self._max_failures,
            )

    def _batch(self, event: LLMCallEvent) -> dict[str, Any]:
        end = self._now()
        items = [] if self._trace_sent else [envelope("trace-create", end, self._trace(end))]
        items.append(envelope("generation-create", end, self._generation(event, end)))
        return {"batch": items}

    def _trace(self, at: datetime) -> dict[str, Any]:
        return {"id": self._trace_id, "name": TRACE_NAME, "timestamp": at.isoformat()}

    def _generation(self, event: LLMCallEvent, end: datetime) -> dict[str, Any]:
        failed = event.status != OK_STATUS
        body: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "traceId": self._trace_id,
            "name": event.task,
            "model": event.model,
            "startTime": (end - timedelta(seconds=event.latency_s)).isoformat(),
            "endTime": end.isoformat(),
            "usageDetails": {"input": event.input_tokens, "output": event.output_tokens},
            # Display only, for Langfuse's cost totals; the exact Decimal is in metadata.
            "costDetails": {"total": float(event.cost_eur)},
            "level": "ERROR" if failed else "DEFAULT",
            "metadata": {
                "prompt_id": event.prompt_id,
                "prompt_hash": event.prompt_hash,
                "attempts": event.attempts,
                "status": event.status,
                "cost_eur": str(event.cost_eur),
            },
        }
        if failed:
            body["statusMessage"] = event.status
        return body


def envelope(kind: str, at: datetime, body: dict[str, Any]) -> dict[str, Any]:
    return {"id": str(uuid.uuid4()), "timestamp": at.isoformat(), "type": kind, "body": body}


def rejected_count(response: httpx.Response) -> int:
    """Langfuse answers 207 with a list of events it refused; their content is never logged."""
    if response.status_code != MULTI_STATUS:
        return 0
    try:
        errors = response.json().get("errors")
    except (ValueError, AttributeError):
        return 0
    return len(errors) if isinstance(errors, list) else 0


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
