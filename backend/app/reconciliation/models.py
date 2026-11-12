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


PACING_GREEN = "green"
PACING_YELLOW = "yellow"
PACING_RED = "red"
PHASE_NOT_STARTED = "not_started"
PHASE_RUNNING = "running"
PHASE_CLOSED = "closed"


@dataclass(frozen=True)
class ZeroSpendRun:
    """Consecutive zero-spend days of a campaign, as quality reports them."""

    first_day: date
    last_day: date
    issue_key: str  # The key of the quality issue about this same problem.


@dataclass(frozen=True)
class PacingOrder:
    """An insertion order with the daily spend of its linked campaigns, summed per day."""

    client: str
    platform: str
    campaign_ref: str
    start_date: date
    end_date: date
    budget: Decimal
    campaign_count: int
    spend: Mapping[date, Decimal]  # Only days with a reported row; a missing day is unknown.
    zero_spend_runs: tuple[ZeroSpendRun, ...] = ()


@dataclass(frozen=True)
class PacingDay:
    """The order's pace as it looked at the end of `day`."""

    day: date
    elapsed_days: int
    cumulative_plan: Decimal
    cumulative_actual: Decimal
    forecast: Decimal
    ratio: Decimal  # Forecast as a share of budget.
    status: str

    @property
    def gap(self) -> Decimal:
        return self.cumulative_actual - self.cumulative_plan


@dataclass(frozen=True)
class PacingResult:
    order: PacingOrder
    phase: str
    current: PacingDay | None  # None until the flight starts.
    worst: PacingDay | None  # The worst day from the minimum day on; None before it.
    peak_gap: Decimal  # Signed cumulative actual - plan with the largest size.


@dataclass(frozen=True)
class PacingDraft:
    """An issue draft that takes over the quality zero-spend issue when there is one."""

    issue: IssueDraft
    quality_key: str | None


@dataclass(frozen=True)
class PacingOutcome:
    as_of: date
    results: list[PacingResult]
    drafts: list[PacingDraft]
    skipped: list[PacingOrder]  # No linked campaign, or no budget to pace against.


@dataclass(frozen=True)
class NameObservation:
    """One reported day of a campaign with the name the platform gave it that day."""

    platform: str
    platform_campaign_id: str
    client: str
    date: date  # Account-local, as reported.
    name: str
    spend: Decimal | None


@dataclass(frozen=True)
class RenameCandidate:
    """A tagged campaign reported under untagged names first seen in `period`."""

    platform: str
    platform_campaign_id: str
    client: str
    period: date  # First day of the month.
    old_name: str
    new_names: tuple[str, ...]  # In order of first appearance.
    renamed_from: date  # The first day any of `new_names` was reported.
    amount: Decimal  # Spend reported under `new_names` in `period`.


@dataclass(frozen=True)
class RenameVerdict:
    """What the issue proposes; never applied by code (decision 047)."""

    action: str
    note: str | None = None  # The model's reason, shown as unverified data.
    codes: tuple[str, ...] = ()  # Fixed codes for why there is no model judgment.
