"""Plain inputs and outputs of the reconciliation rules; nothing here touches a database."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

KIND_PLATFORM = "platform"
BILLING_MARKUP = "markup"
BILLING_MANAGEMENT_FEE = "mgmt_fee"
BILLING_CLIENT_OWNED = "client_owned"
STATUS_OK = "ok"
STATUS_ISSUE = "issue"
STATUS_INCOMPLETE = "incomplete"


def month_start(day: date) -> date:
    return day.replace(day=1)


@dataclass(frozen=True, order=True)
class Slot:
    """One client × platform × month, the unit every comparison runs on."""

    client: str
    platform: str
    period: date  # First day of the month.


@dataclass(frozen=True)
class DeliveryRow:
    """One reported day; `day` is account-local and cannot be moved to another timezone."""

    client: str
    platform: str
    campaign_id: str
    day: date
    spend: Decimal

    @property
    def slot(self) -> Slot:
        return Slot(self.client, self.platform, month_start(self.day))


@dataclass(frozen=True)
class BoundaryShift:
    """A campaign's last day of the month is absent and a row is dated the 1st of the next."""

    client: str
    platform: str
    timezone: str
    campaign_id: str
    campaign_name: str
    missing_day: date
    row_day: date
    spend: Decimal
    issue_key: str  # The key of the quality issue about this same problem.

    @property
    def left_slot(self) -> Slot:
        return Slot(self.client, self.platform, month_start(self.missing_day))

    @property
    def entered_slot(self) -> Slot:
        return Slot(self.client, self.platform, month_start(self.row_day))


@dataclass(frozen=True)
class DeliveryGap:
    """Consecutive days a campaign reported nothing, inside its reported span."""

    client: str
    platform: str
    campaign_id: str
    campaign_name: str
    first_day: date
    last_day: date
    issue_key: str  # The key of the quality issue about this same problem.


@dataclass(frozen=True)
class Line:
    id: int
    line_no: int
    campaign_ref: str | None
    amount: Decimal


@dataclass(frozen=True)
class Invoice:
    kind: str
    vendor: str
    number: str
    client: str
    platform: str | None  # Client invoices cover every platform of the client.
    issue_date: date | None
    period_start: date
    period_end: date
    lines: tuple[Line, ...]

    @property
    def key(self) -> tuple[str, str]:
        return self.vendor, self.number

    @property
    def net(self) -> Decimal:
        return sum((line.amount for line in self.lines), Decimal(0))

    @property
    def period(self) -> date:
        return month_start(self.period_start)


@dataclass(frozen=True)
class PlanOrder:
    """An insertion order and the campaigns linked to it."""

    client: str
    platform: str
    campaign_ref: str
    campaign_ids: tuple[int, ...]


@dataclass(frozen=True)
class Budget:
    """An insertion order's budget over its dates."""

    client: str
    platform: str
    start_date: date
    end_date: date
    amount: Decimal


@dataclass(frozen=True)
class BillingTerms:
    model: str  # markup, mgmt_fee or client_owned.
    markup_pct: Decimal | None = None
    mgmt_fee: Decimal | None = None

    @property
    def is_agency_billed(self) -> bool:
        return self.model != BILLING_CLIENT_OWNED


@dataclass(frozen=True)
class CreditNote:
    """A platform credit note; `amount` is its net subtotal, positive as printed."""

    vendor: str
    number: str
    original: tuple[str, str]  # The `Invoice.key` of the platform invoice it credits.
    amount: Decimal


@dataclass(frozen=True)
class ReconciliationInput:
    agency_timezone: str
    billing: Mapping[str, BillingTerms]
    delivery: Sequence[DeliveryRow]
    shifts: Sequence[BoundaryShift]
    gaps: Sequence[DeliveryGap]
    invoices: Sequence[Invoice]
    orders: Sequence[PlanOrder]
    budgets: Sequence[Budget]
    credit_notes: Sequence[CreditNote]


@dataclass(frozen=True)
class LineLink:
    line_id: int
    campaign_id: int


@dataclass(frozen=True)
class UnmatchedLine:
    invoice: Invoice
    line: Line
    reason: str


@dataclass(frozen=True)
class DuplicateInvoice:
    copy: Invoice
    original: Invoice


@dataclass(frozen=True)
class IssueDraft:
    dedup_key: str
    type: str
    severity: str
    client: str
    platform: str | None
    period: date
    entity_type: str
    entity_id: str
    amount: Decimal
    explanation: str
    proposed_action: str


@dataclass(frozen=True)
class GapNote:
    """Adds a variance to the existing quality issue about the missing days that explain it."""

    issue_key: str
    amount: Decimal
    explanation: str


@dataclass(frozen=True)
class SlotResult:
    slot: Slot
    planned: Decimal | None  # None means the source is missing, not zero.
    delivered: Decimal | None
    invoiced: Decimal | None
    client_invoiced: Decimal | None
    expected_client_amount: Decimal | None
    variance: Decimal | None
    status: str


@dataclass(frozen=True)
class ReconciliationOutcome:
    links: list[LineLink]
    results: list[SlotResult]
    issues: list[IssueDraft]
    gap_notes: list[GapNote]
