"""Issue types, severities, actions and the template text of every explanation (no LLM)."""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.reconciliation.models import (
    BILLING_MARKUP,
    BillingTerms,
    BoundaryShift,
    DeliveryGap,
    Invoice,
    Line,
    Slot,
)

OVERCHARGE = "reconciliation_overcharge"
UNDERCHARGE = "reconciliation_undercharge"
TIMEZONE_SHIFT = "reconciliation_timezone_shift"
MONTH_BOUNDARY_SHIFT = "reconciliation_month_boundary_shift"
DUPLICATE_INVOICE = "reconciliation_duplicate_invoice"
NEEDS_MATCHING = "reconciliation_needs_matching"
BUDGET_OVERSPEND = "reconciliation_budget_overspend"
BUDGET_UNDERSPEND = "reconciliation_budget_underspend"
WRONG_MARKUP = "reconciliation_wrong_markup"
CREDIT_NOT_APPLIED = "reconciliation_credit_not_applied"
CREDIT_OVER_APPLIED = "reconciliation_credit_over_applied"

SEVERITIES = {
    OVERCHARGE: "high",
    UNDERCHARGE: "medium",
    TIMEZONE_SHIFT: "low",
    MONTH_BOUNDARY_SHIFT: "medium",
    DUPLICATE_INVOICE: "high",
    NEEDS_MATCHING: "medium",
    BUDGET_OVERSPEND: "high",
    BUDGET_UNDERSPEND: "medium",
    WRONG_MARKUP: "high",
    CREDIT_NOT_APPLIED: "high",
    CREDIT_OVER_APPLIED: "medium",
}

PROPOSED_ACTIONS = {
    OVERCHARGE: "Query the invoice with the platform before paying it.",
    UNDERCHARGE: "Check with the platform whether a further invoice is due.",
    TIMEZONE_SHIFT: "Confirm the account's timezone with the platform; do not dispute the invoice.",
    MONTH_BOUNDARY_SHIFT: "Ask the platform why this day is dated in the next month.",
    DUPLICATE_INVOICE: "Hold this copy and confirm with the vendor before paying it.",
    NEEDS_MATCHING: "Find the campaign this line belongs to and link it by hand.",
    BUDGET_OVERSPEND: "Check with the planner whether the extra spend was approved.",
    BUDGET_UNDERSPEND: "Check with the planner whether the budget should be moved or released.",
    WRONG_MARKUP: "Hold the client invoice and correct the fee before it is sent.",
    CREDIT_NOT_APPLIED: "Hold the client invoice and pass the platform credit on to the client.",
    CREDIT_OVER_APPLIED: "Check which credit the client invoice applies before it is sent.",
}

PERCENT_CENTS = Decimal("0.01")


def eur(amount: Decimal) -> str:
    return f"EUR {amount:,.2f}"


def month_text(period: date) -> str:
    return period.strftime("%Y-%m")


def slot_text(slot: Slot) -> str:
    return f"{slot.client} {slot.platform} {month_text(slot.period)}"


def percent_text(part: Decimal, whole: Decimal) -> str:
    """`part` as a share of `whole`, rounded half up; empty when `whole` is zero."""
    if whole == 0:
        return ""
    share = (abs(part) / abs(whole) * 100).quantize(PERCENT_CENTS, rounding=ROUND_HALF_UP)
    return f" ({share}%)"


def moved_text(moved: Decimal) -> str:
    if moved == 0:
        return ""
    return f", after correcting delivery by {eur(moved)} for spend dated across a month end"


def variance_explanation(
    slot: Slot, invoiced: Decimal, delivered: Decimal, variance: Decimal, moved: Decimal
) -> str:
    direction = "above" if variance > 0 else "below"
    return (
        f"{slot_text(slot)}: invoiced {eur(invoiced)} is {eur(abs(variance))}"
        f"{percent_text(variance, delivered)} {direction} delivered spend of {eur(delivered)}"
        f"{moved_text(moved)}."
    )


def shift_explanation(shift: BoundaryShift, agency_timezone: str) -> str:
    next_month = month_text(shift.row_day)
    month = month_text(shift.missing_day)
    fact = (
        f"{shift.campaign_name}: no delivery row on {shift.missing_day.isoformat()} and "
        f"{eur(shift.spend)} dated {shift.row_day.isoformat()}. "
        f"That spend counts towards {next_month}, so delivery looks {eur(shift.spend)} low in "
        f"{month} and {eur(shift.spend)} high in {next_month}; this is not an overcharge. "
    )
    if shift.timezone == agency_timezone:
        return (
            fact + f"The account timezone is the agency timezone ({agency_timezone}), "
            "so a timezone difference does not explain it."
        )
    return (
        fact + f"The account timezone ({shift.timezone}) differs from the agency timezone "
        f"({agency_timezone}), which can move spend across a month end."
    )


def gap_explanation(
    slot: Slot,
    gap: DeliveryGap,
    invoiced: Decimal,
    delivered: Decimal,
    variance: Decimal,
    limit: Decimal,
) -> str:
    return (
        f"{gap.campaign_name}: no delivery was reported from {gap.first_day.isoformat()} to "
        f"{gap.last_day.isoformat()}. {slot_text(slot)}: invoiced {eur(invoiced)} is "
        f"{eur(variance)} above delivered spend of {eur(delivered)}, within the {eur(limit)} "
        "that the missing days can account for, so this is not treated as an overcharge."
    )


def duplicate_explanation(copy: Invoice, original: Invoice) -> str:
    return (
        f"Invoice {copy.number} from {copy.vendor} repeats invoice {original.number}: same "
        f"client, period and net amount of {eur(copy.net)}, under a different number."
    )


def needs_matching_explanation(invoice: Invoice, line: Line, reason: str) -> str:
    ref = line.campaign_ref or "no campaign code"
    return (
        f"Invoice {invoice.number} from {invoice.vendor}, line {line.line_no} ({ref}, "
        f"{eur(line.amount)}): {reason}."
    )


def budget_explanation(slot: Slot, planned: Decimal, delivered: Decimal) -> str:
    direction = "above" if delivered > planned else "below"
    return (
        f"{slot_text(slot)}: delivered spend of {eur(delivered)} is "
        f"{eur(abs(delivered - planned))}{percent_text(delivered - planned, planned)} {direction} "
        f"the insertion order budget of {eur(planned)}."
    )


def client_month_text(client: str, period: date) -> str:
    return f"{client} {month_text(period)}"


def fee_explanation(
    client: str,
    period: date,
    terms: BillingTerms,
    base: Decimal,
    invoiced_fee: Decimal,
    expected_fee: Decimal,
) -> str:
    difference = invoiced_fee - expected_fee
    if terms.model == BILLING_MARKUP:
        contract = (
            f"{terms.markup_pct}% of the media it bills net of the credits it applies "
            f"({eur(base)}) is {eur(expected_fee)}"
        )
        fee = "commission"
    else:
        contract = f"the contract management fee is {eur(expected_fee)}"
        fee = "management fee"
    return (
        f"{client_month_text(client, period)}: the {fee} on the client invoice is "
        f"{eur(invoiced_fee)}, but {contract}; the invoice is {eur(abs(difference))} "
        f"{'too high' if difference > 0 else 'too low'}."
    )


def credit_explanation(
    slot: Slot,
    terms: BillingTerms,
    notes: list[str],
    expected: Decimal,
    applied: Decimal,
) -> str:
    missing = expected - applied
    given = f"credit notes {', '.join(notes)} total {eur(expected)}" if notes else "no credit notes"
    text = (
        f"{slot_text(slot)}: the platform issued {given}, and the client invoice applies "
        f"{eur(applied)} of credit"
    )
    if missing < 0:
        return text + f", {eur(-missing)} more than the platform gave."
    text += f", so {eur(missing)} of credit is not passed on to the client."
    if terms.model == BILLING_MARKUP:
        text += f" The {terms.markup_pct}% commission is also charged on that amount."
    return text
