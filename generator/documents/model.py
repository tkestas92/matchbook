"""Document records and their ground-truth JSON form.

Ground truth stores money as strings ("1234.56") so it parses back to the exact Decimal.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

CURRENCY = "EUR"


@dataclass(frozen=True)
class InvoiceLine:
    line_no: int
    campaign_ref: str | None
    description: str
    amount: Decimal


@dataclass(frozen=True)
class Invoice:
    kind: str  # "platform" (platform -> agency) or "client" (agency -> client)
    layout: str  # renderer key: meta / google / tiktok / agency
    vendor: str
    number: str
    client_code: str
    platform_key: str | None
    bill_to: str
    account_id: str | None
    issue_date: date
    due_date: date
    period_start: date
    period_end: date
    lines: tuple[InvoiceLine, ...]
    subtotal: Decimal
    vat_rate: Decimal
    vat: Decimal
    total: Decimal
    currency: str = CURRENCY


@dataclass(frozen=True)
class CreditNote:
    vendor: str
    number: str
    original_number: str
    client_code: str
    platform_key: str
    bill_to: str
    issue_date: date
    period_start: date
    period_end: date
    campaign_ref: str
    reason: str
    subtotal: Decimal
    vat_rate: Decimal
    vat: Decimal
    total: Decimal
    currency: str = CURRENCY


def invoice_ground_truth(invoice: Invoice) -> dict[str, Any]:
    return {
        "type": "invoice",
        "kind": invoice.kind,
        "vendor": invoice.vendor,
        "number": invoice.number,
        "client_code": invoice.client_code,
        "platform": invoice.platform_key,
        "bill_to": invoice.bill_to,
        "account_id": invoice.account_id,
        "issue_date": invoice.issue_date.isoformat(),
        "due_date": invoice.due_date.isoformat(),
        "period_start": invoice.period_start.isoformat(),
        "period_end": invoice.period_end.isoformat(),
        "currency": invoice.currency,
        "lines": [
            {
                "line_no": line.line_no,
                "campaign_ref": line.campaign_ref,
                "description": line.description,
                "amount": str(line.amount),
            }
            for line in invoice.lines
        ],
        "subtotal": str(invoice.subtotal),
        "vat_rate": str(invoice.vat_rate),
        "vat": str(invoice.vat),
        "total": str(invoice.total),
    }


def credit_note_ground_truth(note: CreditNote) -> dict[str, Any]:
    return {
        "type": "credit_note",
        "vendor": note.vendor,
        "number": note.number,
        "original_number": note.original_number,
        "client_code": note.client_code,
        "platform": note.platform_key,
        "bill_to": note.bill_to,
        "issue_date": note.issue_date.isoformat(),
        "period_start": note.period_start.isoformat(),
        "period_end": note.period_end.isoformat(),
        "currency": note.currency,
        "campaign_ref": note.campaign_ref,
        "reason": note.reason,
        "subtotal": str(note.subtotal),
        "vat_rate": str(note.vat_rate),
        "vat": str(note.vat),
        "total": str(note.total),
    }
