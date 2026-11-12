"""The extractor end to end with the fake model (task 2.5, threat model T1-T4).

No database, no network, no key: every model answer is scripted.
"""

import inspect
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from app.config import Settings
from app.extraction.extract import ExtractionConfig, extract_invoice, extract_invoice_text
from app.extraction.vendors import KnownVendors
from app.llm.provider import ModelResponse
from tests.extraction_samples import (
    AGENCY_ANSWER,
    AGENCY_TEXT,
    CONFIG,
    CREDIT_NOTE_ANSWER,
    CREDIT_NOTE_TEXT,
    EMAIL,
    IBAN,
    KNOWN_VENDORS,
    META_ANSWER,
    META_TEXT,
    PHONE,
    SAMPLES,
    TIKTOK_ANSWER,
    TIKTOK_TEXT,
    as_json,
    hand_built_invoice,
    make_client,
)

from generator.documents.injection import INJECTION_VARIANTS, PAYLOADS
from generator.documents.pdf import render_invoice


@pytest.mark.parametrize("layout", sorted(SAMPLES))
def test_benign_text_is_extracted(layout: str) -> None:
    text, answer = SAMPLES[layout]
    client, fake = make_client([as_json(answer)])
    result = extract_invoice_text(text, client, CONFIG, known_vendors=KNOWN_VENDORS)
    assert result.status == "extracted", result.reasons
    assert result.reasons == ()
    assert result.confidence == Decimal("0.95")
    assert result.usage is not None and result.usage.prompt_id == "invoice_extraction_v1"
    assert len(fake.requests) == 1


def test_credit_note_is_extracted_with_its_fields() -> None:
    client, _ = make_client([as_json(CREDIT_NOTE_ANSWER)])
    document = extract_invoice_text(
        CREDIT_NOTE_TEXT, client, CONFIG, known_vendors=KNOWN_VENDORS
    ).document
    assert document is not None
    assert (document.type, document.original_number, document.campaign_ref) == (
        "credit_note",
        "FBADS-456789-987654321",
        "SAU-META-2605",
    )
    assert document.lines == () and document.subtotal == Decimal("100.00")


def test_benign_pdf_is_extracted(tmp_path: Path) -> None:
    path = tmp_path / "google.pdf"
    render_invoice(hand_built_invoice("google"), path, "LT100000000000")
    answer = {
        **META_ANSWER,
        "vendor": "Google",
        "number": "1234567890",
        "account_id": "100-000-123456789",
        "issue_date": "2026-06-01",
        "lines": [
            {
                "campaign_ref": "SAU-GOOGLE-2605",
                "description": "Campaign SAU-GOOGLE-2605",
                "amount": "1234.56",
            }
        ],
        "subtotal": "1234.56",
        "vat": "259.26",
        "total": "1493.82",
    }
    client, fake = make_client([as_json(answer)])
    result = extract_invoice(path.read_bytes(), client, CONFIG, known_vendors=KNOWN_VENDORS)
    assert result.status == "extracted", result.reasons
    assert len(fake.requests) == 1


# --- injection: rejected before the model (T1) --------------------------------------------


@pytest.mark.parametrize("variant", sorted(PAYLOADS))
def test_every_payload_in_text_is_rejected_without_a_model_call(variant: str) -> None:
    client, fake = make_client([])
    text = META_TEXT + "\n" + "\n".join(PAYLOADS[variant])
    result = extract_invoice_text(text, client, CONFIG, known_vendors=KNOWN_VENDORS)
    assert result.status == "rejected"
    assert result.document is None
    assert len(fake.requests) == 0


@pytest.mark.parametrize("variant", sorted(INJECTION_VARIANTS))
def test_every_variant_in_a_pdf_is_rejected_without_a_model_call(
    tmp_path: Path, variant: str
) -> None:
    path = tmp_path / f"{variant}.pdf"
    render_invoice(hand_built_invoice("meta"), path, "LT100000000000", INJECTION_VARIANTS[variant])
    client, fake = make_client([])
    result = extract_invoice(path.read_bytes(), client, CONFIG, known_vendors=KNOWN_VENDORS)
    assert result.status == "rejected", result.reasons
    assert len(fake.requests) == 0


def test_rejection_reasons_are_codes_not_document_text() -> None:
    client, _ = make_client([])
    text = META_TEXT + "\n" + "\n".join(PAYLOADS["change_total"])
    for reason in extract_invoice_text(text, client, CONFIG, known_vendors=KNOWN_VENDORS).reasons:
        assert reason.startswith("content:") and "EUR 1.00" not in reason


# --- grounding and review (T3) --------------------------------------------------------------


def test_a_changed_total_goes_to_review() -> None:
    client, _ = make_client([as_json(META_ANSWER, total="1.00")])
    result = extract_invoice_text(META_TEXT, client, CONFIG, known_vendors=KNOWN_VENDORS)
    assert result.status == "needs_review"
    assert "grounding:total" in result.reasons
    assert result.field_confidence is not None and result.field_confidence["total"] == 0


def test_wrong_arithmetic_on_the_invoice_goes_to_review() -> None:
    lines = [dict(line) for line in AGENCY_ANSWER["lines"]]
    lines[2]["amount"] = "80.00"
    text = AGENCY_TEXT.replace("on media 90.00 €", "on media 80.00 €")
    client, _ = make_client([as_json(AGENCY_ANSWER, lines=lines)])
    result = extract_invoice_text(text, client, CONFIG, known_vendors=KNOWN_VENDORS)
    assert result.status == "needs_review"
    assert "arithmetic:subtotal" in result.reasons


def test_low_model_score_goes_to_review() -> None:
    client, _ = make_client([as_json(META_ANSWER, model_confidence="0.5")])
    result = extract_invoice_text(META_TEXT, client, CONFIG, known_vendors=KNOWN_VENDORS)
    assert result.status == "needs_review"
    assert result.reasons == ("confidence:below_threshold",)


def test_threshold_comes_from_config() -> None:
    client, _ = make_client([as_json(META_ANSWER, model_confidence="0.5")])
    config = ExtractionConfig(confidence_threshold=Decimal("0.5"))
    result = extract_invoice_text(META_TEXT, client, config, known_vendors=KNOWN_VENDORS)
    assert result.status == "extracted"


def test_model_flagging_instructions_goes_to_review() -> None:
    client, _ = make_client([as_json(META_ANSWER, contains_instructions=True)])
    result = extract_invoice_text(META_TEXT, client, CONFIG, known_vendors=KNOWN_VENDORS)
    assert result.status == "needs_review"
    assert "model:contains_instructions" in result.reasons


# --- invalid answers (T3) -------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "not json",
        as_json(META_ANSWER)[:-3],
        as_json(META_ANSWER, status="approved"),
        as_json(META_ANSWER, total=1210.0),
    ],
)
def test_invalid_answers_fail_after_one_retry(bad: str) -> None:
    client, fake = make_client([bad, bad])
    result = extract_invoice_text(META_TEXT, client, CONFIG, known_vendors=KNOWN_VENDORS)
    assert result.status == "failed"
    assert result.reasons == ("llm:output_invalid",)
    assert result.document is None
    assert len(fake.requests) == 2


def test_truncated_answer_fails() -> None:
    cut = ModelResponse(
        text=as_json(META_ANSWER), input_tokens=1, output_tokens=1, finish_complete=False
    )
    client, _ = make_client([cut, cut])
    result = extract_invoice_text(META_TEXT, client, CONFIG, known_vendors=KNOWN_VENDORS)
    assert result.status == "failed"


def test_unreadable_pdf_fails_without_a_model_call() -> None:
    client, fake = make_client([])
    result = extract_invoice(b"not a pdf", client, CONFIG, known_vendors=KNOWN_VENDORS)
    assert (result.status, result.reasons) == ("failed", ("pdf:unreadable",))
    assert len(fake.requests) == 0


# --- what the provider receives (T2, T4) ----------------------------------------------------


def test_the_provider_receives_masked_text_inside_document_tags() -> None:
    client, fake = make_client([as_json(META_ANSWER)])
    extract_invoice_text(META_TEXT, client, CONFIG, known_vendors=KNOWN_VENDORS)
    request = fake.requests[0]
    for original in (IBAN, EMAIL, PHONE):
        assert original not in request.user_text
    assert {"[IBAN_1]", "[EMAIL_1]", "[PHONE_1]"} <= set(request.user_text.split())
    assert request.user_text.startswith("<document>\n")
    assert request.user_text.endswith("\n</document>")
    assert "FBADS-456789-987654321" in request.user_text and "€1,210.00" in request.user_text
    assert "<document>" in request.system


def test_the_request_has_no_tools_and_uses_the_strict_schema() -> None:
    client, fake = make_client([as_json(META_ANSWER)])
    extract_invoice_text(META_TEXT, client, CONFIG, known_vendors=KNOWN_VENDORS)
    request = fake.requests[0]
    assert not hasattr(request, "tools")
    assert request.json_schema["additionalProperties"] is False
    assert "tools" not in request.json_schema.get("properties", {})


def test_no_path_parameter_anywhere() -> None:
    for function in (extract_invoice, extract_invoice_text):
        names = set(inspect.signature(function).parameters)
        assert not names & {"path", "file", "filename", "file_path", "name"}


def test_config_defaults_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    for name in ("EXTRACTION_MAX_PDF_BYTES", "EXTRACTION_MAX_PAGES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EXTRACTION_CONFIDENCE_THRESHOLD", "0.9")
    config = ExtractionConfig.from_settings(Settings(_env_file=None))
    assert config == ExtractionConfig(
        confidence_threshold=Decimal("0.9"), max_pdf_bytes=5_000_000, max_pages=5
    )


def test_document_dates_are_dates() -> None:
    client, _ = make_client([as_json(META_ANSWER)])
    document = extract_invoice_text(META_TEXT, client, CONFIG, known_vendors=KNOWN_VENDORS).document
    assert document is not None and document.issue_date == date(2026, 6, 1)


# --- vendor resolution (decision 044) -------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "answer", "printed", "canonical"),
    [
        (META_TEXT, META_ANSWER, "Meta Ads", "Meta"),
        (TIKTOK_TEXT, TIKTOK_ANSWER, "TikTok For Business", "TikTok"),
    ],
)
def test_printed_heading_is_extracted_as_the_canonical_vendor(
    text: str, answer: dict[str, object], printed: str, canonical: str
) -> None:
    client, _ = make_client([as_json(answer, vendor=printed)])
    result = extract_invoice_text(text, client, CONFIG, known_vendors=KNOWN_VENDORS)
    assert result.status == "extracted", result.reasons
    assert result.document is not None and result.document.vendor == canonical


def test_unknown_vendor_goes_to_review_with_a_fixed_reason() -> None:
    text = META_TEXT.replace("Meta Ads", "Acme Media Ads")
    client, _ = make_client([as_json(META_ANSWER, vendor="Acme Media")])
    result = extract_invoice_text(text, client, CONFIG, known_vendors=KNOWN_VENDORS)
    assert result.status == "needs_review"
    assert "unknown:vendor" in result.reasons
    assert not any("acme" in reason.casefold() for reason in result.reasons)


def test_empty_known_vendors_never_extracts() -> None:
    client, _ = make_client([as_json(META_ANSWER)])
    result = extract_invoice_text(META_TEXT, client, CONFIG, known_vendors=KnownVendors.of(()))
    assert result.status == "needs_review"
    assert "unknown:vendor" in result.reasons


def test_known_vendors_are_required() -> None:
    for function in (extract_invoice, extract_invoice_text):
        parameter = inspect.signature(function).parameters["known_vendors"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty
