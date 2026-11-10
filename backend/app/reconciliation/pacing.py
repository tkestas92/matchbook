"""Pacing per insertion order: pure functions over plain dataclasses, as of an explicit date."""

from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal

from app.reconciliation import templates
from app.reconciliation.config import (
    BUDGET_MAX_SHARE,
    BUDGET_MIN_SHARE,
    PACING_FORECAST_WINDOW_DAYS,
    PACING_GREEN_MAX,
    PACING_GREEN_MIN,
    PACING_MIN_DAYS_FOR_ISSUE,
    PACING_WEEKDAY_WEIGHTS,
    PACING_YELLOW_MAX,
    PACING_YELLOW_MIN,
)
from app.reconciliation.models import (
    PACING_GREEN,
    PACING_RED,
    PACING_YELLOW,
    PHASE_CLOSED,
    PHASE_NOT_STARTED,
    PHASE_RUNNING,
    IssueDraft,
    PacingDay,
    PacingDraft,
    PacingOrder,
    PacingOutcome,
    PacingResult,
    ZeroSpendRun,
    month_start,
)

ZERO = Decimal(0)
STATUS_RANK = {PACING_GREEN: 0, PACING_YELLOW: 1, PACING_RED: 2}


def pacing_status(ratio: Decimal) -> str:
    """Forecast as a share of budget; every bound belongs to the milder status."""
    if PACING_GREEN_MIN <= ratio <= PACING_GREEN_MAX:
        return PACING_GREEN
    if PACING_YELLOW_MIN <= ratio <= PACING_YELLOW_MAX:
        return PACING_YELLOW
    return PACING_RED


def flight_days(order: PacingOrder) -> list[date]:
    count = (order.end_date - order.start_date).days + 1
    return [order.start_date + timedelta(days=offset) for offset in range(count)]


def day_weight(day: date) -> Decimal:
    return PACING_WEEKDAY_WEIGHTS[day.weekday()]


def recent_average(order: PacingOrder, day: date, elapsed_days: int) -> Decimal:
    """Average over the window's reported days; a missing day is unknown, not zero."""
    window = min(PACING_FORECAST_WINDOW_DAYS, elapsed_days)
    reported = [
        order.spend[earlier]
        for earlier in (day - timedelta(days=offset) for offset in range(window))
        if earlier in order.spend
    ]
    if not reported:
        return ZERO
    return sum(reported, ZERO) / len(reported)


def pace_days(order: PacingOrder, as_of: date) -> list[PacingDay]:
    """The pace at the end of every flight day up to `as_of`."""
    days = flight_days(order)
    total_weight = sum((day_weight(day) for day in days), ZERO)
    weight_to_date = actual = ZERO
    paced: list[PacingDay] = []
    for elapsed, day in enumerate(days, start=1):
        if day > as_of:
            break
        weight_to_date += day_weight(day)
        actual += order.spend.get(day, ZERO)
        forecast = actual + recent_average(order, day, elapsed) * (len(days) - elapsed)
        ratio = forecast / order.budget
        paced.append(
            PacingDay(
                day=day,
                elapsed_days=elapsed,
                cumulative_plan=order.budget * weight_to_date / total_weight,
                cumulative_actual=actual,
                forecast=forecast,
                ratio=ratio,
                status=pacing_status(ratio),
            )
        )
    return paced


def severity_key(day: PacingDay) -> tuple[int, Decimal]:
    return STATUS_RANK[day.status], abs(day.ratio - 1)


def phase(order: PacingOrder, as_of: date) -> str:
    if as_of < order.start_date:
        return PHASE_NOT_STARTED
    if as_of < order.end_date:
        return PHASE_RUNNING
    return PHASE_CLOSED


def pace_order(order: PacingOrder, as_of: date) -> PacingResult:
    days = pace_days(order, as_of)
    countable = [day for day in days if day.elapsed_days >= PACING_MIN_DAYS_FOR_ISSUE]
    return PacingResult(
        order=order,
        phase=phase(order, as_of),
        current=days[-1] if days else None,
        worst=max(countable, key=severity_key, default=None),
        peak_gap=max((day.gap for day in days), key=abs, default=ZERO),
    )


def is_within_budget_rule(order: PacingOrder, final: Decimal) -> bool:
    """Whether reconciliation rule 2 stays quiet about this order's final total."""
    return order.budget * BUDGET_MIN_SHARE <= final <= order.budget * BUDGET_MAX_SHARE


def spans_months(order: PacingOrder) -> bool:
    return month_start(order.start_date) != month_start(order.end_date)


def flagged_day(result: PacingResult) -> PacingDay | None:
    """The day an issue is about, or None when there is no issue to raise."""
    current, worst = result.current, result.worst
    if current is None or worst is None:
        return None
    red_peak = worst if worst.status == PACING_RED else None
    if result.phase == PHASE_RUNNING:
        if current.status != PACING_GREEN and (red_peak is None or current.status == PACING_RED):
            return current
        return red_peak
    # A closed flight's final total is rule 2's; only a red trajectory it cannot see is raised.
    if red_peak is None:
        return None
    if spans_months(result.order) or is_within_budget_rule(result.order, current.cumulative_actual):
        return red_peak
    return None


def first_zero_spend(order: PacingOrder, as_of: date) -> ZeroSpendRun | None:
    runs = [
        run
        for run in order.zero_spend_runs
        if order.start_date <= run.first_day <= min(as_of, order.end_date)
    ]
    return min(runs, key=lambda run: run.first_day, default=None)


def pacing_key(order: PacingOrder) -> str:
    """Never holds a status or an amount, so a re-run updates the same issue."""
    month = templates.month_text(order.start_date)
    return f"pacing:{order.client}:{order.platform}:{order.campaign_ref}:{month}"


def pacing_draft(result: PacingResult, as_of: date) -> PacingDraft | None:
    day = flagged_day(result)
    if day is None or result.current is None:
        return None
    order = result.order
    issue_type = (
        templates.PACING_OVERPACING if day.forecast > order.budget else templates.PACING_UNDERPACING
    )
    amount = result.peak_gap if result.phase == PHASE_CLOSED else result.current.gap
    zero_spend = first_zero_spend(order, as_of)
    issue = IssueDraft(
        dedup_key=pacing_key(order),
        type=issue_type,
        severity=templates.PACING_SEVERITIES[day.status],
        client=order.client,
        platform=order.platform,
        period=month_start(order.start_date),
        entity_type="insertion_order",
        entity_id=order.campaign_ref,
        amount=templates.cents(amount),
        explanation=templates.pacing_explanation(result, day, zero_spend),
        proposed_action=templates.PACING_ACTIONS[issue_type],
    )
    return PacingDraft(issue, None if zero_spend is None else zero_spend.issue_key)


def can_pace(order: PacingOrder) -> bool:
    return order.campaign_count > 0 and order.budget > 0


def run_pacing(orders: Sequence[PacingOrder], as_of: date) -> PacingOutcome:
    """Every order's pace on `as_of`; the caller chooses the date, never the clock."""
    results: list[PacingResult] = []
    drafts: list[PacingDraft] = []
    skipped: list[PacingOrder] = []
    for order in orders:
        if not can_pace(order):
            skipped.append(order)
            continue
        result = pace_order(order, as_of)
        results.append(result)
        draft = pacing_draft(result, as_of)
        if draft is not None:
            drafts.append(draft)
    return PacingOutcome(as_of=as_of, results=results, drafts=drafts, skipped=skipped)
