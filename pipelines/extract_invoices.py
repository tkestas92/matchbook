"""Pipeline step 5, llm source: invoice PDFs through extraction into `core.*` (decision 045).

    discover PDFs (invoices/, credit_notes/, inbox/; never a .json)
      -> sha256 already loaded or reviewed: skipped, no model call
      -> extract_invoice (decision 042) outside any transaction
      -> extracted: written to core with the ground-truth writers (status `extracted`)
      -> needs_review / rejected / failed: never in core, one `app.issues` card per file

Only `extracted` documents reach `core`, so nothing a human has not seen is reconciled. A
document is routed by what extraction says it is, not by its folder. The cost cap or a provider
error stops extraction; the remaining files are counted as skipped and are tried next run.
"""

import hashlib
import re
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.db.models.app import Issue
from app.db.models.core import CreditNote, InsertionOrder, Invoice
from app.extraction.extract import ExtractionConfig, extract_invoice
from app.extraction.schema import (
    EXTRACTED,
    FAILED,
    NEEDS_REVIEW,
    REJECTED,
    ExtractedDocument,
    ExtractionResult,
)
from app.extraction.vendors import KnownVendors
from app.llm.client import LLMClient
from app.llm.cost import CostCapExceeded
from app.llm.provider import ProviderError
from sqlalchemy import Connection, Engine, select

from pipelines.load_invoices import (
    CREDIT_NOTES_DIR,
    INVOICES_DIR,
    KIND_PLATFORM,
    CreditNoteDocument,
    DocumentKey,
    InvoiceDocument,
    InvoiceLineDocument,
    LoadInvoicesResult,
    Registry,
    remove_stale_lines,
    write_credit_notes,
    write_invoices,
    write_lines,
)
from pipelines.load_plans import client_ids, platform_ids, require_directory
from pipelines.normalize import RowError, upsert
from pipelines.reconcile import ISSUE_COLUMNS

INBOX_DIR = "inbox"
PDF_FOLDERS = (INVOICES_DIR, CREDIT_NOTES_DIR, INBOX_DIR)
INVOICE_TYPE = "invoice"
CREDIT_NOTE_TYPE = "credit_note"
ISSUE_KEY_PREFIX = "extraction:"
ISSUE_TYPES = {
    NEEDS_REVIEW: ("extraction_needs_review", "medium"),
    REJECTED: ("extraction_rejected_suspicious", "high"),
    FAILED: ("extraction_failed", "medium"),
}
PROPOSED_ACTION = "Check the PDF and enter it by hand; nothing was loaded."
# A file name is external input: only a plain relative path is stored as the issue's entity.
SAFE_PATH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
SKIPPED_COST_CAP = "skipped_cost_cap"
SKIPPED_PROVIDER_ERROR = "skipped_provider_error"

UNKNOWN_CLIENT = "load:unknown_client"
UNKNOWN_PLATFORM = "load:unknown_platform"
MISSING_FIELD = "load:missing_field"
UNKNOWN_ORIGINAL = "load:unknown_original"
DUPLICATE_NUMBER = "load:duplicate_number"
UNKNOWN_TYPE = "load:unknown_type"


@dataclass(frozen=True)
class SourceFile:
    relative: str  # POSIX path relative to the data directory, as found on disk.
    sha256: str
    pdf: bytes


@dataclass(frozen=True)
class ReviewItem:
    entity_id: str
    sha256: str
    status: str
    reasons: tuple[str, ...]
    client_id: int | None = None
    platform_id: int | None = None
    period: date | None = None


@dataclass
class LoadExtractedResult(LoadInvoicesResult):
    documents_seen: int = 0
    skipped_unchanged: int = 0
    skipped_cost_cap: int = 0
    skipped_provider_error: int = 0
    statuses: Counter[str] = field(default_factory=Counter)
    review: list[ReviewItem] = field(default_factory=list)
    llm_cost_eur: Decimal = Decimal(0)
    llm_tokens: int = 0
    llm_calls: int = 0


def known_vendors_from_config(path: Path) -> KnownVendors:
    """Platform names and the agency from the generator config (decision 044).

    Imported here: the Airflow worker does not mount `generator/`, and the ground-truth source
    must keep working there (decision 045).
    """
    from generator.documents.config import load_documents_config
    from generator.world import ConfigError, load_config

    try:
        platforms = load_config(path).platforms
        agency = load_documents_config(path).agency
        return KnownVendors.of([*(p.name for p in platforms), agency.name])
    except (OSError, KeyError, TypeError, ValueError, ConfigError) as exc:
        raise RuntimeError(f"{path}: cannot read known vendors ({type(exc).__name__})") from exc


def known_campaign_refs(conn: Connection) -> frozenset[str]:
    return frozenset(conn.scalars(select(InsertionOrder.campaign_ref).distinct()))


def loaded_hashes(conn: Connection) -> set[str]:
    hashes: set[str] = set()
    for column in (Invoice.file_sha256, CreditNote.file_sha256):
        hashes.update(conn.scalars(select(column).where(column.is_not(None))))
    return hashes


def reviewed_hashes(conn: Connection) -> set[str]:
    """Files that already have an extraction issue, whatever its status (decision 045)."""
    keys = conn.scalars(select(Issue.dedup_key).where(Issue.dedup_key.startswith(ISSUE_KEY_PREFIX)))
    return {key.removeprefix(ISSUE_KEY_PREFIX) for key in keys}


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def discover_pdfs(data_dir: Path) -> list[Path]:
    """Every `.pdf` in the document folders, in path order; nothing else is ever opened."""
    paths = (
        path
        for folder in PDF_FOLDERS
        if (data_dir / folder).is_dir()
        for path in (data_dir / folder).rglob("*.pdf")
        if path.is_file()
    )
    return sorted(paths)


def safe_entity_id(relative: str, sha256: str) -> str:
    """The relative path if it is plain ASCII and safe to show, else a placeholder."""
    plain = (
        SAFE_PATH.fullmatch(relative) is not None
        and relative.endswith(".pdf")
        and ".." not in relative.split("/")
        and "//" not in relative
    )
    return relative if plain else f"document:{sha256}"


def line_confidence(field_confidence: Mapping[str, Decimal], index: int) -> Decimal | None:
    prefix = f"lines[{index}]."
    scores = [score for name, score in field_confidence.items() if name.startswith(prefix)]
    return min(scores) if scores else None


def required[T](value: T | None) -> T:
    if value is None:
        raise RowError(MISSING_FIELD)
    return value


def to_lines(
    document: ExtractedDocument, extraction: ExtractionResult
) -> tuple[InvoiceLineDocument, ...]:
    confidence = extraction.field_confidence or {}
    return tuple(
        InvoiceLineDocument(
            line_no=line.line_no,
            campaign_ref=line.campaign_ref,
            description=line.description,
            amount=line.amount,
            extracted_confidence=line_confidence(confidence, index),
        )
        for index, line in enumerate(document.lines)
    )


def to_invoice_document(
    source: SourceFile, extraction: ExtractionResult, registry: Registry
) -> InvoiceDocument:
    """Raises `RowError` with a fixed reason code; no document text goes into it."""
    document = required(extraction.document)
    kind = required(document.kind)
    client_id = registry.clients.get(required(document.client_code))
    if client_id is None:
        raise RowError(UNKNOWN_CLIENT)
    platform_id = None
    if kind == KIND_PLATFORM:
        platform_id = registry.platforms.get(required(document.platform))
        if platform_id is None:
            raise RowError(UNKNOWN_PLATFORM)
    if not document.lines:
        raise RowError(MISSING_FIELD)
    return InvoiceDocument(
        vendor=document.vendor,
        number=document.number,
        issue_date=required(document.issue_date),
        file_path=source.relative,
        file_sha256=source.sha256,
        kind=kind,
        client_id=client_id,
        platform_id=platform_id,
        period_start=required(document.period_start),
        period_end=required(document.period_end),
        subtotal=document.subtotal,
        vat=document.vat,
        total=document.total,
        lines=to_lines(document, extraction),
    )


def to_credit_note_document(source: SourceFile, extraction: ExtractionResult) -> CreditNoteDocument:
    """`amount` is the note's net subtotal as printed, as in decision 029."""
    document = required(extraction.document)
    return CreditNoteDocument(
        vendor=document.vendor,
        number=document.number,
        issue_date=required(document.issue_date),
        file_path=source.relative,
        file_sha256=source.sha256,
        original_number=required(document.original_number),
        amount=document.subtotal,
        reason=required(document.reason),
    )


def review_item(
    source: SourceFile,
    status: str,
    reasons: Sequence[str],
    document: ExtractedDocument | None = None,
    registry: Registry | None = None,
) -> ReviewItem:
    client_id = platform_id = period = None
    if document is not None and registry is not None:
        client_id = registry.clients.get(document.client_code or "")
        platform_id = registry.platforms.get(document.platform or "")
        period = document.period_start.replace(day=1) if document.period_start else None
    return ReviewItem(
        entity_id=safe_entity_id(source.relative, source.sha256),
        sha256=source.sha256,
        status=status,
        reasons=tuple(reasons),
        client_id=client_id,
        platform_id=platform_id,
        period=period,
    )


def review_issue_row(run_id: uuid.UUID, item: ReviewItem) -> dict[str, Any]:
    issue_type, severity = ISSUE_TYPES[item.status]
    reasons = ", ".join(item.reasons) or "none"
    return {
        "dedup_key": f"{ISSUE_KEY_PREFIX}{item.sha256}",
        "type": issue_type,
        "severity": severity,
        "client_id": item.client_id,
        "platform_id": item.platform_id,
        "period": item.period,
        "entity_type": "document",
        "entity_id": item.entity_id,
        "amount": None,
        "explanation": (
            f"Invoice extraction ended as {item.status} (reasons: {reasons}). "
            "The document was not loaded and is not reconciled."
        ),
        "proposed_action": PROPOSED_ACTION,
        "run_id": run_id,
    }


def write_review_issues(engine: Engine, run_id: uuid.UUID, items: Sequence[ReviewItem]) -> None:
    """Upsert as `app`; status and decision columns are never updated."""
    rows = [review_issue_row(run_id, item) for item in items]
    with engine.begin() as conn:
        upsert(conn, Issue, rows, ("dedup_key",), (*ISSUE_COLUMNS, "entity_id"))


def review_list(items: Sequence[ReviewItem]) -> list[dict[str, Any]]:
    return [
        {"document": item.entity_id, "status": item.status, "reasons": list(item.reasons)}
        for item in items
    ]


def read_sources(data_dir: Path, skip: set[str], result: LoadExtractedResult) -> list[SourceFile]:
    """New files only: a known or repeated sha256 is counted and skipped."""
    sources = []
    for path in discover_pdfs(data_dir):
        result.documents_seen += 1
        pdf = path.read_bytes()
        sha = sha256_of(pdf)
        if sha in skip:
            result.skipped_unchanged += 1
            continue
        skip.add(sha)
        sources.append(SourceFile(path.relative_to(data_dir).as_posix(), sha, pdf))
    return sources


def extract_sources(
    result: LoadExtractedResult,
    sources: Sequence[SourceFile],
    client: LLMClient,
    config: ExtractionConfig,
    refs: frozenset[str],
    known_vendors: KnownVendors,
) -> list[tuple[SourceFile, ExtractionResult]]:
    """Extract until the cost cap or a provider error; the rest wait for the next run."""
    done: list[tuple[SourceFile, ExtractionResult]] = []
    for index, source in enumerate(sources):
        try:
            extraction = extract_invoice(
                source.pdf, client, config, refs, known_vendors=known_vendors
            )
        except CostCapExceeded:
            result.skipped_cost_cap = len(sources) - index
            break
        except ProviderError:
            result.skipped_provider_error = len(sources) - index
            break
        done.append((source, extraction))
    return done


@dataclass
class Routed:
    invoices: dict[DocumentKey, InvoiceDocument] = field(default_factory=dict)
    notes: dict[DocumentKey, CreditNoteDocument] = field(default_factory=dict)
    sources: dict[DocumentKey, tuple[SourceFile, ExtractionResult]] = field(default_factory=dict)


def route(
    result: LoadExtractedResult,
    extracted: Sequence[tuple[SourceFile, ExtractionResult]],
    registry: Registry,
) -> Routed:
    """Extracted documents by type; everything else, and anything that cannot load, to review."""
    routed = Routed()
    for source, extraction in extracted:
        document = extraction.document
        if extraction.status != EXTRACTED:
            item = review_item(source, extraction.status, extraction.reasons, document, registry)
            result.review.append(item)
            continue
        try:
            add_document(routed, source, extraction, registry)
        except RowError as error:
            item = review_item(source, NEEDS_REVIEW, (str(error),), document, registry)
            result.review.append(item)
    return routed


def add_document(
    routed: Routed, source: SourceFile, extraction: ExtractionResult, registry: Registry
) -> None:
    """The first file in path order keeps a repeated vendor and number, as in decision 029."""
    document_type = required(extraction.document).type
    target: dict[DocumentKey, Any]
    parsed: InvoiceDocument | CreditNoteDocument
    if document_type == INVOICE_TYPE:
        parsed, target = to_invoice_document(source, extraction, registry), routed.invoices
    elif document_type == CREDIT_NOTE_TYPE:
        parsed, target = to_credit_note_document(source, extraction), routed.notes
    else:
        raise RowError(UNKNOWN_TYPE)
    if parsed.key in routed.invoices or parsed.key in routed.notes:
        raise RowError(DUPLICATE_NUMBER)
    target[parsed.key] = parsed
    routed.sources[parsed.key] = (source, extraction)


def write_documents(
    conn: Connection, result: LoadExtractedResult, routed: Routed, registry: Registry
) -> None:
    invoices = list(routed.invoices.values())
    ids = write_invoices(conn, result.run_id, invoices, extracted=True)
    result.invoices_written = len(invoices)
    result.lines_written = write_lines(conn, result.run_id, invoices, ids, extracted=True)
    result.lines_removed = remove_stale_lines(conn, invoices, ids)
    notes = []
    for note in routed.notes.values():
        if (note.vendor, note.original_number) in ids:
            notes.append(note)
            continue
        source, extraction = routed.sources[note.key]
        result.review.append(
            review_item(source, NEEDS_REVIEW, (UNKNOWN_ORIGINAL,), extraction.document, registry)
        )
    result.credit_notes_written = write_credit_notes(conn, result, notes, ids, extracted=True)


def load_invoices_llm(
    run_id: uuid.UUID,
    data_dir: Path,
    etl: Engine,
    app: Engine,
    client: LLMClient,
    config: ExtractionConfig,
    known_vendors: KnownVendors,
) -> LoadExtractedResult:
    """Read, extract outside any transaction, then write `core` (etl) and the issues (app)."""
    require_directory(data_dir / INVOICES_DIR)
    result = LoadExtractedResult(run_id=run_id)
    with etl.connect() as conn:
        registry = Registry(client_ids(conn), platform_ids(conn))
        refs = known_campaign_refs(conn)
        known = loaded_hashes(conn)
    with app.connect() as conn:
        known |= reviewed_hashes(conn)

    sources = read_sources(data_dir, known, result)
    ledger = client.ledger
    cost_before, tokens_before, calls_before = ledger.total, ledger.tokens, ledger.calls
    try:
        extracted = extract_sources(result, sources, client, config, refs, known_vendors)
    finally:
        result.llm_cost_eur = ledger.total - cost_before
        result.llm_tokens = ledger.tokens - tokens_before
        result.llm_calls = ledger.calls - calls_before

    routed = route(result, extracted, registry)
    with etl.begin() as conn:
        write_documents(conn, result, routed, registry)
    write_review_issues(app, run_id, result.review)
    result.statuses = Counter(
        {EXTRACTED: result.invoices_written + result.credit_notes_written}
    ) + Counter(item.status for item in result.review)
    return result
