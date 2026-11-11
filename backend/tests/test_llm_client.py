"""LLMClient with the fake provider only: no network, no key, no database."""

import dataclasses
from decimal import Decimal
from pathlib import Path

import pytest
from app.config import ModelPrice
from app.llm.client import (
    InputTooLarge,
    LLMClient,
    LLMLimits,
    LLMOutputInvalid,
    LLMResult,
    RetryPolicy,
)
from app.llm.cost import CostCapExceeded, CostLedger, UnknownModelPrice
from app.llm.fake import FakeProvider
from app.llm.provider import (
    RATE_LIMITED,
    SERVER_ERROR,
    ModelRequest,
    ModelResponse,
    ProviderError,
    RetryableProviderError,
)
from app.llm.tracing import LLMCallEvent
from pydantic import BaseModel, ConfigDict

MODEL = "test-model"
PRICE = ModelPrice(input_eur_per_mtok=Decimal("1"), output_eur_per_mtok=Decimal("2"))
IBAN = "LT12 1000 0111 0100 1000"
EMAIL = "billing@vendor.example"
PHONE = "+44 20 7946 0123"
TEXT = f"Invoice INV-001 total 121.00 EUR. Pay to {IBAN}. Contact {EMAIL}, {PHONE}."
VALID = '{"total": "121.00"}'


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total: str


class LooseAnswer(BaseModel):
    total: str


class RecordingTracer:
    def __init__(self) -> None:
        self.events: list[LLMCallEvent] = []

    def on_call(self, event: LLMCallEvent) -> None:
        self.events.append(event)


class BrokenTracer:
    def on_call(self, event: LLMCallEvent) -> None:
        raise RuntimeError("tracer down")


@pytest.fixture
def prompts_dir(tmp_path: Path) -> Path:
    (tmp_path / "extract_v1.md").write_text("Return the total as JSON.", encoding="utf-8")
    return tmp_path


def make_client(
    prompts_dir: Path,
    fake: FakeProvider,
    ledger: CostLedger | None = None,
    tracer: object | None = None,
    sleeps: list[float] | None = None,
    **limits: object,
) -> LLMClient:
    return LLMClient(
        provider=fake,
        limits=LLMLimits(model=MODEL, prices={MODEL: PRICE}, **limits),  # type: ignore[arg-type]
        ledger=ledger or CostLedger(run_cap=Decimal("1"), monthly_cap=Decimal("10")),
        tracer=tracer,  # type: ignore[arg-type]
        prompts_dir=prompts_dir,
        sleep=(sleeps.append if sleeps is not None else lambda _: None),
    )


def complete(
    client: LLMClient, schema: type[BaseModel] = Answer, text: str = TEXT
) -> LLMResult[BaseModel]:
    return client.complete_json("invoice_extraction", "extract_v1", text, schema)


# --- masking (T2) ---------------------------------------------------------------------------


def test_provider_receives_masked_text_only(prompts_dir: Path) -> None:
    fake = FakeProvider([VALID])
    complete(make_client(prompts_dir, fake))
    sent = fake.requests[0].user_text
    for original in (IBAN, EMAIL, PHONE):
        assert original not in sent
    assert "[IBAN_1]" in sent and "[EMAIL_1]" in sent and "[PHONE_1]" in sent
    assert "INV-001" in sent and "121.00" in sent


def test_tracer_receives_masked_text_only(prompts_dir: Path) -> None:
    tracer = RecordingTracer()
    fake = FakeProvider([f'{{"total": "{EMAIL}"}}'])
    complete(make_client(prompts_dir, fake, tracer=tracer))
    event = tracer.events[0]
    assert event.status == "ok"
    for original in (IBAN, EMAIL, PHONE):
        assert original not in event.masked_input
        assert original not in event.masked_output


def test_failing_tracer_does_not_fail_the_call(prompts_dir: Path) -> None:
    result = complete(make_client(prompts_dir, FakeProvider([VALID]), tracer=BrokenTracer()))
    assert result.data == Answer(total="121.00")


# --- no tools (T4) --------------------------------------------------------------------------


def test_request_shape_has_no_tools() -> None:
    fields = {f.name for f in dataclasses.fields(ModelRequest)}
    assert fields == {
        "model",
        "system",
        "user_text",
        "json_schema",
        "max_output_tokens",
        "temperature",
    }


def test_request_is_temperature_zero_with_the_schema(prompts_dir: Path) -> None:
    fake = FakeProvider([VALID])
    complete(make_client(prompts_dir, fake, max_output_tokens=100))
    request = fake.requests[0]
    assert request.temperature == 0
    assert request.json_schema == Answer.model_json_schema()
    assert request.system == "Return the total as JSON."
    assert request.max_output_tokens == 100


# --- result ---------------------------------------------------------------------------------


def test_result_carries_tokens_cost_model_and_prompt_hash(prompts_dir: Path) -> None:
    ledger = CostLedger(run_cap=Decimal("1"), monthly_cap=Decimal("10"))
    fake = FakeProvider([ModelResponse(VALID, input_tokens=1000, output_tokens=500)])
    result = complete(make_client(prompts_dir, fake, ledger=ledger))
    assert result.data == Answer(total="121.00")
    assert (result.input_tokens, result.output_tokens) == (1000, 500)
    assert result.cost_eur == Decimal("0.002")  # 1000 × 1 + 500 × 2 per million
    assert result.model == MODEL and result.prompt_id == "extract_v1"
    assert len(result.prompt_hash) == 64
    assert ledger.total == Decimal("0.002") and ledger.calls == 1


# --- invalid output (T3) --------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["not json", '{"total": "1.00", "approved": true}', '{"amount": "1.00"}', ""],
)
def test_invalid_output_retries_once_then_fails(prompts_dir: Path, bad: str) -> None:
    tracer = RecordingTracer()
    fake = FakeProvider([bad, bad])
    with pytest.raises(LLMOutputInvalid):
        complete(make_client(prompts_dir, fake, tracer=tracer))
    assert len(fake.requests) == 2
    assert tracer.events[0].status == "LLMOutputInvalid"


def test_invalid_then_valid_succeeds(prompts_dir: Path) -> None:
    fake = FakeProvider(["{oops", VALID])
    result = complete(make_client(prompts_dir, fake))
    assert result.attempts == 2
    assert len(fake.requests) == 2


def test_truncated_answer_counts_as_invalid(prompts_dir: Path) -> None:
    truncated = ModelResponse(VALID, input_tokens=1, output_tokens=1, finish_complete=False)
    fake = FakeProvider([truncated, truncated])
    with pytest.raises(LLMOutputInvalid):
        complete(make_client(prompts_dir, fake))


def test_schema_must_forbid_extra_fields(prompts_dir: Path) -> None:
    fake = FakeProvider([VALID])
    with pytest.raises(ValueError, match="extra='forbid'"):
        complete(make_client(prompts_dir, fake), schema=LooseAnswer)
    assert fake.requests == []


# --- bounds and caps (T5) -------------------------------------------------------------------


def test_input_over_the_limit_is_never_sent(prompts_dir: Path) -> None:
    fake = FakeProvider([VALID])
    with pytest.raises(InputTooLarge):
        complete(make_client(prompts_dir, fake, max_input_chars=10), text="x" * 11)
    assert fake.requests == []


def test_run_cap_refuses_before_the_call(prompts_dir: Path) -> None:
    ledger = CostLedger(run_cap=Decimal("0.000001"), monthly_cap=Decimal("10"))
    fake = FakeProvider([VALID])
    with pytest.raises(CostCapExceeded, match="per-run"):
        complete(make_client(prompts_dir, fake, ledger=ledger))
    assert fake.requests == []


def test_monthly_cap_refuses_before_the_call(prompts_dir: Path) -> None:
    ledger = CostLedger(
        run_cap=Decimal("1"),
        monthly_cap=Decimal("1"),
        spent_this_month_before_run=Decimal("0.999999"),
    )
    fake = FakeProvider([VALID])
    with pytest.raises(CostCapExceeded, match="monthly"):
        complete(make_client(prompts_dir, fake, ledger=ledger))
    assert fake.requests == []


def test_cap_reached_mid_run_stops_later_calls(prompts_dir: Path) -> None:
    ledger = CostLedger(run_cap=Decimal("0.01"), monthly_cap=Decimal("10"))
    expensive = ModelResponse(VALID, input_tokens=0, output_tokens=4500)  # EUR 0.009
    fake = FakeProvider([expensive, VALID])
    client = make_client(prompts_dir, fake, ledger=ledger, max_output_tokens=2048)
    complete(client)
    with pytest.raises(CostCapExceeded):
        complete(client)
    assert len(fake.requests) == 1


def test_unknown_model_price_is_refused(prompts_dir: Path) -> None:
    fake = FakeProvider([VALID])
    client = LLMClient(
        provider=fake,
        limits=LLMLimits(model="unpriced", prices={MODEL: PRICE}),
        ledger=CostLedger(run_cap=Decimal("1"), monthly_cap=Decimal("10")),
        prompts_dir=prompts_dir,
    )
    with pytest.raises(UnknownModelPrice):
        complete(client)
    assert fake.requests == []


# --- retries (decision 027 backoff) ---------------------------------------------------------


def rate_limited(retry_after: float = 0.0) -> RetryableProviderError:
    return RetryableProviderError("429", RATE_LIMITED, retry_after)


def server_error() -> RetryableProviderError:
    return RetryableProviderError("500", SERVER_ERROR)


def test_server_errors_back_off_then_succeed(prompts_dir: Path) -> None:
    sleeps: list[float] = []
    fake = FakeProvider([server_error(), server_error(), VALID])
    complete(make_client(prompts_dir, fake, sleeps=sleeps))
    assert sleeps == [1.0, 2.0]
    assert len(fake.requests) == 3


def test_429_waits_for_retry_after_when_longer(prompts_dir: Path) -> None:
    sleeps: list[float] = []
    fake = FakeProvider([rate_limited(7), rate_limited(0), VALID])
    complete(make_client(prompts_dir, fake, sleeps=sleeps))
    assert sleeps == [7.0, 2.0]


def test_server_errors_are_bounded(prompts_dir: Path) -> None:
    policy = RetryPolicy(max_attempts_5xx=3)
    fake = FakeProvider([server_error()] * 5)
    with pytest.raises(RetryableProviderError):
        complete(make_client(prompts_dir, fake, retry=policy))
    assert len(fake.requests) == 3


def test_retry_after_over_the_limit_fails(prompts_dir: Path) -> None:
    sleeps: list[float] = []
    fake = FakeProvider([rate_limited(3600), VALID])
    with pytest.raises(ProviderError, match="limit"):
        complete(make_client(prompts_dir, fake, sleeps=sleeps))
    assert sleeps == [] and len(fake.requests) == 1


def test_other_provider_errors_fail_at_once(prompts_dir: Path) -> None:
    fake = FakeProvider([ProviderError("HTTP 400"), VALID])
    with pytest.raises(ProviderError):
        complete(make_client(prompts_dir, fake))
    assert len(fake.requests) == 1


def test_cap_is_checked_before_every_retry(prompts_dir: Path) -> None:
    class SpendingFake(FakeProvider):
        def generate(self, request: ModelRequest) -> ModelResponse:
            ledger.record(Decimal("1.5"), 0)  # the whole run budget is gone meanwhile
            return super().generate(request)

    ledger = CostLedger(run_cap=Decimal("1.5"), monthly_cap=Decimal("10"))
    fake = SpendingFake([server_error(), VALID])
    with pytest.raises(CostCapExceeded):
        complete(make_client(prompts_dir, fake, ledger=ledger))
    assert len(fake.requests) == 1
