"""Pacing rules on small, hand-built orders; no database."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from app.reconciliation import templates
from app.reconciliation.config import (
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
    PacingDraft,
    PacingOrder,
    ZeroSpendRun,
)
from app.reconciliation.pacing import (
    flight_days,
    pace_days,
    pace_order,
    pacing_key,
    pacing_status,
    recent_average,
    run_pacing,
)

JUNE = date(2026, 6, 1)  # A Monday; June has 30 days.
JUNE_END = date(2026, 6, 30)
BUDGET = "3000.00"


def june(day: int) -> date:
    return JUNE + timedelta(days=day - 1)


def order(
    spends: dict[date, str] | None = None,
    budget: str = BUDGET,
    start: date = JUNE,
    end: date = JUNE_END,
    campaign_count: int = 1,
    zero_spend_runs: tuple[ZeroSpendRun, ...] = (),
) -> PacingOrder:
    return PacingOrder(
        client="AAA",
        platform="meta",
        campaign_ref="AAA-META-2606",
        start_date=start,
        end_date=end,
        budget=Decimal(budget),
        campaign_count=campaign_count,
        spend={day: Decimal(spend) for day, spend in (spends or {}).items()},
        zero_spend_runs=zero_spend_runs,
    )


def july(day: int) -> date:
    return date(2026, 7, day)


def flat(spend: str, days: int, start: date = JUNE) -> dict[date, str]:
    return {start + timedelta(days=offset): spend for offset in range(days)}


def draft_for(test_order: PacingOrder, as_of: date) -> PacingDraft | None:
    drafts = run_pacing([test_order], as_of).drafts
    assert len(drafts) <= 1
    return drafts[0] if drafts else None


# Status thresholds: a flat spend s over 10 days of a 30-day, EUR 3,000 flight forecasts 30 × s.


@pytest.mark.parametrize(
    ("ratio", "status"),
    [
        (PACING_GREEN_MIN, PACING_GREEN),
        (PACING_GREEN_MAX, PACING_GREEN),
        (PACING_YELLOW_MIN, PACING_YELLOW),
        (PACING_YELLOW_MAX, PACING_YELLOW),
        (Decimal("1.00"), PACING_GREEN),
        (Decimal("0.9799"), PACING_YELLOW),
        (Decimal("1.1501"), PACING_YELLOW),
        (Decimal("0.8499"), PACING_RED),
        (Decimal("1.2501"), PACING_RED),
    ],
)
def test_every_bound_belongs_to_the_milder_status(ratio: Decimal, status: str) -> None:
    assert pacing_status(ratio) == status


@pytest.mark.parametrize(
    ("spend", "status"),
    [
        ("98.00", PACING_GREEN),
        ("97.99", PACING_YELLOW),
        ("115.00", PACING_GREEN),
        ("115.01", PACING_YELLOW),
        ("85.00", PACING_YELLOW),
        ("84.99", PACING_RED),
        ("125.00", PACING_YELLOW),
        ("125.01", PACING_RED),
    ],
)
def test_forecast_exactly_at_and_one_cent_past_each_bound(spend: str, status: str) -> None:
    result = pace_order(order(flat(spend, 10)), june(10))
    assert result.current is not None
    assert result.current.forecast == Decimal(spend) * 30
    assert result.current.status == status


@pytest.mark.parametrize(
    ("spend", "issue_type", "severity"),
    [
        ("80.00", templates.PACING_UNDERPACING, "high"),
        ("90.00", templates.PACING_UNDERPACING, "medium"),
        ("120.00", templates.PACING_OVERPACING, "medium"),
        ("130.00", templates.PACING_OVERPACING, "high"),
    ],
)
def test_a_running_red_or_yellow_flight_gets_an_issue(
    spend: str, issue_type: str, severity: str
) -> None:
    draft = draft_for(order(flat(spend, 10)), june(10))
    assert draft is not None
    issue = draft.issue
    assert (issue.type, issue.severity) == (issue_type, severity)
    assert issue.dedup_key == "pacing:AAA:meta:AAA-META-2606:2026-06"
    assert (issue.client, issue.platform, issue.period) == ("AAA", "meta", JUNE)
    assert (issue.entity_type, issue.entity_id) == ("insertion_order", "AAA-META-2606")
    assert draft.quality_key is None


def test_a_running_green_flight_gets_no_issue() -> None:
    assert draft_for(order(flat("100.00", 10)), june(10)) is None


def test_a_running_amount_is_the_gap_to_plan_on_the_as_of_date() -> None:
    result = pace_order(order(flat("130.00", 10)), june(10))
    draft = draft_for(order(flat("130.00", 10)), june(10))
    assert result.current is not None and draft is not None
    assert draft.issue.amount == templates.cents(result.current.gap)
    assert draft.issue.amount > 0


def test_the_key_never_depends_on_status_or_amount() -> None:
    yellow = draft_for(order(flat("90.00", 10)), june(10))
    red = draft_for(order(flat("130.00", 12)), june(12))
    assert yellow is not None and red is not None
    assert yellow.issue.dedup_key == red.issue.dedup_key == pacing_key(order())


# Fewer than 7 days.


def test_the_forecast_averages_the_days_available_before_a_week() -> None:
    spends = {june(1): "90.00", june(2): "120.00", june(3): "150.00"}
    result = pace_order(order(spends), june(3))
    assert result.current is not None
    assert result.current.elapsed_days == 3
    assert result.current.forecast == Decimal("360.00") + Decimal("120.00") * 27


def test_the_forecast_uses_only_the_last_seven_days() -> None:
    spends = {**flat("500.00", 3), **flat("100.00", 7, start=june(4))}
    result = pace_order(order(spends), june(10))
    assert result.current is not None
    assert result.current.forecast == Decimal("2200.00") + Decimal("100.00") * 20


def test_no_issue_before_the_minimum_day_and_one_on_it() -> None:
    red = order(flat("200.00", PACING_MIN_DAYS_FOR_ISSUE))
    before = june(PACING_MIN_DAYS_FOR_ISSUE - 1)
    assert pace_order(red, before).current is not None
    assert pace_order(red, before).current.status == PACING_RED  # type: ignore[union-attr]
    assert draft_for(red, before) is None
    assert draft_for(red, june(PACING_MIN_DAYS_FOR_ISSUE)) is not None


# Zero and missing spend.


def test_zero_spend_in_the_last_week_forecasts_the_actual() -> None:
    spends = {**flat("100.00", 5), **flat("0.00", PACING_FORECAST_WINDOW_DAYS, start=june(6))}
    result = pace_order(order(spends), june(12))
    assert result.current is not None
    assert result.current.forecast == result.current.cumulative_actual == Decimal("500.00")
    assert result.current.status == PACING_RED


def test_a_missing_day_is_not_counted_as_zero() -> None:
    spends = flat("100.00", 10)
    del spends[june(9)]
    test_order = order(spends)
    assert recent_average(test_order, june(10), 10) == Decimal("100.00")


def test_no_reported_day_in_the_window_averages_zero() -> None:
    test_order = order(flat("100.00", 2))
    assert recent_average(test_order, june(12), 12) == 0


# The planted pacing drift: the whole budget in the first 60% of the month.


def drift(final_total: str = BUDGET) -> PacingOrder:
    active = 18  # 60% of 30 days.
    per_day = Decimal(final_total) / active
    spends = {june(day): str(per_day) for day in range(1, active + 1)}
    spends.update({june(day): "0.00" for day in range(active + 1, 31)})
    runs = (ZeroSpendRun(june(active + 1), JUNE_END, "quality:zero_spend:meta:1001:c1:x:y"),)
    return order(spends, zero_spend_runs=runs)


def test_a_drift_is_red_mid_month_and_green_at_its_end() -> None:
    result = pace_order(drift(), JUNE_END)
    assert result.phase == PHASE_CLOSED
    assert result.current is not None and result.worst is not None
    assert result.current.status == PACING_GREEN
    assert result.worst.status == PACING_RED
    assert result.worst.elapsed_days >= PACING_MIN_DAYS_FOR_ISSUE


def test_a_closed_drift_gets_one_red_issue_with_the_peak_gap() -> None:
    result = pace_order(drift(), JUNE_END)
    draft = draft_for(drift(), JUNE_END)
    assert draft is not None
    assert (draft.issue.type, draft.issue.severity) == (templates.PACING_OVERPACING, "high")
    assert draft.issue.amount == templates.cents(result.peak_gap)
    peak = max(day.gap for day in pace_days(drift(), JUNE_END))
    assert result.peak_gap == peak > 0
    assert "2026-06-19 to 2026-06-30" in draft.issue.explanation


def test_a_drift_takes_over_the_quality_zero_spend_issue() -> None:
    draft = draft_for(drift(), JUNE_END)
    assert draft is not None
    assert draft.quality_key == "quality:zero_spend:meta:1001:c1:x:y"


def test_a_zero_spend_run_after_the_as_of_date_is_not_taken_over() -> None:
    draft = draft_for(drift(), june(12))
    assert draft is not None
    assert draft.quality_key is None


def test_a_closed_drift_whose_total_misses_the_budget_is_left_to_rule_2() -> None:
    assert draft_for(drift(final_total="3600.00"), JUNE_END) is None


def test_a_closed_flight_with_a_yellow_end_gets_no_issue() -> None:
    result = pace_order(order(flat("93.00", 30)), JUNE_END)
    assert result.current is not None and result.current.status == PACING_YELLOW
    assert draft_for(order(flat("93.00", 30)), JUNE_END) is None


def test_a_closed_flight_with_only_yellow_days_gets_no_issue() -> None:
    spends = {**flat("90.00", 10), **flat("105.00", 20, start=june(11))}
    result = pace_order(order(spends), JUNE_END)
    assert result.worst is not None and result.worst.status == PACING_YELLOW
    assert draft_for(order(spends), JUNE_END) is None


def test_a_running_flight_keeps_an_earlier_red_after_it_turns_green() -> None:
    spends = {**flat("200.00", 7), **flat("70.00", 8, start=june(8))}
    result = pace_order(order(spends), june(15))
    assert result.current is not None and result.current.status != PACING_RED
    draft = draft_for(order(spends), june(15))
    assert draft is not None and draft.issue.severity == "high"


# Dates.


def test_the_cumulative_plan_is_the_budget_on_the_last_day() -> None:
    days = pace_days(order(flat("100.00", 30)), JUNE_END)
    assert days[-1].cumulative_plan == Decimal(BUDGET)


def test_the_daily_plan_follows_the_weekday_weights() -> None:
    days = pace_days(order(flat("100.00", 30)), JUNE_END)
    total = sum(PACING_WEEKDAY_WEIGHTS[d.weekday()] for d in flight_days(order()))
    monday, saturday = days[0].cumulative_plan, days[5].cumulative_plan - days[4].cumulative_plan
    assert monday == Decimal(BUDGET) * PACING_WEEKDAY_WEIGHTS[0] / total
    assert saturday == pytest.approx(Decimal(BUDGET) * PACING_WEEKDAY_WEIGHTS[5] / total)


def test_a_mid_month_flight_paces_over_its_own_dates() -> None:
    mid = order(flat("100.00", 10, start=june(11)), budget="1500.00", start=june(11), end=june(25))
    result = pace_order(mid, june(20))
    assert result.current is not None
    assert result.current.elapsed_days == 10
    assert result.current.forecast == Decimal("1000.00") + Decimal("100.00") * 5
    assert pace_days(mid, june(25))[-1].cumulative_plan == Decimal("1500.00")


def test_spend_outside_the_flight_is_ignored() -> None:
    spends = {date(2026, 5, 31): "999.00", **flat("100.00", 10)}
    result = pace_order(order(spends), june(10))
    assert result.current is not None
    assert result.current.cumulative_actual == Decimal("1000.00")


def test_an_order_spanning_months_is_paced_over_its_whole_flight() -> None:
    spanning = order(
        flat("100.00", 20, start=june(21)),
        budget="4000.00",
        start=june(21),
        end=date(2026, 7, 31),
    )
    result = pace_order(spanning, date(2026, 7, 10))
    assert result.phase == PHASE_RUNNING
    assert result.current is not None
    assert result.current.forecast == Decimal("2000.00") + Decimal("100.00") * 21


def test_a_closed_drift_spanning_months_is_raised_even_off_budget() -> None:
    spends = {**flat("200.00", 15, start=june(16)), **flat("0.00", 46, start=july(1))}
    spanning = order(spends, budget="2000.00", start=june(16), end=date(2026, 8, 15))
    draft = draft_for(spanning, date(2026, 8, 15))
    assert draft is not None
    assert draft.issue.dedup_key.endswith(":2026-06")


def test_before_the_flight_nothing_is_paced() -> None:
    result = pace_order(order(flat("100.00", 5)), date(2026, 5, 31))
    assert result.phase == PHASE_NOT_STARTED
    assert (result.current, result.worst) == (None, None)
    assert draft_for(order(flat("100.00", 5)), date(2026, 5, 31)) is None


def test_after_the_flight_it_is_closed_at_its_last_day() -> None:
    result = pace_order(order(flat("100.00", 30)), date(2026, 7, 15))
    assert result.phase == PHASE_CLOSED
    assert result.current is not None and result.current.day == JUNE_END


def test_the_last_day_itself_is_closed() -> None:
    assert pace_order(order(flat("100.00", 30)), JUNE_END).phase == PHASE_CLOSED
    assert pace_order(order(flat("100.00", 29)), june(29)).phase == PHASE_RUNNING


# Orders that cannot be paced.


@pytest.mark.parametrize(
    "test_order",
    [order(flat("10.00", 10), campaign_count=0), order(flat("10.00", 10), budget="0.00")],
)
def test_an_unlinked_or_unbudgeted_order_is_skipped(test_order: PacingOrder) -> None:
    outcome = run_pacing([test_order], june(10))
    assert (outcome.results, outcome.drafts, outcome.skipped) == ([], [], [test_order])


def test_run_pacing_records_the_date_it_was_given() -> None:
    assert run_pacing([], june(10)).as_of == june(10)


def test_every_pacing_type_has_an_action_and_every_alert_a_severity() -> None:
    assert templates.PACING_ACTIONS.keys() == {
        templates.PACING_OVERPACING,
        templates.PACING_UNDERPACING,
    }
    assert templates.PACING_SEVERITIES.keys() == {PACING_YELLOW, PACING_RED}
