"""What the model may answer, and what extraction returns (threat model T3, decision 042).

`InvoiceExtraction` is the only shape the model can answer in: strict types, no extra fields,
money as decimal strings (a JSON number fails, so no float ever carries cents), dates as
`YYYY-MM-DD` strings that code parses, and a length or pattern limit on every string. It has no
field for an email, phone, IBAN or address: extraction never needs PII (T2).

`ExtractedDocument` uses the ground-truth field names (generator/documents/model.py) with
`Decimal` and `date` values. Fields the code derives (type, kind, platform, client_code,
line_no) are not asked from the model.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

EXTRACTED = "extracted"
NEEDS_REVIEW = "needs_review"
REJECTED = "rejected"
FAILED = "failed"
ExtractionStatus = Literal["extracted", "needs_review", "rejected", "failed"]

MAX_LINES = 50

Name = Annotated[str, StringConstraints(min_length=1, max_length=120)]
Text = Annotated[str, StringConstraints(min_length=1, max_length=200)]
DocumentNumber = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9/._-]{0,39}$")]
AccountId = Annotated[str, StringConstraints(pattern=r"^[0-9][0-9 -]{0,29}$")]
CampaignRef = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,39}$")]
DateText = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
Money = Annotated[str, StringConstraints(pattern=r"^-?\d{1,10}\.\d{2}$")]
Rate = Annotated[str, StringConstraints(pattern=r"^0\.\d{1,4}$")]
Score = Annotated[str, StringConstraints(pattern=r"^(?:0(?:\.\d{1,3})?|1(?:\.0{1,3})?)$")]


class LineExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    campaign_ref: CampaignRef | None
    description: Text
    amount: Money


class InvoiceExtraction(BaseModel):
    """The model's answer for one invoice or credit note."""

    model_config = ConfigDict(extra="forbid", strict=True)

    document_type: Literal["invoice", "credit_note"]
    vendor: Name
    number: DocumentNumber
    bill_to: Name
    account_id: AccountId | None
    issue_date: DateText
    due_date: DateText | None
    period_start: DateText
    period_end: DateText
    currency: Currency
    lines: Annotated[list[LineExtraction], Field(max_length=MAX_LINES)]
    subtotal: Money
    vat_rate: Rate
    vat: Money
    total: Money
    original_number: DocumentNumber | None
    campaign_ref: CampaignRef | None
    reason: Text | None
    model_confidence: Score
    contains_instructions: bool


@dataclass(frozen=True)
class ExtractedLine:
    line_no: int
    campaign_ref: str | None
    description: str
    amount: Decimal


@dataclass(frozen=True)
class ExtractedDocument:
    """A field the code could not parse or derive is None, and its confidence is 0."""

    type: str
    kind: str | None  # "platform" / "client"; None for credit notes
    vendor: str
    number: str
    client_code: str | None
    platform: str | None
    bill_to: str
    account_id: str | None
    issue_date: date | None
    due_date: date | None
    period_start: date | None
    period_end: date | None
    currency: str
    lines: tuple[ExtractedLine, ...]
    subtotal: Decimal
    vat_rate: Decimal
    vat: Decimal
    total: Decimal
    original_number: str | None
    campaign_ref: str | None
    reason: str | None


@dataclass(frozen=True)
class LLMUsage:
    model: str
    prompt_id: str
    prompt_hash: str
    input_tokens: int
    output_tokens: int
    cost_eur: Decimal
    latency_s: float
    attempts: int


@dataclass(frozen=True)
class ExtractionResult:
    """Status and reasons are decided by code. No status means "approved": a human does that."""

    status: ExtractionStatus
    reasons: tuple[str, ...]
    document: ExtractedDocument | None = None
    field_confidence: Mapping[str, Decimal] | None = None
    confidence: Decimal = Decimal(0)
    usage: LLMUsage | None = None
