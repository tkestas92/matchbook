"""Pipeline step 5: load invoices and credit notes into `core.*`, exactly as the documents say.

Stage 1 reads the JSON next to each PDF; stage 2 replaces that with extraction (decision 029).
"""

import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path, PurePath
from typing import Any

from app.db.models.core import CreditNote, Invoice, InvoiceLine
from app.db.session import create_etl_engine
from sqlalchemy import Connection, Engine, delete, select

from pipelines.load_plans import (
    DEFAULT_DATA_DIR,
    RejectedDocument,
    client_ids,
    platform_ids,
    require_directory,
)
from pipelines.normalize import (
    DECIMAL_STRING,
    RowError,
    parse_date,
    parse_spend,
    parse_text,
    upsert,
)

INVOICES_DIR = "invoices"
CREDIT_NOTES_DIR = "credit_notes"
ISO_DATE = "%Y-%m-%d"
CURRENCY = "EUR"
KIND_PLATFORM = "platform"
KIND_CLIENT = "client"
DOCUMENT_KEY = ("vendor", "number")
DUPLICATE_NUMBER = "Another document has the same vendor and number"

DocumentKey = tuple[str, str]


@dataclass(frozen=True)
class Document:
    vendor: str
    number: str
    issue_date: date
    file_path: str

    @property
    def key(self) -> DocumentKey:
        return self.vendor, self.number


@dataclass(frozen=True)
class InvoiceLineDocument:
    line_no: int
    campaign_ref: str | None
    description: str
    amount: Decimal


@dataclass(frozen=True)
class InvoiceDocument(Document):
    kind: str
    client_id: int
    platform_id: int | None
    period_start: date
    period_end: date
    subtotal: Decimal
    vat: Decimal
    total: Decimal
    lines: tuple[InvoiceLineDocument, ...]


@dataclass(frozen=True)
class CreditNoteDocument(Document):
    original_number: str
    amount: Decimal  # Net of VAT and positive, as printed on the note.
    reason: str


@dataclass(frozen=True)
class Registry:
    clients: dict[str, int]
    platforms: dict[str, int]


@dataclass
class LoadInvoicesResult:
    run_id: uuid.UUID
    invoices_written: int = 0
    lines_written: int = 0
    lines_removed: int = 0
    credit_notes_written: int = 0
    rejected: list[RejectedDocument] = field(default_factory=list)


def parse_money(value: Any, name: str) -> Decimal:
    amount = parse_spend(value, name, DECIMAL_STRING)
    if amount is None:
        raise RowError(f"{name} is missing")
    return amount


def parse_iso_date(value: Any, name: str) -> date:
    return parse_date(value, name, ISO_DATE)


def require_document(payload: dict[str, Any], expected_type: str) -> None:
    if payload.get("type") != expected_type:
        raise RowError(f"Document type is not {expected_type}")
    if payload.get("currency") != CURRENCY:
        raise RowError(f"Currency is not {CURRENCY}")


def resolve_client(code: Any, registry: Registry) -> int:
    client_id = registry.clients.get(parse_text(code, "client_code"))
    if client_id is None:
        raise RowError(f"Unknown client code {code!r}")
    return client_id


def resolve_platform(kind: str, name: Any, registry: Registry) -> int | None:
    if kind == KIND_CLIENT:
        return None
    platform_id = registry.platforms.get(parse_text(name, "platform"))
    if platform_id is None:
        raise RowError(f"Unknown platform {name!r}")
    return platform_id


def parse_line(raw: Any) -> InvoiceLineDocument:
    if not isinstance(raw, dict):
        raise RowError("Invoice line is not an object")
    line_no = raw.get("line_no")
    if not isinstance(line_no, int) or isinstance(line_no, bool):
        raise RowError("line_no is not an integer")
    campaign_ref = raw.get("campaign_ref")
    return InvoiceLineDocument(
        line_no=line_no,
        campaign_ref=None if campaign_ref is None else parse_text(campaign_ref, "campaign_ref"),
        description=parse_text(raw.get("description"), "description"),
        amount=parse_money(raw.get("amount"), "amount"),
    )


def parse_lines(raw: Any) -> tuple[InvoiceLineDocument, ...]:
    if not isinstance(raw, list) or not raw:
        raise RowError("lines is not a non-empty list")
    lines = tuple(parse_line(item) for item in raw)
    if len({line.line_no for line in lines}) != len(lines):
        raise RowError("Invoice has a repeated line_no")
    return lines


def pdf_path(relative: PurePath) -> str:
    return relative.with_suffix(".pdf").as_posix()


def parse_invoice(
    payload: dict[str, Any], relative: PurePath, registry: Registry
) -> InvoiceDocument:
    require_document(payload, "invoice")
    kind = payload.get("kind")
    if kind not in (KIND_PLATFORM, KIND_CLIENT):
        raise RowError("kind is not platform or client")
    return InvoiceDocument(
        vendor=parse_text(payload.get("vendor"), "vendor"),
        number=parse_text(payload.get("number"), "number"),
        issue_date=parse_iso_date(payload.get("issue_date"), "issue_date"),
        file_path=pdf_path(relative),
        kind=kind,
        client_id=resolve_client(payload.get("client_code"), registry),
        platform_id=resolve_platform(kind, payload.get("platform"), registry),
        period_start=parse_iso_date(payload.get("period_start"), "period_start"),
        period_end=parse_iso_date(payload.get("period_end"), "period_end"),
        subtotal=parse_money(payload.get("subtotal"), "subtotal"),
        vat=parse_money(payload.get("vat"), "vat"),
        total=parse_money(payload.get("total"), "total"),
        lines=parse_lines(payload.get("lines")),
    )


def parse_credit_note(payload: dict[str, Any], relative: PurePath) -> CreditNoteDocument:
    require_document(payload, "credit_note")
    return CreditNoteDocument(
        vendor=parse_text(payload.get("vendor"), "vendor"),
        number=parse_text(payload.get("number"), "number"),
        issue_date=parse_iso_date(payload.get("issue_date"), "issue_date"),
        file_path=pdf_path(relative),
        original_number=parse_text(payload.get("original_number"), "original_number"),
        amount=parse_money(payload.get("subtotal"), "subtotal"),
        reason=parse_text(payload.get("reason"), "reason"),
    )


def read_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RowError("File is not readable JSON") from error
    if not isinstance(payload, dict):
        raise RowError("File is not a JSON object")
    return payload


def collect[D: Document](
    result: LoadInvoicesResult,
    data_dir: Path,
    subdir: str,
    parse: Callable[[dict[str, Any], PurePath], D],
) -> list[D]:
    """Parse one folder; the first file in path order keeps a repeated vendor and number."""
    kept: dict[DocumentKey, D] = {}
    for path in sorted((data_dir / subdir).rglob("*.json")):
        relative = path.relative_to(data_dir)
        try:
            document = parse(read_payload(path), relative)
            if document.key in kept:
                raise RowError(DUPLICATE_NUMBER)
        except RowError as error:
            result.rejected.append(RejectedDocument(relative.as_posix(), str(error)))
            continue
        kept[document.key] = document
    return list(kept.values())


def write_invoices(
    conn: Connection, run_id: uuid.UUID, invoices: Sequence[InvoiceDocument]
) -> dict[DocumentKey, int]:
    """Upsert invoices and return every invoice id; `status` is outside the upsert."""
    columns = (
        "kind",
        "client_id",
        "platform_id",
        "issue_date",
        "period_start",
        "period_end",
        "subtotal",
        "vat",
        "total",
        "file_path",
    )
    rows = [
        {
            "vendor": invoice.vendor,
            "number": invoice.number,
            "run_id": run_id,
            **{column: getattr(invoice, column) for column in columns},
        }
        for invoice in invoices
    ]
    upsert(conn, Invoice, rows, DOCUMENT_KEY, (*columns, "run_id"))
    query = select(Invoice.vendor, Invoice.number, Invoice.id)
    return {(row.vendor, row.number): row.id for row in conn.execute(query)}


def write_lines(
    conn: Connection,
    run_id: uuid.UUID,
    invoices: Sequence[InvoiceDocument],
    ids: dict[DocumentKey, int],
) -> int:
    """`campaign_id` and `extracted_confidence` belong to later steps and stay untouched."""
    rows = [
        {
            "invoice_id": ids[invoice.key],
            "line_no": line.line_no,
            "campaign_ref": line.campaign_ref,
            "description": line.description,
            "amount": line.amount,
            "run_id": run_id,
        }
        for invoice in invoices
        for line in invoice.lines
    ]
    upsert(
        conn,
        InvoiceLine,
        rows,
        ("invoice_id", "line_no"),
        ("campaign_ref", "description", "amount", "run_id"),
    )
    return len(rows)


def remove_stale_lines(
    conn: Connection, invoices: Sequence[InvoiceDocument], ids: dict[DocumentKey, int]
) -> int:
    """Drop lines a re-loaded invoice no longer has, so its lines match the document."""
    removed = 0
    for invoice in invoices:
        statement = delete(InvoiceLine).where(
            InvoiceLine.invoice_id == ids[invoice.key],
            InvoiceLine.line_no.not_in([line.line_no for line in invoice.lines]),
        )
        removed += conn.execute(statement).rowcount
    return removed


def write_credit_notes(
    conn: Connection,
    result: LoadInvoicesResult,
    notes: Sequence[CreditNoteDocument],
    ids: dict[DocumentKey, int],
) -> int:
    rows = []
    for note in notes:
        original_id = ids.get((note.vendor, note.original_number))
        if original_id is None:
            reason = f"Unknown original invoice {note.original_number!r}"
            result.rejected.append(RejectedDocument(json_path(note), reason))
            continue
        rows.append(
            {
                "original_invoice_id": original_id,
                "vendor": note.vendor,
                "number": note.number,
                "issue_date": note.issue_date,
                "amount": note.amount,
                "reason": note.reason,
                "file_path": note.file_path,
                "run_id": result.run_id,
            }
        )
    upsert(
        conn,
        CreditNote,
        rows,
        DOCUMENT_KEY,
        ("original_invoice_id", "issue_date", "amount", "reason", "file_path", "run_id"),
    )
    return len(rows)


def json_path(document: Document) -> str:
    return PurePath(document.file_path).with_suffix(".json").as_posix()


def load_invoices(run_id: uuid.UUID, data_dir: Path, engine: Engine) -> LoadInvoicesResult:
    """Load every readable document in one transaction, so a failure leaves `core` unchanged."""
    require_directory(data_dir / INVOICES_DIR)  # `credit_notes/` exists only if a note was issued.
    result = LoadInvoicesResult(run_id=run_id)
    with engine.begin() as conn:
        registry = Registry(client_ids(conn), platform_ids(conn))
        invoices = collect(
            result,
            data_dir,
            INVOICES_DIR,
            lambda payload, relative: parse_invoice(payload, relative, registry),
        )
        notes = collect(result, data_dir, CREDIT_NOTES_DIR, parse_credit_note)
        ids = write_invoices(conn, run_id, invoices)
        result.invoices_written = len(invoices)
        result.lines_written = write_lines(conn, run_id, invoices, ids)
        result.lines_removed = remove_stale_lines(conn, invoices, ids)
        result.credit_notes_written = write_credit_notes(conn, result, notes, ids)
    return result


def load_invoices_from_env(
    run_id: uuid.UUID, data_dir: Path = DEFAULT_DATA_DIR
) -> LoadInvoicesResult:
    engine = create_etl_engine()
    try:
        return load_invoices(run_id, data_dir, engine)
    finally:
        engine.dispose()
