"""Rule 3: client invoices against the billing model, and their split over platforms."""

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

from app.reconciliation import templates
from app.reconciliation.config import CLIENT_INVOICE_MIN_EUR
from app.reconciliation.models import (
    BILLING_MANAGEMENT_FEE,
    BILLING_MARKUP,
    KIND_PLATFORM,
    BillingTerms,
    CreditNote,
    Invoice,
    IssueDraft,
    Line,
    PlanOrder,
    Slot,
)

CENT = Decimal("0.01")
ZERO = Decimal(0)

ClientMonth = tuple[str, date]


def is_fee_line(invoice: Invoice, line: Line) -> bool:
    """Client invoices carry a commission or fee line that belongs to no campaign."""
    return invoice.kind != KIND_PLATFORM and line.campaign_ref is None


def is_material(difference: Decimal) -> bool:
    return abs(difference) > CLIENT_INVOICE_MIN_EUR


def expected_fee(base: Decimal, terms: BillingTerms) -> Decimal:
    """The fee the contract gives on `base`; a commission is rounded half up to cents."""
    if terms.model == BILLING_MARKUP and terms.markup_pct is not None:
        return (base * terms.markup_pct / 100).quantize(CENT, rounding=ROUND_HALF_UP)
    if terms.model == BILLING_MANAGEMENT_FEE and terms.mgmt_fee is not None:
        return terms.mgmt_fee
    raise ValueError(f"billing model {terms.model} has no agency fee")


def split_by_weight(amount: Decimal, weights: Mapping[str, Decimal]) -> dict[str, Decimal]:
    """Split `amount` in whole cents by weight, largest remainder first, ties by name.

    The parts always sum to `amount`. Without a usable weight (a negative one, or a total of
    zero or less) every name gets an equal share.
    """
    names = sorted(weights)
    if not names:
        return {}
    usable = all(weight >= 0 for weight in weights.values()) and sum(weights.values()) > 0
    shares = dict(weights) if usable else {name: Decimal(1) for name in names}
    weight_total = sum(shares.values(), ZERO)
    cents = int(amount / CENT)
    exact = {name: cents * shares[name] / weight_total for name in names}
    parts = {name: int(exact[name].to_integral_value(rounding=ROUND_FLOOR)) for name in names}
    left = cents - sum(parts.values())
    for name in sorted(names, key=lambda n: (parts[n] - exact[n], n))[:left]:
        parts[name] += 1
    return {name: parts[name] * CENT for name in names}


def platform_by_code(orders: Iterable[PlanOrder]) -> dict[tuple[str, str], str]:
    """The platform of each client's campaign code, when exactly one order has it."""
    platforms: dict[tuple[str, str], set[str]] = defaultdict(set)
    for order in orders:
        platforms[(order.client, order.campaign_ref)].add(order.platform)
    return {key: next(iter(found)) for key, found in platforms.items() if len(found) == 1}


def credits_by_slot(
    credit_notes: Iterable[CreditNote], platform_invoices: Iterable[Invoice]
) -> dict[Slot, list[CreditNote]]:
    """Credit notes of the kept platform invoices; a note on a duplicate copy is not passed on."""
    slots = {
        invoice.key: Slot(invoice.client, invoice.platform, invoice.period)
        for invoice in platform_invoices
        if invoice.kind == KIND_PLATFORM and invoice.platform is not None
    }
    credits: dict[Slot, list[CreditNote]] = defaultdict(list)
    for note in credit_notes:
        if note.original in slots:
            credits[slots[note.original]].append(note)
    return dict(credits)


@dataclass
class PlatformLines:
    """One platform's lines on a client's invoices for a month."""

    media: Decimal = ZERO
    credit_applied: Decimal = ZERO  # Positive: the credit lines are printed negative.

    @property
    def net(self) -> Decimal:
        return self.media - self.credit_applied


@dataclass
class ClientMonthLines:
    fee: Decimal = ZERO
    non_fee: Decimal = ZERO
    platforms: dict[str, PlatformLines] = field(default_factory=lambda: defaultdict(PlatformLines))
    unattributed: bool = False


def read_lines(
    invoices: Iterable[Invoice], platform_of: Mapping[tuple[str, str], str]
) -> ClientMonthLines:
    lines = ClientMonthLines()
    for invoice in invoices:
        for line in invoice.lines:
            if is_fee_line(invoice, line):
                lines.fee += line.amount
                continue
            lines.non_fee += line.amount
            platform = platform_of.get((invoice.client, line.campaign_ref or ""))
            if platform is None:
                lines.unattributed = True
            elif line.amount < 0:
                lines.platforms[platform].credit_applied -= line.amount
            else:
                lines.platforms[platform].media += line.amount
    return lines


def month_key(client: str, period: date) -> str:
    return f"{client}:{templates.month_text(period)}"


def fee_draft(
    client: str,
    period: date,
    terms: BillingTerms,
    lines: ClientMonthLines,
    expected: Decimal,
) -> IssueDraft:
    """Client-level: the fee belongs to no platform."""
    return IssueDraft(
        dedup_key=f"reconciliation:client_fee:{month_key(client, period)}",
        type=templates.WRONG_MARKUP,
        severity=templates.SEVERITIES[templates.WRONG_MARKUP],
        client=client,
        platform=None,
        period=period,
        entity_type="client_month",
        entity_id=month_key(client, period),
        amount=lines.fee - expected,
        explanation=templates.fee_explanation(
            client, period, terms, lines.non_fee, lines.fee, expected
        ),
        proposed_action=templates.PROPOSED_ACTIONS[templates.WRONG_MARKUP],
    )


def credit_draft(
    slot: Slot, terms: BillingTerms, notes: Sequence[CreditNote], applied: Decimal
) -> IssueDraft:
    """Keyed without the type, so a credit that turns from missing to excess stays one issue."""
    expected = sum((note.amount for note in notes), ZERO)
    missing = expected - applied
    issue_type = templates.CREDIT_NOT_APPLIED if missing > 0 else templates.CREDIT_OVER_APPLIED
    entity = f"{slot.client}:{slot.platform}:{templates.month_text(slot.period)}"
    return IssueDraft(
        dedup_key=f"reconciliation:credit:{entity}",
        type=issue_type,
        severity=templates.SEVERITIES[issue_type],
        client=slot.client,
        platform=slot.platform,
        period=slot.period,
        entity_type="client_platform_month",
        entity_id=entity,
        amount=missing,
        explanation=templates.credit_explanation(
            slot, terms, sorted(note.number for note in notes), expected, applied
        ),
        proposed_action=templates.PROPOSED_ACTIONS[issue_type],
    )


def with_fee_share(net: Mapping[str, Decimal], fee: Decimal) -> dict[str, Decimal]:
    """Each platform's net media plus its share of the fee in proportion to that net."""
    shares = split_by_weight(fee, net)
    return {platform: net[platform] + shares[platform] for platform in net}


@dataclass(frozen=True)
class ClientBillingOutcome:
    issues: list[IssueDraft]
    client_invoiced: dict[Slot, Decimal]
    expected_client_amount: dict[Slot, Decimal]
    billed: set[ClientMonth]  # Client-months with at least one client invoice.


def check_client_month(
    client: str,
    period: date,
    terms: BillingTerms,
    lines: ClientMonthLines,
    credits: Mapping[Slot, list[CreditNote]],
) -> tuple[list[IssueDraft], dict[str, Decimal], dict[str, Decimal]]:
    """Rule 3 for one client-month: the fee, then credits per platform, then the D5 split."""
    issues: list[IssueDraft] = []
    expected = expected_fee(lines.non_fee, terms)
    if is_material(lines.fee - expected):
        issues.append(fee_draft(client, period, terms, lines, expected))
    if lines.unattributed:
        return issues, {}, {}
    credited = {slot.platform for slot in credits if (slot.client, slot.period) == (client, period)}
    invoiced_net: dict[str, Decimal] = {}
    expected_net: dict[str, Decimal] = {}
    for platform in sorted(lines.platforms.keys() | credited):
        slot = Slot(client, platform, period)
        notes = credits.get(slot, [])
        platform_lines = lines.platforms.get(platform, PlatformLines())
        credit = sum((note.amount for note in notes), ZERO)
        if is_material(credit - platform_lines.credit_applied):
            issues.append(credit_draft(slot, terms, notes, platform_lines.credit_applied))
        invoiced_net[platform] = platform_lines.net
        expected_net[platform] = platform_lines.media - credit
    fee_due = expected_fee(sum(expected_net.values(), ZERO), terms)
    return issues, with_fee_share(invoiced_net, lines.fee), with_fee_share(expected_net, fee_due)


def check_client_invoices(
    kept: Sequence[Invoice],
    orders: Iterable[PlanOrder],
    billing: Mapping[str, BillingTerms],
    credit_notes: Iterable[CreditNote],
) -> ClientBillingOutcome:
    """Rule 3 over the kept invoices; client-owned clients are billed by the platform directly."""
    platform_of = platform_by_code(orders)
    credits = credits_by_slot(credit_notes, kept)
    by_month: dict[ClientMonth, list[Invoice]] = defaultdict(list)
    for invoice in kept:
        if invoice.kind != KIND_PLATFORM:
            by_month[(invoice.client, invoice.period)].append(invoice)
    outcome = ClientBillingOutcome([], {}, {}, set())
    for (client, period), invoices in sorted(by_month.items()):
        terms = billing.get(client)
        if terms is None or not terms.is_agency_billed:
            continue
        outcome.billed.add((client, period))
        issues, invoiced, expected = check_client_month(
            client, period, terms, read_lines(invoices, platform_of), credits
        )
        outcome.issues.extend(issues)
        for platform, amount in invoiced.items():
            outcome.client_invoiced[Slot(client, platform, period)] = amount
        for platform, amount in expected.items():
            outcome.expected_client_amount[Slot(client, platform, period)] = amount
    return outcome
