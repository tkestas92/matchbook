"""The one way application code calls a model (AGENTS.md rule 6, decisions 010 and 039).

`LLMClient.complete_json` runs every guard before anything leaves the process:
strict schema → input size → PII masking → prompt → cost cap → provider (bounded retries) →
schema validation (one retry on invalid output, then failure) → tracer (metadata only, no text).
There is no tools parameter anywhere in this layer (threat model T4).
"""

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import httpx
from app.config import ModelPrice, Settings
from app.llm import gemini
from app.llm.cost import CostLedger, cost_of, price_for, worst_case_cost
from app.llm.masking import mask
from app.llm.prompts import PROMPTS_DIR, load_prompt
from app.llm.provider import (
    RATE_LIMITED,
    LLMError,
    ModelRequest,
    ModelResponse,
    Provider,
    ProviderError,
    RetryableProviderError,
)
from app.llm.tracing import LLMCallEvent, NoopTracer, Tracer, create_tracer, safe_trace
from pydantic import BaseModel, ValidationError

OUTPUT_ATTEMPTS = 2  # the first answer plus one retry on invalid output, never a repair loop


class MissingApiKeyError(LLMError):
    """The provider's API key is not configured."""


class InputTooLarge(LLMError):
    """The input text is over the configured limit, so it is never sent."""


class LLMOutputInvalid(LLMError):
    """The model's answer did not validate against the schema, even after one retry."""


@dataclass(frozen=True)
class RetryPolicy:
    """Decision 027's backoff: min(cap, base × 2^(n-1)); a 429 waits at least its Retry-After."""

    base_delay: float = 1.0
    cap: float = 30.0
    max_attempts_429: int = 6
    max_attempts_5xx: int = 4
    max_wait: float = 60.0

    def backoff(self, attempt: int) -> float:
        return min(self.cap, self.base_delay * 2 ** (attempt - 1))

    def max_attempts(self, kind: str) -> int:
        return self.max_attempts_429 if kind == RATE_LIMITED else self.max_attempts_5xx

    def delay(self, error: RetryableProviderError, attempt: int) -> float:
        backoff = self.backoff(attempt)
        return max(error.retry_after, backoff) if error.kind == RATE_LIMITED else backoff


@dataclass(frozen=True)
class LLMLimits:
    model: str
    prices: dict[str, ModelPrice]
    max_input_chars: int = 50_000
    max_output_tokens: int = 2048
    retry: RetryPolicy = field(default_factory=RetryPolicy)

    @classmethod
    def from_settings(cls, settings: Settings) -> "LLMLimits":
        return cls(
            model=settings.llm_model,
            prices=settings.llm_prices_eur_per_mtok,
            max_input_chars=settings.llm_max_input_chars,
            max_output_tokens=settings.llm_max_output_tokens,
            retry=RetryPolicy(
                base_delay=settings.llm_retry_base_seconds,
                cap=settings.llm_retry_cap_seconds,
                max_attempts_429=settings.llm_retry_attempts_429,
                max_attempts_5xx=settings.llm_retry_attempts_5xx,
                max_wait=settings.llm_max_retry_wait_seconds,
            ),
        )


@dataclass(frozen=True)
class LLMResult[T: BaseModel]:
    data: T
    input_tokens: int
    output_tokens: int
    cost_eur: Decimal
    latency_s: float
    model: str
    prompt_id: str
    prompt_hash: str
    attempts: int


@dataclass
class CallTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_eur: Decimal = Decimal(0)
    attempts: int = 0

    def add(self, response: ModelResponse, cost: Decimal) -> None:
        self.input_tokens += response.input_tokens
        self.output_tokens += response.output_tokens
        self.cost_eur += cost
        self.attempts += 1


class LLMClient:
    def __init__(
        self,
        provider: Provider,
        limits: LLMLimits,
        ledger: CostLedger,
        tracer: Tracer | None = None,
        prompts_dir: Path = PROMPTS_DIR,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.provider = provider
        self.limits = limits
        self.ledger = ledger
        self.tracer = tracer or NoopTracer()
        self.prompts_dir = prompts_dir
        self.sleep = sleep
        self.clock = clock

    def complete_json[T: BaseModel](
        self, task: str, prompt_id: str, text: str, schema: type[T]
    ) -> LLMResult[T]:
        require_strict_schema(schema)
        if len(text) > self.limits.max_input_chars:
            raise InputTooLarge(
                f"input has {len(text)} characters, the limit is {self.limits.max_input_chars}"
            )
        masked = mask(text)
        prompt = load_prompt(prompt_id, self.prompts_dir)
        price = price_for(self.limits.prices, self.limits.model)
        request = ModelRequest(
            model=self.limits.model,
            system=prompt.text,
            user_text=masked,
            json_schema=schema.model_json_schema(),
            max_output_tokens=self.limits.max_output_tokens,
        )
        totals = CallTotals()
        started = self.clock()
        status = "ok"
        try:
            for _ in range(OUTPUT_ATTEMPTS):
                response = self._send(request, price, totals)
                data = parse_output(response, schema)
                if data is not None:
                    return LLMResult(
                        data=data,
                        input_tokens=totals.input_tokens,
                        output_tokens=totals.output_tokens,
                        cost_eur=totals.cost_eur,
                        latency_s=self.clock() - started,
                        model=self.limits.model,
                        prompt_id=prompt.id,
                        prompt_hash=prompt.sha256,
                        attempts=totals.attempts,
                    )
            raise LLMOutputInvalid(
                f"{task}: model output failed {schema.__name__} validation "
                f"after {OUTPUT_ATTEMPTS} attempts"
            )
        except Exception as exc:
            status = type(exc).__name__
            raise
        finally:
            event = LLMCallEvent(
                task=task,
                prompt_id=prompt.id,
                prompt_hash=prompt.sha256,
                model=self.limits.model,
                status=status,
                attempts=totals.attempts,
                input_tokens=totals.input_tokens,
                output_tokens=totals.output_tokens,
                cost_eur=totals.cost_eur,
                latency_s=self.clock() - started,
            )
            safe_trace(self.tracer, event)

    def _send(self, request: ModelRequest, price: ModelPrice, totals: CallTotals) -> ModelResponse:
        """One answer from the provider. The cap is checked before every attempt."""
        failures: dict[str, int] = {}
        estimate = worst_case_cost(
            price, request.system + request.user_text, request.max_output_tokens
        )
        while True:
            self.ledger.check(estimate)
            try:
                response = self.provider.generate(request)
            except RetryableProviderError as exc:
                failures[exc.kind] = failures.get(exc.kind, 0) + 1
                policy = self.limits.retry
                if failures[exc.kind] >= policy.max_attempts(exc.kind):
                    raise
                delay = policy.delay(exc, failures[exc.kind])
                if delay > policy.max_wait:
                    raise ProviderError(
                        f"provider asked to wait {delay:.0f} s, over the {policy.max_wait:.0f} s limit"
                    ) from exc
                self.sleep(delay)
                continue
            cost = cost_of(price, response.input_tokens, response.output_tokens)
            self.ledger.record(cost, response.input_tokens + response.output_tokens)
            totals.add(response, cost)
            return response


def require_strict_schema(schema: type[BaseModel]) -> None:
    """Model output may not add fields (threat model T3)."""
    if schema.model_config.get("extra") != "forbid":
        raise ValueError(f"{schema.__name__} must set model_config extra='forbid'")


def parse_output[T: BaseModel](response: ModelResponse, schema: type[T]) -> T | None:
    if not response.finish_complete:
        return None
    try:
        return schema.model_validate_json(response.text)
    except ValidationError:
        return None


def new_run_ledger(settings: Settings, spent_this_month_before_run: Decimal) -> CostLedger:
    return CostLedger(
        run_cap=settings.llm_run_cap_eur,
        monthly_cap=settings.llm_monthly_cap_eur,
        spent_this_month_before_run=spent_this_month_before_run,
    )


def create_llm_client(
    settings: Settings,
    ledger: CostLedger,
    tracer: Tracer | None = None,
    http_client: httpx.Client | None = None,
    run_id: uuid.UUID | None = None,
) -> LLMClient:
    """The production client. A missing key is an error, never a default.

    With a `run_id` and no explicit tracer, calls are traced under that run when Langfuse is set up.
    """
    key = settings.gemini_api_key
    if key is None or not key.get_secret_value().strip():
        raise MissingApiKeyError(
            "GEMINI_API_KEY is not set; add it to .env (see .env.example). "
            "Tests use FakeProvider and need no key."
        )
    # Looked up on the module so the test suite can block real network clients in one place.
    provider = gemini.GeminiProvider(
        api_key=key,
        client=http_client or gemini.default_http_client(settings.llm_timeout_seconds),
        base_url=settings.gemini_base_url,
        max_response_bytes=settings.llm_max_response_bytes,
    )
    if tracer is None and run_id is not None:
        tracer = create_tracer(settings, run_id)
    return LLMClient(provider, LLMLimits.from_settings(settings), ledger, tracer)
