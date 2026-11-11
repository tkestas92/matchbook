"""Tracer hook for LLM calls. No-op until task 2.9 adds Langfuse.

A tracer only ever sees an `LLMCallEvent`, whose text fields are masked (threat model T2).
"""

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMCallEvent:
    task: str
    prompt_id: str
    prompt_hash: str
    model: str
    status: str  # "ok" or the error class name
    input_tokens: int
    output_tokens: int
    cost_eur: Decimal
    latency_s: float
    masked_input: str
    masked_output: str


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
