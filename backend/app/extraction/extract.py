"""Invoice PDF bytes -> ExtractionResult (task 2.5, threat model T1-T4, decision 042).

    bytes -> limits -> read_pdf (hidden text, metadata) -> guards.scan (visible text)
          -> any finding: REJECTED, the model is never called
          -> LLMClient.complete_json (masking, strict schema, no tools, one retry)
          -> invalid answer: FAILED
          -> code checks -> per-field confidence -> EXTRACTED or NEEDS_REVIEW

There is no path or file-name parameter: only what is inside the document counts. Statuses
come from code; nothing here ever approves an invoice. Cost-cap and provider errors are not
caught: the pipeline (2.7) decides whether the run stops.
"""

from dataclasses import dataclass
from decimal import Decimal

from app.config import Settings
from app.extraction.confidence import evaluate
from app.extraction.guards import Finding, scan
from app.extraction.pdf_text import PdfUnreadable, read_pdf
from app.extraction.schema import (
    EXTRACTED,
    FAILED,
    NEEDS_REVIEW,
    REJECTED,
    ExtractionResult,
    InvoiceExtraction,
    LLMUsage,
)
from app.llm.client import InputTooLarge, LLMClient, LLMOutputInvalid, LLMResult

TASK = "invoice_extraction"
PROMPT_ID = "invoice_extraction_v1"


@dataclass(frozen=True)
class ExtractionConfig:
    confidence_threshold: Decimal = Decimal("0.80")
    max_pdf_bytes: int = 5_000_000
    max_pages: int = 5

    @classmethod
    def from_settings(cls, settings: Settings) -> "ExtractionConfig":
        return cls(
            confidence_threshold=settings.extraction_confidence_threshold,
            max_pdf_bytes=settings.extraction_max_pdf_bytes,
            max_pages=settings.extraction_max_pages,
        )


def extract_invoice(
    pdf: bytes,
    client: LLMClient,
    config: ExtractionConfig,
    known_campaign_refs: frozenset[str] | None = None,
) -> ExtractionResult:
    try:
        pdf_text = read_pdf(pdf, config.max_pdf_bytes, config.max_pages)
    except PdfUnreadable:
        return ExtractionResult(status=FAILED, reasons=("pdf:unreadable",))
    if pdf_text.findings:
        return rejected(pdf_text.findings)
    return extract_invoice_text(pdf_text.text, client, config, known_campaign_refs)


def extract_invoice_text(
    text: str,
    client: LLMClient,
    config: ExtractionConfig,
    known_campaign_refs: frozenset[str] | None = None,
) -> ExtractionResult:
    findings = scan(text)
    if findings:
        return rejected(findings)
    try:
        answer = client.complete_json(TASK, PROMPT_ID, as_document(text), InvoiceExtraction)
    except InputTooLarge:
        return ExtractionResult(status=FAILED, reasons=("llm:input_too_large",))
    except LLMOutputInvalid:
        return ExtractionResult(status=FAILED, reasons=("llm:output_invalid",))

    evaluation = evaluate(answer.data, text, known_campaign_refs)
    reasons = list(evaluation.reasons)
    if answer.data.contains_instructions:
        reasons.append("model:contains_instructions")
    if evaluation.confidence < config.confidence_threshold:
        reasons.append("confidence:below_threshold")
    return ExtractionResult(
        status=NEEDS_REVIEW if reasons else EXTRACTED,
        reasons=tuple(reasons),
        document=evaluation.document,
        field_confidence=evaluation.field_confidence,
        confidence=evaluation.confidence,
        usage=usage_of(answer),
    )


def as_document(text: str) -> str:
    """The prompt treats everything between these tags as data (guards reject a spoofed tag)."""
    return f"<document>\n{text}\n</document>"


def rejected(findings: tuple[Finding, ...]) -> ExtractionResult:
    return ExtractionResult(status=REJECTED, reasons=tuple(f.code for f in findings))


def usage_of(answer: LLMResult[InvoiceExtraction]) -> LLMUsage:
    return LLMUsage(
        model=answer.model,
        prompt_id=answer.prompt_id,
        prompt_hash=answer.prompt_hash,
        input_tokens=answer.input_tokens,
        output_tokens=answer.output_tokens,
        cost_eur=answer.cost_eur,
        latency_s=answer.latency_s,
        attempts=answer.attempts,
    )
