"""One live call with invented text, for the owner only (decision 041).

Run by `make llm-smoke`; tests and CI never run it. It prints only the model, the parsed result,
tokens, cost, attempts and whether the provider got placeholders instead of the original PII.
It never prints settings, the key, headers, the URL or the raw response body.
"""

import tempfile
from decimal import Decimal
from pathlib import Path

from app.config import get_settings
from app.llm.client import LLMClient, MissingApiKeyError, create_llm_client, new_run_ledger
from app.llm.provider import LLMError, ModelRequest, ModelResponse, Provider
from pydantic import BaseModel, ConfigDict

SMOKE_IBAN = "LT12 1000 0111 0100 1000"
SMOKE_EMAIL = "billing@vendor.example"
SMOKE_TEXT = f"Invoice SMOKE-001 total 121.00 EUR. Pay to {SMOKE_IBAN}. Contact {SMOKE_EMAIL}."
PROMPT_ID = "smoke_v1"
PROMPT_TEXT = "Return the invoice number and the total in EUR as JSON.\n"
PLACEHOLDERS = ("[EMAIL_1]", "[IBAN_1]")


class SmokeAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    invoice_number: str
    total_eur: str


class RecordingProvider:
    """Wraps the real provider and keeps the user text of every request it forwards."""

    def __init__(self, inner: Provider) -> None:
        self.inner = inner
        self.name = inner.name
        self.user_texts: list[str] = []

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.user_texts.append(request.user_text)
        return self.inner.generate(request)


def yes_no(value: bool) -> str:
    return "yes" if value else "no"


def got_placeholders(texts: list[str]) -> bool:
    return bool(texts) and all(all(p in text for p in PLACEHOLDERS) for text in texts)


def got_originals(texts: list[str]) -> bool:
    return any(SMOKE_IBAN in text or SMOKE_EMAIL in text for text in texts)


def run_smoke(client: LLMClient) -> list[str]:
    recorder = RecordingProvider(client.provider)
    client.provider = recorder
    lines = [f"model: {client.limits.model}"]
    try:
        result = client.complete_json("llm_smoke", PROMPT_ID, SMOKE_TEXT, SmokeAnswer)
    except LLMError as exc:
        return [*lines, f"error: {type(exc).__name__}: {exc}"]
    except Exception as exc:  # anything else may carry a raw body, so only its class is shown
        return [*lines, f"error: {type(exc).__name__}"]
    return [
        *lines,
        f"result: {result.data.model_dump_json()}",
        f"tokens: in {result.input_tokens} / out {result.output_tokens}",
        f"cost_eur: {result.cost_eur}",
        f"attempts: {result.attempts}",
        f"provider got placeholders: {yes_no(got_placeholders(recorder.user_texts))}",
        f"provider got originals: {yes_no(got_originals(recorder.user_texts))}",
    ]


def main() -> int:
    settings = get_settings()
    with tempfile.TemporaryDirectory() as tmp:
        prompts_dir = Path(tmp)
        (prompts_dir / f"{PROMPT_ID}.md").write_text(PROMPT_TEXT, encoding="utf-8")
        try:
            client = create_llm_client(settings, new_run_ledger(settings, Decimal(0)))
        except MissingApiKeyError as exc:
            print(f"error: {type(exc).__name__}: {exc}")
            return 1
        client.prompts_dir = prompts_dir
        lines = run_smoke(client)
    for line in lines:
        print(line)
    return 1 if any(line.startswith("error:") for line in lines) else 0


if __name__ == "__main__":
    raise SystemExit(main())
