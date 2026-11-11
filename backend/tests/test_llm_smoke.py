"""The smoke check's report logic with the fake provider only: no network, no key, no database."""

from decimal import Decimal
from pathlib import Path

from app.config import ModelPrice
from app.llm.client import LLMClient, LLMLimits
from app.llm.cost import CostLedger, cost_of
from app.llm.fake import FakeProvider, Scripted
from app.llm.masking import mask
from app.llm.provider import ProviderError
from app.llm.smoke import PROMPT_ID, SMOKE_EMAIL, SMOKE_IBAN, SMOKE_TEXT, run_smoke

MODEL = "test-model"
PRICE = ModelPrice(input_eur_per_mtok=Decimal("1"), output_eur_per_mtok=Decimal("2"))
VALID = '{"invoice_number": "SMOKE-001", "total_eur": "121.00"}'


def make_client(tmp_path: Path, fake: FakeProvider) -> LLMClient:
    (tmp_path / f"{PROMPT_ID}.md").write_text("Return JSON.", encoding="utf-8")
    return LLMClient(
        provider=fake,
        limits=LLMLimits(model=MODEL, prices={MODEL: PRICE}),
        ledger=CostLedger(run_cap=Decimal("1"), monthly_cap=Decimal("10")),
        prompts_dir=tmp_path,
        sleep=lambda _: None,
    )


def smoke(tmp_path: Path, *script: Scripted) -> tuple[list[str], FakeProvider]:
    fake = FakeProvider(script)
    return run_smoke(make_client(tmp_path, fake)), fake


def test_run_smoke_reports_success_and_masking(tmp_path: Path) -> None:
    lines, fake = smoke(tmp_path, VALID)

    assert lines == [
        f"model: {MODEL}",
        f"result: {VALID.replace(' ', '')}",
        "tokens: in 100 / out 50",
        f"cost_eur: {cost_of(PRICE, 100, 50)}",
        "attempts: 1",
        "provider got placeholders: yes",
        "provider got originals: no",
    ]
    assert "[EMAIL_1]" in fake.requests[0].user_text
    assert "[IBAN_1]" in fake.requests[0].user_text


def test_run_smoke_reports_provider_error(tmp_path: Path) -> None:
    lines, _ = smoke(tmp_path, ProviderError("Gemini refused the request (HTTP 404)"))

    assert "error: ProviderError: Gemini refused the request (HTTP 404)" in lines


def test_run_smoke_hides_unexpected_error_details(tmp_path: Path) -> None:
    lines, _ = smoke(tmp_path, RuntimeError("secret-body"))

    assert "error: RuntimeError" in lines
    assert not any("secret-body" in line for line in lines)


def test_smoke_text_masks_to_both_placeholders() -> None:
    masked = mask(SMOKE_TEXT)

    assert "[EMAIL_1]" in masked
    assert "[IBAN_1]" in masked
    assert SMOKE_EMAIL not in masked
    assert SMOKE_IBAN not in masked
