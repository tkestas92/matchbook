"""Reconciliation rules per client × platform × month: pure functions over plain dataclasses."""

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from app.reconciliation import templates
from app.reconciliation.client_billing import ClientMonth, check_client_invoices, is_fee_line
from app.reconciliation.config import (
    BUDGET_MAX_SHARE,
    BUDGET_MIN_SHARE,
    VARIANCE_MIN_EUR,
    VARIANCE_MIN_SHARE,
    missing_days_limit,
)
from app.reconciliation.models import (
    KIND_PLATFORM,
    STATUS_INCOMPLETE,
    STATUS_ISSUE,
    STATUS_OK,
    BillingTerms,
    BoundaryShift,
    Budget,
    DeliveryGap,
    DeliveryRow,
    DuplicateInvoice,
    GapNote,
    Invoice,
    IssueDraft,
    Line,
    LineLink,
    PlanOrder,
    ReconciliationInput,
    ReconciliationOutcome,
    Slot,
    SlotResult,
    UnmatchedLine,
    month_start,
)

NO_CODE = "the line has no campaign code"
NO_ORDER = "no insertion order has this campaign code"
SEVERAL_ORDERS = "several insertion orders have this campaign code"
NO_CAMPAIGN = "no delivered campaign is linked to this insertion order"
SEVERAL_CAMPAIGNS = "several delivered campaigns are linked to this insertion order"

ZERO = Decimal(0)


def is_material(variance: Decimal, base: Decimal) -> bool:
    size = abs(variance)
    return size > VARIANCE_MIN_EUR and size > abs(base) * VARIANCE_MIN_SHARE


def group_by[K, V](items: Iterable[V], key: Callable[[V], K]) -> dict[K, list[V]]:
    grouped: dict[K, list[V]] = defaultdict(list)
    for item in items:
        grouped[key(item)].append(item)
    return dict(grouped)


def total(amounts: Iterable[Decimal]) -> Decimal:
    return sum(amounts, ZERO)


def duplicate_group(invoice: Invoice) -> tuple[object, ...]:
    return (
        invoice.kind,
        invoice.vendor,
        invoice.client,
        invoice.platform,
        invoice.period_start,
        invoice.period_end,
        invoice.net,
    )


def issue_order(invoice: Invoice) -> tuple[date, str]:
    return invoice.issue_date or date.max, invoice.number


def split_duplicates(invoices: Iterable[Invoice]) -> tuple[list[Invoice], list[DuplicateInvoice]]:
    """The invoices to count, and the copies; the earliest issued of a group is the original."""
    kept: list[Invoice] = []
    duplicates: list[DuplicateInvoice] = []
    for group in group_by(invoices, duplicate_group).values():
        original, *copies = sorted(group, key=issue_order)
        kept.append(original)
        duplicates.extend(DuplicateInvoice(copy, original) for copy in copies)
    return kept, duplicates


def match_line(
    invoice: Invoice, line: Line, orders: dict[tuple[str, str], list[PlanOrder]]
) -> LineLink | str:
    """The campaign of one line by its code, or why there is none; nothing is guessed."""
    if line.campaign_ref is None:
        return NO_CODE
    candidates = [
        order
        for order in orders.get((invoice.client, line.campaign_ref), [])
        if invoice.platform in (None, order.platform)
    ]
    if not candidates:
        return NO_ORDER
    if len(candidates) > 1:
        return SEVERAL_ORDERS
    campaign_ids = candidates[0].campaign_ids
    if not campaign_ids:
        return NO_CAMPAIGN
    if len(campaign_ids) > 1:
        return SEVERAL_CAMPAIGNS
    return LineLink(line.id, campaign_ids[0])


def link_lines(
    invoices: Iterable[Invoice], orders: Iterable[PlanOrder]
) -> tuple[list[LineLink], list[UnmatchedLine]]:
    by_code = group_by(orders, lambda order: (order.client, order.campaign_ref))
    links: list[LineLink] = []
    unmatched: list[UnmatchedLine] = []
    for invoice in invoices:
        for line in invoice.lines:
            if is_fee_line(invoice, line):
                continue
            match = match_line(invoice, line, by_code)
            if isinstance(match, LineLink):
                links.append(match)
            else:
                unmatched.append(UnmatchedLine(invoice, line, match))
    return links, unmatched


def next_month(period: date) -> date:
    return (period + timedelta(days=32)).replace(day=1)


def gap_days_in(gap: DeliveryGap, period: date) -> int:
    first = max(gap.first_day, period)
    last = min(gap.last_day, next_month(period) - timedelta(days=1))
    return max((last - first).days + 1, 0)


@dataclass(frozen=True)
class SlotEvidence:
    """What delivery itself says about a variance in one slot."""

    left: list[BoundaryShift]  # Days of this month that are dated in the next one.
    entered: list[BoundaryShift]  # Days of the previous month that are dated in this one.
    gaps: list[DeliveryGap]
    rows: list[DeliveryRow]


def missing_spend_limit(period: date, evidence: SlotEvidence) -> Decimal:
    limit = ZERO
    for gap in evidence.gaps:
        spends = (row.spend for row in evidence.rows if row.campaign_id == gap.campaign_id)
        limit += missing_days_limit(gap_days_in(gap, period), max(spends, default=ZERO))
    return limit


def shift_draft(shift: BoundaryShift, agency_timezone: str) -> IssueDraft:
    """Keyed like the quality issue, so one problem stays one issue."""
    same_timezone = shift.timezone == agency_timezone
    issue_type = templates.MONTH_BOUNDARY_SHIFT if same_timezone else templates.TIMEZONE_SHIFT
    return IssueDraft(
        dedup_key=shift.issue_key,
        type=issue_type,
        severity=templates.SEVERITIES[issue_type],
        client=shift.client,
        platform=shift.platform,
        period=shift.left_slot.period,
        entity_type="campaign",
        entity_id=shift.campaign_id,
        amount=shift.spend,
        explanation=templates.shift_explanation(shift, agency_timezone),
        proposed_action=templates.PROPOSED_ACTIONS[issue_type],
    )


def variance_draft(
    slot: Slot, invoiced: Decimal, delivered: Decimal, variance: Decimal, moved: Decimal
) -> IssueDraft:
    """Keyed without the type, so a later reclassification updates this issue."""
    issue_type = templates.OVERCHARGE if variance > 0 else templates.UNDERCHARGE
    month = templates.month_text(slot.period)
    return IssueDraft(
        dedup_key=f"reconciliation:invoice_variance:{slot.client}:{slot.platform}:{month}",
        type=issue_type,
        severity=templates.SEVERITIES[issue_type],
        client=slot.client,
        platform=slot.platform,
        period=slot.period,
        entity_type="client_platform_month",
        entity_id=f"{slot.client}:{slot.platform}:{month}",
        amount=variance,
        explanation=templates.variance_explanation(slot, invoiced, delivered, variance, moved),
        proposed_action=templates.PROPOSED_ACTIONS[issue_type],
    )


def check_invoice_variance(
    slot: Slot,
    invoiced: Decimal,
    delivered: Decimal,
    evidence: SlotEvidence,
    agency_timezone: str,
) -> tuple[list[IssueDraft], list[GapNote]]:
    """Rule 1: invoice vs delivered; shifted days first, then missing days, then a real variance."""
    variance = invoiced - delivered
    if not is_material(variance, delivered):
        return [], []
    drafts: list[IssueDraft] = []
    moved = total(s.spend for s in evidence.left) - total(s.spend for s in evidence.entered)
    corrected = delivered + moved
    if abs(invoiced - corrected) < abs(variance):
        drafts = [shift_draft(shift, agency_timezone) for shift in evidence.left]
        variance, delivered = invoiced - corrected, corrected
    else:
        moved = ZERO
    if not is_material(variance, delivered):
        return drafts, []
    limit = missing_spend_limit(slot.period, evidence)
    if evidence.gaps and 0 < variance <= limit:
        gap = min(evidence.gaps, key=lambda g: (g.first_day, g.campaign_id))
        text = templates.gap_explanation(slot, gap, invoiced, delivered, variance, limit)
        return drafts, [GapNote(gap.issue_key, variance, text)]
    return [*drafts, variance_draft(slot, invoiced, delivered, variance, moved)], []


def duplicate_draft(duplicate: DuplicateInvoice) -> IssueDraft:
    copy = duplicate.copy
    return IssueDraft(
        dedup_key=f"reconciliation:duplicate_invoice:{copy.vendor}:{copy.number}",
        type=templates.DUPLICATE_INVOICE,
        severity=templates.SEVERITIES[templates.DUPLICATE_INVOICE],
        client=copy.client,
        platform=copy.platform,
        period=copy.period,
        entity_type="invoice",
        entity_id=copy.number,
        amount=copy.net,
        explanation=templates.duplicate_explanation(copy, duplicate.original),
        proposed_action=templates.PROPOSED_ACTIONS[templates.DUPLICATE_INVOICE],
    )


def needs_matching_draft(unmatched: UnmatchedLine) -> IssueDraft:
    invoice, line = unmatched.invoice, unmatched.line
    return IssueDraft(
        dedup_key=f"reconciliation:needs_matching:{invoice.vendor}:{invoice.number}:{line.line_no}",
        type=templates.NEEDS_MATCHING,
        severity=templates.SEVERITIES[templates.NEEDS_MATCHING],
        client=invoice.client,
        platform=invoice.platform,
        period=invoice.period,
        entity_type="invoice_line",
        entity_id=f"{invoice.number}:{line.line_no}",
        amount=line.amount,
        explanation=templates.needs_matching_explanation(invoice, line, unmatched.reason),
        proposed_action=templates.PROPOSED_ACTIONS[templates.NEEDS_MATCHING],
    )


def invoiced_by_slot(invoices: Iterable[Invoice]) -> dict[Slot, Decimal]:
    """Net of VAT and before credit notes, which only affect what the client is billed."""
    amounts: dict[Slot, Decimal] = defaultdict(Decimal)
    for invoice in invoices:
        if invoice.kind == KIND_PLATFORM and invoice.platform is not None:
            amounts[Slot(invoice.client, invoice.platform, invoice.period)] += invoice.net
    return dict(amounts)


def planned_by_slot(budgets: Iterable[Budget]) -> dict[Slot, Decimal | None]:
    """Budgets of one-month orders; an order spanning months leaves each of them unknown."""
    planned: dict[Slot, Decimal | None] = {}
    for budget in budgets:
        first, last = month_start(budget.start_date), month_start(budget.end_date)
        if first != last:
            period = first
            while period <= last:
                planned[Slot(budget.client, budget.platform, period)] = None
                period = next_month(period)
            continue
        slot = Slot(budget.client, budget.platform, first)
        current = planned.get(slot, ZERO)
        planned[slot] = None if current is None else current + budget.amount
    return planned


def check_budget(slot: Slot, planned: Decimal, delivered: Decimal) -> IssueDraft | None:
    """Rule 2: delivered vs the insertion order budget; the bounds themselves are fine."""
    if planned * BUDGET_MIN_SHARE <= delivered <= planned * BUDGET_MAX_SHARE:
        return None
    issue_type = templates.BUDGET_OVERSPEND if delivered > planned else templates.BUDGET_UNDERSPEND
    entity = f"{slot.client}:{slot.platform}:{templates.month_text(slot.period)}"
    return IssueDraft(
        dedup_key=f"reconciliation:budget:{entity}",
        type=issue_type,
        severity=templates.SEVERITIES[issue_type],
        client=slot.client,
        platform=slot.platform,
        period=slot.period,
        entity_type="client_platform_month",
        entity_id=entity,
        amount=delivered - planned,
        explanation=templates.budget_explanation(slot, planned, delivered),
        proposed_action=templates.PROPOSED_ACTIONS[issue_type],
    )


@dataclass(frozen=True)
class Flags:
    slots: set[Slot]  # Slots with an issue of their own.
    client_months: set[ClientMonth]  # Client-months with a client-level issue.

    def __contains__(self, slot: Slot) -> bool:
        return slot in self.slots or (slot.client, slot.period) in self.client_months


def slot_status(
    slot: Slot,
    delivered: Decimal | None,
    invoiced: Decimal | None,
    flagged: Flags,
    billing: Mapping[str, BillingTerms],
    billed: set[ClientMonth],
) -> str:
    if slot in flagged:
        return STATUS_ISSUE
    if delivered is None:
        return STATUS_INCOMPLETE
    terms = billing.get(slot.client)
    is_agency_billed = terms is None or terms.is_agency_billed
    if invoiced is None:
        return STATUS_INCOMPLETE if is_agency_billed else STATUS_OK
    if is_material(invoiced - delivered, delivered):
        return STATUS_ISSUE
    if is_agency_billed and (slot.client, slot.period) not in billed:
        return STATUS_INCOMPLETE
    return STATUS_OK


def slot_evidence(
    slot: Slot,
    data: ReconciliationInput,
    rows: dict[Slot, list[DeliveryRow]],
) -> SlotEvidence:
    return SlotEvidence(
        left=[shift for shift in data.shifts if shift.left_slot == slot],
        entered=[shift for shift in data.shifts if shift.entered_slot == slot],
        gaps=[
            gap
            for gap in data.gaps
            if (gap.client, gap.platform) == (slot.client, slot.platform)
            and gap_days_in(gap, slot.period) > 0
        ],
        rows=rows.get(slot, []),
    )


def flagged_slots(issues: Sequence[IssueDraft]) -> Flags:
    return Flags(
        slots={
            Slot(issue.client, issue.platform, issue.period)
            for issue in issues
            if issue.platform is not None
        },
        client_months={(issue.client, issue.period) for issue in issues if issue.platform is None},
    )


def run_rules(data: ReconciliationInput) -> ReconciliationOutcome:
    """All rules over one input; duplicates go first so a copy never reads as a variance."""
    kept, duplicates = split_duplicates(data.invoices)
    links, unmatched = link_lines(data.invoices, data.orders)
    kept_keys = {invoice.key for invoice in kept}
    issues = [
        *(needs_matching_draft(item) for item in unmatched if item.invoice.key in kept_keys),
        *(duplicate_draft(item) for item in duplicates),
    ]
    notes: list[GapNote] = []
    rows = group_by(data.delivery, lambda row: row.slot)
    delivered = {slot: total(row.spend for row in group) for slot, group in rows.items()}
    invoiced = invoiced_by_slot(kept)
    for slot in sorted(delivered.keys() & invoiced.keys()):
        evidence = slot_evidence(slot, data, rows)
        drafts, gap_notes = check_invoice_variance(
            slot, invoiced[slot], delivered[slot], evidence, data.agency_timezone
        )
        issues.extend(drafts)
        notes.extend(gap_notes)
    planned = planned_by_slot(data.budgets)
    for slot in sorted(delivered.keys() & planned.keys()):
        budget = planned[slot]
        draft = None if budget is None else check_budget(slot, budget, delivered[slot])
        if draft is not None:
            issues.append(draft)
    client = check_client_invoices(kept, data.orders, data.billing, data.credit_notes)
    issues.extend(client.issues)
    flagged = flagged_slots(issues)
    slots = delivered.keys() | invoiced.keys() | planned.keys() | client.client_invoiced.keys()
    results = []
    for slot in sorted(slots | client.expected_client_amount.keys()):
        spent, billed = delivered.get(slot), invoiced.get(slot)
        results.append(
            SlotResult(
                slot=slot,
                planned=planned.get(slot),
                delivered=spent,
                invoiced=billed,
                client_invoiced=client.client_invoiced.get(slot),
                expected_client_amount=client.expected_client_amount.get(slot),
                variance=None if spent is None or billed is None else billed - spent,
                status=slot_status(slot, spent, billed, flagged, data.billing, client.billed),
            )
        )
    return ReconciliationOutcome(links=links, results=results, issues=issues, gap_notes=notes)
