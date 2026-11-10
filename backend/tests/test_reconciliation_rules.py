"""Reconciliation rules on small, hand-built inputs; no database."""

from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from app import reconciliation
from app.reconciliation import templates
from app.reconciliation.client_billing import expected_fee, split_by_weight
from app.reconciliation.config import (
    BUDGET_MAX_SHARE,
    BUDGET_MIN_SHARE,
    CLIENT_INVOICE_MIN_EUR,
    MISSING_DAY_SPEND_FACTOR,
    VARIANCE_MIN_EUR,
    VARIANCE_MIN_SHARE,
    missing_days_limit,
)
from app.reconciliation.models import (
    BillingTerms,
    BoundaryShift,
    Budget,
    CreditNote,
    DeliveryGap,
    DeliveryRow,
    Invoice,
    IssueDraft,
    Line,
    PlanOrder,
    ReconciliationInput,
    ReconciliationOutcome,
    Slot,
    SlotResult,
)
from app.reconciliation.rules import (
    NO_CAMPAIGN,
    NO_CODE,
    NO_ORDER,
    SEVERAL_CAMPAIGNS,
    SEVERAL_ORDERS,
    gap_days_in,
    is_material,
    link_lines,
    run_rules,
    split_duplicates,
)

VILNIUS = "Europe/Vilnius"
JUNE = date(2026, 6, 1)
JULY = date(2026, 7, 1)
SLOT = Slot("AAA", "meta", JUNE)
MARKUP = BillingTerms("markup", markup_pct=Decimal("15.00"))
CLIENT_OWNED = BillingTerms("client_owned")
JULY_SLOT = Slot("AAA", "meta", JULY)


def money(value: str) -> Decimal:
    return Decimal(value)


def delivery(period: date, spends: Sequence[str], **overrides: Any) -> list[DeliveryRow]:
    """One row per spend on consecutive days, starting on the 1st of `period`."""
    values: dict[str, Any] = {"client": "AAA", "platform": "meta", "campaign_id": "c1"}
    values.update(overrides)
    return [
        DeliveryRow(day=period + timedelta(days=offset), spend=money(spend), **values)
        for offset, spend in enumerate(spends)
    ]


def invoice(amount: str, **overrides: Any) -> Invoice:
    values: dict[str, Any] = {
        "kind": "platform",
        "vendor": "Meta",
        "number": "INV-1",
        "client": "AAA",
        "platform": "meta",
        "issue_date": date(2026, 7, 2),
        "period_start": JUNE,
        "period_end": date(2026, 6, 30),
        "lines": (Line(1, 1, "AAA-META-2606", money(amount)),),
    }
    return Invoice(**{**values, **overrides})


def shift(spend: str = "100.00", **overrides: Any) -> BoundaryShift:
    values: dict[str, Any] = {
        "client": "AAA",
        "platform": "meta",
        "timezone": "UTC",
        "campaign_id": "c1",
        "campaign_name": "Campaign [AAA-META-2606]",
        "missing_day": date(2026, 6, 30),
        "row_day": JULY,
        "spend": money(spend),
        "issue_key": "quality:month_boundary_shift:key",
    }
    return BoundaryShift(**{**values, **overrides})


def gap(first: date, last: date, **overrides: Any) -> DeliveryGap:
    values: dict[str, Any] = {
        "client": "AAA",
        "platform": "meta",
        "campaign_id": "c1",
        "campaign_name": "Campaign [AAA-META-2606]",
        "first_day": first,
        "last_day": last,
        "issue_key": "quality:missing_days:key",
    }
    return DeliveryGap(**{**values, **overrides})


def order(**overrides: Any) -> PlanOrder:
    values: dict[str, Any] = {
        "client": "AAA",
        "platform": "meta",
        "campaign_ref": "AAA-META-2606",
        "campaign_ids": (7,),
    }
    return PlanOrder(**{**values, **overrides})


def outcome(**overrides: Any) -> ReconciliationOutcome:
    values: dict[str, Any] = {
        "agency_timezone": VILNIUS,
        "billing": {"AAA": MARKUP},
        "delivery": [],
        "shifts": [],
        "gaps": [],
        "invoices": [],
        "orders": [order()],
        "budgets": [],
        "credit_notes": [],
    }
    return run_rules(ReconciliationInput(**{**values, **overrides}))


def types(result: ReconciliationOutcome) -> list[str]:
    return [issue.type for issue in result.issues]


def only_issue(result: ReconciliationOutcome) -> IssueDraft:
    assert len(result.issues) == 1
    return result.issues[0]


def result_for(result: ReconciliationOutcome, slot: Slot) -> SlotResult:
    return next(item for item in result.results if item.slot == slot)


# --- config -------------------------------------------------------------------------------


def test_thresholds_are_the_agreed_values() -> None:
    assert VARIANCE_MIN_EUR == money("1.00")
    assert VARIANCE_MIN_SHARE == money("0.005")
    assert MISSING_DAY_SPEND_FACTOR == 1


def test_missing_days_limit_is_gap_days_times_highest_daily_spend() -> None:
    assert missing_days_limit(3, money("100.50")) == money("301.50")


# --- materiality ---------------------------------------------------------------------------


def test_exactly_one_euro_is_not_material() -> None:
    assert not is_material(money("1.00"), money("10.00"))


def test_just_above_one_euro_on_a_small_base_is_material() -> None:
    assert is_material(money("1.01"), money("10.00"))


def test_exactly_half_a_percent_is_not_material() -> None:
    assert not is_material(money("5.00"), money("1000.00"))


def test_just_above_half_a_percent_is_material() -> None:
    assert is_material(money("5.01"), money("1000.00"))


def test_above_one_euro_but_below_half_a_percent_is_not_material() -> None:
    assert not is_material(money("2.00"), money("1000.00"))


def test_zero_base_needs_only_the_euro_test() -> None:
    assert is_material(money("1.01"), money("0.00"))
    assert not is_material(money("1.00"), money("0.00"))


def test_negative_variance_is_measured_by_size() -> None:
    assert is_material(money("-6.00"), money("1000.00"))


# --- rule 1: invoice vs delivered ----------------------------------------------------------


def test_invoice_above_delivery_is_an_overcharge() -> None:
    result = outcome(delivery=delivery(JUNE, ["100.00"]), invoices=[invoice("110.00")])
    issue = only_issue(result)
    assert issue.type == templates.OVERCHARGE
    assert issue.amount == money("10.00")
    assert (issue.client, issue.platform, issue.period) == ("AAA", "meta", JUNE)
    assert "EUR 110.00" in issue.explanation
    assert issue.proposed_action
    item = result_for(result, SLOT)
    assert (item.delivered, item.invoiced, item.variance, item.status) == (
        money("100.00"),
        money("110.00"),
        money("10.00"),
        "issue",
    )


def test_invoice_below_delivery_is_an_undercharge() -> None:
    issue = only_issue(outcome(delivery=delivery(JUNE, ["100.00"]), invoices=[invoice("90.00")]))
    assert issue.type == templates.UNDERCHARGE
    assert issue.amount == money("-10.00")


def test_variance_within_thresholds_is_ok() -> None:
    result = outcome(
        delivery=delivery(JUNE, ["1000.00"]),
        invoices=[invoice("1004.00")],
        billing={"AAA": CLIENT_OWNED},
    )
    assert result.issues == []
    assert result_for(result, SLOT).status == "ok"


def test_delivery_is_summed_by_calendar_month() -> None:
    rows = [*delivery(date(2026, 6, 30), ["100.00"]), *delivery(JULY, ["500.00"])]
    result = outcome(delivery=rows, invoices=[invoice("100.00")])
    assert result_for(result, SLOT).delivered == money("100.00")
    assert result.issues == []


def test_year_end_slots_are_separate() -> None:
    rows = [*delivery(date(2026, 12, 31), ["100.00"]), *delivery(date(2027, 1, 1), ["70.00"])]
    result = outcome(delivery=rows)
    assert {item.slot.period: item.delivered for item in result.results} == {
        date(2026, 12, 1): money("100.00"),
        date(2027, 1, 1): money("70.00"),
    }


def test_two_different_invoices_for_one_month_are_summed() -> None:
    result = outcome(
        delivery=delivery(JUNE, ["150.00"]),
        invoices=[invoice("100.00"), invoice("50.00", number="INV-2")],
    )
    assert result.issues == []
    assert result_for(result, SLOT).invoiced == money("150.00")


def test_client_invoices_are_not_compared_with_delivery() -> None:
    client_invoice = invoice("500.00", kind="client", platform=None, vendor="Agency")
    result = outcome(
        delivery=delivery(JUNE, ["100.00"]),
        invoices=[client_invoice],
        billing={"AAA": CLIENT_OWNED},
    )
    assert result.issues == []
    assert result_for(result, SLOT).invoiced is None


def test_variance_key_ignores_the_classification() -> None:
    over = only_issue(outcome(delivery=delivery(JUNE, ["100.00"]), invoices=[invoice("110.00")]))
    under = only_issue(outcome(delivery=delivery(JUNE, ["100.00"]), invoices=[invoice("90.00")]))
    assert over.dedup_key == under.dedup_key == "reconciliation:invoice_variance:AAA:meta:2026-06"


# --- rule 1: spend dated across a month end ------------------------------------------------


def shifted(spend: str = "100.00", **shift_overrides: Any) -> dict[str, Any]:
    """June's last day (100.00) is dated July 1st; both invoices bill what really happened."""
    return {
        "delivery": [*delivery(JUNE, ["500.00"]), *delivery(JULY, [spend, "400.00"])],
        "shifts": [shift(spend, **shift_overrides)],
        "invoices": [
            invoice("600.00"),
            invoice(
                "400.00",
                number="INV-2",
                period_start=JULY,
                period_end=date(2026, 7, 31),
                lines=(Line(2, 1, "AAA-META-2606", money("400.00")),),
            ),
        ],
    }


def test_shift_on_a_foreign_timezone_account_is_a_timezone_shift() -> None:
    issue = only_issue(outcome(**shifted()))
    assert issue.type == templates.TIMEZONE_SHIFT
    assert issue.amount == money("100.00")
    assert issue.period == JUNE
    assert "UTC" in issue.explanation
    assert "not an overcharge" in issue.explanation


def test_shift_on_an_agency_timezone_account_does_not_claim_a_timezone_cause() -> None:
    issue = only_issue(outcome(**shifted(timezone=VILNIUS)))
    assert issue.type == templates.MONTH_BOUNDARY_SHIFT
    assert "does not explain" in issue.explanation


def test_shift_uses_the_quality_issue_key() -> None:
    assert only_issue(outcome(**shifted())).dedup_key == "quality:month_boundary_shift:key"


def test_next_month_opposite_variance_makes_no_second_issue() -> None:
    result = outcome(**shifted())
    assert len(result.issues) == 1
    assert result_for(result, JULY_SLOT).status == "issue"
    assert result_for(result, JULY_SLOT).variance == money("-100.00")


def test_stored_variance_is_not_corrected_for_the_shift() -> None:
    item = result_for(outcome(**shifted()), SLOT)
    assert (item.delivered, item.variance) == (money("500.00"), money("100.00"))


def test_shift_across_the_year_end() -> None:
    december = date(2026, 12, 1)
    moved = shift(missing_day=date(2026, 12, 31), row_day=date(2027, 1, 1))
    issue = only_issue(
        outcome(
            delivery=delivery(december, ["500.00"]),
            shifts=[moved],
            invoices=[
                invoice("600.00", period_start=december, period_end=date(2026, 12, 31)),
            ],
        )
    )
    assert issue.type == templates.TIMEZONE_SHIFT
    assert issue.period == december


def test_variance_left_after_the_shift_is_a_second_issue() -> None:
    inputs = shifted()
    inputs["invoices"][0] = invoice("700.00")
    result = outcome(**inputs)
    assert sorted(types(result)) == sorted([templates.TIMEZONE_SHIFT, templates.OVERCHARGE])
    overcharge = next(i for i in result.issues if i.type == templates.OVERCHARGE)
    assert overcharge.amount == money("100.00")
    assert "EUR 100.00" in overcharge.explanation
    assert "month end" in overcharge.explanation


def test_shift_that_does_not_reduce_the_variance_is_ignored() -> None:
    issue = only_issue(
        outcome(
            delivery=delivery(JUNE, ["500.00"]),
            shifts=[shift("100.00")],
            invoices=[invoice("450.00")],
        )
    )
    assert issue.type == templates.UNDERCHARGE
    assert issue.amount == money("-50.00")


def test_shift_below_the_thresholds_makes_no_reconciliation_issue() -> None:
    result = outcome(
        delivery=delivery(JUNE, ["1000.00"]),
        shifts=[shift("3.00")],
        invoices=[invoice("1003.00")],
    )
    assert result.issues == []


def test_shift_with_no_next_month_invoice_leaves_that_month_incomplete() -> None:
    inputs = shifted()
    inputs["invoices"] = inputs["invoices"][:1]
    result = outcome(**inputs)
    assert only_issue(result).type == templates.TIMEZONE_SHIFT
    assert result_for(result, JULY_SLOT).status == "incomplete"


def test_real_overcharge_of_one_day_size_is_not_taken_for_a_shift() -> None:
    issue = only_issue(
        outcome(delivery=delivery(JUNE, ["500.00", "120.00"]), invoices=[invoice("700.00")])
    )
    assert issue.type == templates.OVERCHARGE


# --- rule 1: missing delivery days ---------------------------------------------------------


def with_gap(invoiced: str, days: int = 2) -> dict[str, Any]:
    """Highest daily spend is 100.00, so `days` missing days can account for days × 100.00."""
    return {
        "delivery": delivery(JUNE, ["100.00", "80.00"]),
        "gaps": [gap(date(2026, 6, 3), date(2026, 6, 2 + days))],
        "invoices": [invoice(invoiced)],
    }


def test_variance_within_the_gap_limit_goes_to_the_quality_issue() -> None:
    result = outcome(**with_gap("330.00"))
    assert result.issues == []
    note = result.gap_notes[0]
    assert (note.issue_key, note.amount) == ("quality:missing_days:key", money("150.00"))
    assert "EUR 200.00" in note.explanation
    assert result_for(result, SLOT).status == "issue"


def test_variance_exactly_at_the_gap_limit_is_absorbed() -> None:
    result = outcome(**with_gap("380.00"))
    assert (result.issues, len(result.gap_notes)) == ([], 1)


def test_variance_above_the_gap_limit_is_an_overcharge() -> None:
    result = outcome(**with_gap("380.01"))
    assert result.gap_notes == []
    issue = only_issue(result)
    assert issue.type == templates.OVERCHARGE
    assert issue.amount == money("200.01")


def test_invoice_below_delivery_is_never_explained_by_missing_days() -> None:
    inputs = with_gap("100.00")
    result = outcome(**inputs)
    assert result.gap_notes == []
    assert only_issue(result).type == templates.UNDERCHARGE


def test_gap_limit_uses_the_gap_campaigns_own_spend() -> None:
    rows = [
        *delivery(JUNE, ["10.00", "10.00"], campaign_id="c1"),
        *delivery(JUNE, ["900.00"], campaign_id="c2"),
    ]
    result = outcome(
        delivery=rows,
        gaps=[gap(date(2026, 6, 3), date(2026, 6, 3))],
        invoices=[invoice("1000.00")],
    )
    assert result.gap_notes == []
    assert only_issue(result).type == templates.OVERCHARGE


def test_gap_days_count_only_inside_the_month() -> None:
    crossing = gap(date(2026, 6, 29), date(2026, 7, 2))
    assert gap_days_in(crossing, JUNE) == 2
    assert gap_days_in(crossing, JULY) == 2
    assert gap_days_in(crossing, date(2026, 8, 1)) == 0


def test_note_goes_to_the_earliest_gap() -> None:
    later = gap(date(2026, 6, 20), date(2026, 6, 20), issue_key="later")
    earlier = gap(date(2026, 6, 3), date(2026, 6, 3), issue_key="earlier")
    result = outcome(
        delivery=delivery(JUNE, ["100.00"]), gaps=[later, earlier], invoices=[invoice("250.00")]
    )
    assert result.gap_notes[0].issue_key == "earlier"


def test_shift_is_removed_before_the_gap_limit_is_applied() -> None:
    result = outcome(
        delivery=delivery(JUNE, ["100.00", "80.00"]),
        shifts=[shift("50.00")],
        gaps=[gap(date(2026, 6, 3), date(2026, 6, 3))],
        invoices=[invoice("330.00")],
    )
    assert types(result) == [templates.TIMEZONE_SHIFT]
    assert result.gap_notes[0].amount == money("100.00")


# --- rule 4: duplicates --------------------------------------------------------------------


def test_duplicate_is_the_later_issued_copy() -> None:
    first = invoice("100.00", number="B", issue_date=date(2026, 7, 2))
    second = invoice("100.00", number="A", issue_date=date(2026, 7, 9))
    kept, duplicates = split_duplicates([second, first])
    assert kept == [first]
    assert (duplicates[0].copy, duplicates[0].original) == (second, first)


def test_missing_issue_date_counts_as_the_copy() -> None:
    dated = invoice("100.00", number="B")
    undated = invoice("100.00", number="A", issue_date=None)
    assert split_duplicates([undated, dated])[0] == [dated]


def test_same_amount_in_another_period_is_not_a_duplicate() -> None:
    other = invoice("100.00", number="B", period_start=JULY, period_end=date(2026, 7, 31))
    assert split_duplicates([invoice("100.00"), other])[1] == []


def test_different_amount_is_not_a_duplicate() -> None:
    assert split_duplicates([invoice("100.00"), invoice("100.01", number="B")])[1] == []


def test_same_amount_for_another_client_is_not_a_duplicate() -> None:
    assert (
        split_duplicates([invoice("100.00"), invoice("100.00", number="B", client="BBB")])[1] == []
    )


def test_duplicate_is_one_issue_and_never_a_variance() -> None:
    copy = invoice("100.00", number="INV-2", issue_date=date(2026, 7, 9))
    result = outcome(delivery=delivery(JUNE, ["100.00"]), invoices=[invoice("100.00"), copy])
    issue = only_issue(result)
    assert issue.type == templates.DUPLICATE_INVOICE
    assert issue.dedup_key == "reconciliation:duplicate_invoice:Meta:INV-2"
    assert issue.amount == money("100.00")
    assert "INV-1" in issue.explanation
    item = result_for(result, SLOT)
    assert (item.invoiced, item.status) == (money("100.00"), "issue")


# --- linking -------------------------------------------------------------------------------


def line(ref: str | None, line_id: int = 1) -> Line:
    return Line(line_id, line_id, ref, money("10.00"))


def reasons(*orders: PlanOrder, ref: str | None = "AAA-META-2606") -> list[str]:
    _, unmatched = link_lines([invoice("10.00", lines=(line(ref),))], orders)
    return [item.reason for item in unmatched]


def test_line_links_to_the_campaign_of_its_order() -> None:
    links, unmatched = link_lines([invoice("10.00", lines=(line("AAA-META-2606", 5),))], [order()])
    assert [(link.line_id, link.campaign_id) for link in links] == [(5, 7)]
    assert unmatched == []


def test_line_without_a_code_is_unmatched() -> None:
    assert reasons(order(), ref=None) == [NO_CODE]


def test_line_with_an_unknown_code_is_unmatched() -> None:
    assert reasons(order(campaign_ref="OTHER")) == [NO_ORDER]


def test_order_of_another_client_does_not_match() -> None:
    assert reasons(order(client="BBB")) == [NO_ORDER]


def test_order_of_another_platform_does_not_match() -> None:
    assert reasons(order(platform="google")) == [NO_ORDER]


def test_code_on_several_orders_is_not_guessed() -> None:
    assert reasons(order(), order(campaign_ids=(8,))) == [SEVERAL_ORDERS]


def test_order_without_a_campaign_is_unmatched() -> None:
    assert reasons(order(campaign_ids=())) == [NO_CAMPAIGN]


def test_order_with_several_campaigns_is_not_guessed() -> None:
    assert reasons(order(campaign_ids=(7, 8))) == [SEVERAL_CAMPAIGNS]


def test_client_invoice_links_without_a_platform_and_skips_the_fee_line() -> None:
    client_invoice = invoice(
        "20.00", kind="client", platform=None, lines=(line("AAA-META-2606", 1), line(None, 2))
    )
    links, unmatched = link_lines([client_invoice], [order()])
    assert [link.line_id for link in links] == [1]
    assert unmatched == []


def test_unmatched_line_is_an_issue() -> None:
    issue = only_issue(
        outcome(
            delivery=delivery(JUNE, ["10.00"]),
            invoices=[invoice("10.00", lines=(line("UNKNOWN", 1),))],
        )
    )
    assert issue.type == templates.NEEDS_MATCHING
    assert issue.dedup_key == "reconciliation:needs_matching:Meta:INV-1:1"
    assert NO_ORDER in issue.explanation


def test_line_of_a_duplicate_is_linked_but_not_reported_twice() -> None:
    copy = invoice(
        "10.00", number="INV-2", issue_date=date(2026, 7, 9), lines=(line("UNKNOWN", 2),)
    )
    original = invoice("10.00", lines=(line("UNKNOWN", 1),))
    result = outcome(delivery=delivery(JUNE, ["10.00"]), invoices=[original, copy])
    assert sorted(types(result)) == sorted([templates.NEEDS_MATCHING, templates.DUPLICATE_INVOICE])


# --- result status -------------------------------------------------------------------------


def test_client_owned_month_without_an_invoice_is_ok() -> None:
    result = outcome(delivery=delivery(JUNE, ["10.00"]), billing={"AAA": CLIENT_OWNED})
    item = result_for(result, SLOT)
    assert (item.invoiced, item.variance, item.status) == (None, None, "ok")


def test_agency_billed_month_without_an_invoice_is_incomplete() -> None:
    result = outcome(delivery=delivery(JUNE, ["10.00"]))
    assert result_for(result, SLOT).status == "incomplete"
    assert result.issues == []


def test_invoice_without_any_delivery_is_incomplete() -> None:
    result = outcome(invoices=[invoice("100.00")])
    item = result_for(result, SLOT)
    assert (item.delivered, item.variance, item.status) == (None, None, "incomplete")
    assert result.issues == []


def test_zero_delivery_is_compared_rather_than_incomplete() -> None:
    result = outcome(delivery=delivery(JUNE, ["0.00"]), invoices=[invoice("5.00")])
    assert only_issue(result).type == templates.OVERCHARGE


# --- templates -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("part", "whole", "expected"),
    [
        ("1.25", "1000.00", " (0.13%)"),  # Half up; half even would give 0.12.
        ("-1.25", "1000.00", " (0.13%)"),
        ("1.00", "0.00", ""),
    ],
)
def test_percent_text_rounds_half_up(part: str, whole: str, expected: str) -> None:
    assert templates.percent_text(money(part), money(whole)) == expected


def test_every_issue_type_has_a_severity_and_an_action() -> None:
    assert templates.SEVERITIES.keys() == templates.PROPOSED_ACTIONS.keys()


def test_rules_are_exact_on_cents() -> None:
    rows = delivery(JUNE, ["0.10", "0.20"])
    result = outcome(delivery=rows, invoices=[invoice("0.30")])
    assert result_for(result, SLOT).variance == 0


def test_sources_never_mention_the_answer_key() -> None:
    package = Path(reconciliation.__file__).parent
    for path in package.glob("*.py"):
        assert "answer_key" not in path.read_text(encoding="utf-8")


# --- rule 2: delivered vs IO budget --------------------------------------------------------

DECEMBER = date(2026, 12, 1)
JANUARY = date(2027, 1, 1)


def month_end(period: date) -> date:
    return (period + timedelta(days=32)).replace(day=1) - timedelta(days=1)


def budget(amount: str, period: date = JUNE, **overrides: Any) -> Budget:
    values: dict[str, Any] = {
        "client": "AAA",
        "platform": "meta",
        "start_date": period,
        "end_date": month_end(period),
        "amount": money(amount),
    }
    return Budget(**{**values, **overrides})


def budget_outcome(
    delivered: str, planned: str = "1000.00", **overrides: Any
) -> ReconciliationOutcome:
    values: dict[str, Any] = {
        "delivery": delivery(JUNE, [delivered]),
        "budgets": [budget(planned)],
        "billing": {"AAA": CLIENT_OWNED},
    }
    return outcome(**{**values, **overrides})


def test_budget_and_client_invoice_thresholds_are_the_agreed_values() -> None:
    assert (BUDGET_MIN_SHARE, BUDGET_MAX_SHARE) == (money("0.85"), money("1.15"))
    assert CLIENT_INVOICE_MIN_EUR == money("1.00")


def test_delivery_above_115_percent_of_budget_is_an_overspend() -> None:
    result = budget_outcome("1150.01")
    issue = only_issue(result)
    assert issue.type == templates.BUDGET_OVERSPEND
    assert issue.amount == money("150.01")
    assert issue.dedup_key == "reconciliation:budget:AAA:meta:2026-06"
    assert "EUR 1,000.00" in issue.explanation
    item = result_for(result, SLOT)
    assert (item.planned, item.status) == (money("1000.00"), "issue")


def test_delivery_below_85_percent_of_budget_is_an_underspend() -> None:
    issue = only_issue(budget_outcome("849.99"))
    assert issue.type == templates.BUDGET_UNDERSPEND
    assert issue.amount == money("-150.01")


def test_delivery_within_the_budget_band_is_ok() -> None:
    result = budget_outcome("1000.00")
    assert result.issues == []
    assert result_for(result, SLOT).status == "ok"


@pytest.mark.parametrize("delivered", ["850.00", "1150.00"])
def test_exactly_85_or_115_percent_of_budget_does_not_fire(delivered: str) -> None:
    assert budget_outcome(delivered).issues == []


def test_budget_key_ignores_the_classification() -> None:
    over, under = only_issue(budget_outcome("2000.00")), only_issue(budget_outcome("10.00"))
    assert over.dedup_key == under.dedup_key


def test_client_owned_clients_get_the_budget_rule_only() -> None:
    client_invoice = invoice("1.00", kind="client", platform=None, vendor="Agency", number="AG-1")
    result = budget_outcome("2000.00", invoices=[client_invoice])
    assert types(result) == [templates.BUDGET_OVERSPEND]
    item = result_for(result, SLOT)
    assert (item.client_invoiced, item.expected_client_amount) == (None, None)


def test_budgets_of_december_and_january_are_separate_slots() -> None:
    result = outcome(
        delivery=[*delivery(DECEMBER, ["1000.00"]), *delivery(JANUARY, ["300.00"])],
        budgets=[budget("1000.00", DECEMBER), budget("500.00", JANUARY)],
        billing={"AAA": CLIENT_OWNED},
    )
    issue = only_issue(result)
    assert (issue.period, issue.dedup_key) == (JANUARY, "reconciliation:budget:AAA:meta:2027-01")
    assert result_for(result, Slot("AAA", "meta", DECEMBER)).planned == money("1000.00")


def test_two_orders_in_one_month_are_summed() -> None:
    result = budget_outcome("1000.00", budgets=[budget("600.00"), budget("400.00")])
    assert result.issues == []
    assert result_for(result, SLOT).planned == money("1000.00")


def test_an_order_spanning_months_leaves_planned_unknown() -> None:
    spanning = budget("1000.00", end_date=date(2026, 7, 31))
    result = budget_outcome("10.00", budgets=[spanning, budget("10.00")])
    assert result.issues == []
    assert result_for(result, SLOT).planned is None
    assert result_for(result, JULY_SLOT).planned is None


def test_a_budget_without_delivery_is_incomplete_and_not_an_underspend() -> None:
    result = outcome(budgets=[budget("1000.00")], billing={"AAA": CLIENT_OWNED})
    assert result.issues == []
    item = result_for(result, SLOT)
    assert (item.planned, item.delivered, item.status) == (money("1000.00"), None, "incomplete")


# --- rule 3: client invoice vs billing model -----------------------------------------------

META_REF = "AAA-META-2606"
GOOGLE_REF = "AAA-GOOGLE-2606"
GOOGLE_SLOT = Slot("AAA", "google", JUNE)
MANAGEMENT_FEE = BillingTerms("mgmt_fee", mgmt_fee=Decimal("500.00"))


def client_invoice(
    *lines: tuple[str | None, str], number: str = "AG-1", period: date = JUNE, **overrides: Any
) -> Invoice:
    """`lines` are (campaign code, amount); a line without a code is the fee."""
    values: dict[str, Any] = {
        "kind": "client",
        "vendor": "Agency",
        "number": number,
        "client": "AAA",
        "platform": None,
        "issue_date": period + timedelta(days=40),
        "period_start": period,
        "period_end": month_end(period),
        "lines": tuple(
            Line(100 + no, no, ref, money(amount)) for no, (ref, amount) in enumerate(lines, 1)
        ),
    }
    return Invoice(**{**values, **overrides})


def credit(amount: str, number: str = "CN-1", original: str = "INV-1") -> CreditNote:
    return CreditNote("Meta", number, ("Meta", original), money(amount))


def billed(*client_invoices: Invoice, **overrides: Any) -> ReconciliationOutcome:
    """June meta: 1000.00 delivered and billed by the platform, plus the client invoices."""
    values: dict[str, Any] = {
        "delivery": delivery(JUNE, ["1000.00"]),
        "invoices": [invoice("1000.00"), *client_invoices],
    }
    return outcome(**{**values, **overrides})


def two_platforms(*client_invoices: Invoice) -> ReconciliationOutcome:
    google_invoice = invoice(
        "333.33",
        vendor="Google",
        platform="google",
        lines=(Line(2, 1, GOOGLE_REF, money("333.33")),),
    )
    return billed(
        *client_invoices,
        delivery=[*delivery(JUNE, ["1000.00"]), *delivery(JUNE, ["333.33"], platform="google")],
        invoices=[invoice("1000.00"), google_invoice, *client_invoices],
        orders=[order(), order(platform="google", campaign_ref=GOOGLE_REF, campaign_ids=(8,))],
    )


def test_fee_is_rounded_half_up_to_cents() -> None:
    terms = BillingTerms("markup", markup_pct=Decimal("12.50"))
    assert expected_fee(money("100.20"), terms) == money("12.53")
    assert expected_fee(money("0.04"), terms) == money("0.01")
    assert expected_fee(money("100.10"), terms) == money("12.51")


def test_management_fee_is_the_contract_amount() -> None:
    assert expected_fee(money("123.45"), MANAGEMENT_FEE) == money("500.00")


def test_correct_markup_makes_no_issue_and_the_month_is_ok() -> None:
    result = billed(client_invoice((META_REF, "1000.00"), (None, "150.00")))
    assert result.issues == []
    item = result_for(result, SLOT)
    assert (item.client_invoiced, item.expected_client_amount, item.status) == (
        money("1150.00"),
        money("1150.00"),
        "ok",
    )


def test_wrong_markup_is_a_client_level_issue() -> None:
    result = two_platforms(
        client_invoice((META_REF, "1000.00"), (GOOGLE_REF, "333.33"), (None, "253.33"))
    )
    issue = only_issue(result)
    assert issue.type == templates.WRONG_MARKUP
    assert (issue.platform, issue.amount) == (None, money("53.33"))
    assert issue.dedup_key == "reconciliation:client_fee:AAA:2026-06"
    assert "15.00%" in issue.explanation
    assert result_for(result, SLOT).status == result_for(result, GOOGLE_SLOT).status == "issue"


def test_fee_exactly_one_euro_off_does_not_fire() -> None:
    result = billed(client_invoice((META_REF, "1000.00"), (None, "151.00")))
    assert result.issues == []


def test_fee_just_over_one_euro_off_fires() -> None:
    result = billed(client_invoice((META_REF, "1000.00"), (None, "148.99")))
    assert only_issue(result).amount == money("-1.01")


def test_wrong_management_fee_fires_and_the_right_one_does_not() -> None:
    right = billed(
        client_invoice((META_REF, "1000.00"), (None, "500.00")), billing={"AAA": MANAGEMENT_FEE}
    )
    wrong = billed(
        client_invoice((META_REF, "1000.00"), (None, "550.00")), billing={"AAA": MANAGEMENT_FEE}
    )
    assert right.issues == []
    issue = only_issue(wrong)
    assert (issue.type, issue.amount) == (templates.WRONG_MARKUP, money("50.00"))
    assert "management fee" in issue.explanation


def test_an_applied_credit_makes_no_issue() -> None:
    result = billed(
        client_invoice((META_REF, "1000.00"), (META_REF, "-50.00"), (None, "142.50")),
        credit_notes=[credit("50.00")],
    )
    assert result.issues == []
    item = result_for(result, SLOT)
    assert item.client_invoiced == item.expected_client_amount == money("1092.50")


def test_a_missing_credit_is_one_issue_and_not_also_a_wrong_markup() -> None:
    result = billed(
        client_invoice((META_REF, "1000.00"), (None, "150.00")),
        credit_notes=[credit("50.00")],
    )
    issue = only_issue(result)
    assert issue.type == templates.CREDIT_NOT_APPLIED
    assert (issue.platform, issue.amount) == ("meta", money("50.00"))
    assert issue.dedup_key == "reconciliation:credit:AAA:meta:2026-06"
    assert "CN-1" in issue.explanation and "commission" in issue.explanation
    item = result_for(result, SLOT)
    assert (item.client_invoiced, item.expected_client_amount, item.status) == (
        money("1150.00"),
        money("1092.50"),
        "issue",
    )


def test_a_partly_applied_credit_is_flagged_for_the_remainder() -> None:
    result = billed(
        client_invoice((META_REF, "1000.00"), (META_REF, "-20.00"), (None, "147.00")),
        credit_notes=[credit("50.00")],
    )
    assert only_issue(result).amount == money("30.00")


def test_a_credit_exactly_one_euro_short_does_not_fire() -> None:
    result = billed(
        client_invoice((META_REF, "1000.00"), (META_REF, "-49.00"), (None, "142.65")),
        credit_notes=[credit("50.00")],
    )
    assert result.issues == []


def test_more_credit_than_the_platform_gave_is_flagged() -> None:
    result = billed(
        client_invoice((META_REF, "1000.00"), (META_REF, "-80.00"), (None, "138.00")),
        credit_notes=[credit("50.00")],
    )
    issue = only_issue(result)
    assert (issue.type, issue.amount) == (templates.CREDIT_OVER_APPLIED, money("-30.00"))
    assert issue.dedup_key == "reconciliation:credit:AAA:meta:2026-06"


def test_a_credit_on_a_duplicate_copy_is_not_expected_on_the_client_invoice() -> None:
    copy = invoice("1000.00", number="INV-2", issue_date=date(2026, 7, 9))
    result = billed(
        client_invoice((META_REF, "1000.00"), (None, "150.00")),
        invoices=[
            invoice("1000.00"),
            copy,
            client_invoice((META_REF, "1000.00"), (None, "150.00")),
        ],
        credit_notes=[credit("50.00", original="INV-2")],
    )
    assert types(result) == [templates.DUPLICATE_INVOICE]


def test_a_december_credit_is_checked_in_december_not_january() -> None:
    dec_ref, jan_ref = "AAA-META-2612", "AAA-META-2701"
    platform_invoices = [
        invoice(
            "1000.00",
            number=f"INV-{period.month}",
            period_start=period,
            period_end=month_end(period),
            issue_date=period + timedelta(days=35),
            lines=(Line(period.month, 1, ref, money("1000.00")),),
        )
        for period, ref in ((DECEMBER, dec_ref), (JANUARY, jan_ref))
    ]
    result = outcome(
        delivery=[*delivery(DECEMBER, ["1000.00"]), *delivery(JANUARY, ["1000.00"])],
        invoices=[
            *platform_invoices,
            client_invoice((dec_ref, "1000.00"), (None, "150.00"), number="AG-12", period=DECEMBER),
            client_invoice((jan_ref, "1000.00"), (None, "150.00"), number="AG-01", period=JANUARY),
        ],
        orders=[order(campaign_ref=dec_ref), order(campaign_ref=jan_ref, campaign_ids=(8,))],
        credit_notes=[credit("50.00", original="INV-12")],
    )
    issue = only_issue(result)
    assert (issue.type, issue.period) == (templates.CREDIT_NOT_APPLIED, DECEMBER)
    assert issue.dedup_key == "reconciliation:credit:AAA:meta:2026-12"
    assert result_for(result, Slot("AAA", "meta", JANUARY)).status == "ok"


def test_a_legitimate_second_client_invoice_is_combined_not_a_duplicate() -> None:
    first = client_invoice((META_REF, "800.00"), (None, "120.00"))
    second = client_invoice((META_REF, "200.00"), (None, "30.00"), number="AG-2")
    result = billed(first, second)
    assert result.issues == []
    assert result_for(result, SLOT).client_invoiced == money("1150.00")


def test_a_duplicated_client_invoice_counts_once() -> None:
    first = client_invoice((META_REF, "1000.00"), (None, "150.00"))
    copy = client_invoice(
        (META_REF, "1000.00"), (None, "150.00"), number="AG-2", issue_date=date(2026, 7, 20)
    )
    result = billed(first, copy)
    assert types(result) == [templates.DUPLICATE_INVOICE]
    assert result_for(result, SLOT).client_invoiced == money("1150.00")


def test_agency_billed_month_without_a_client_invoice_is_incomplete() -> None:
    result = billed()
    assert result.issues == []
    item = result_for(result, SLOT)
    assert (item.client_invoiced, item.expected_client_amount, item.status) == (
        None,
        None,
        "incomplete",
    )


def test_an_unattributed_line_leaves_the_columns_unknown_but_checks_the_fee() -> None:
    result = billed(client_invoice((META_REF, "1000.00"), ("UNKNOWN", "10.00"), (None, "100.00")))
    assert sorted(types(result)) == sorted([templates.NEEDS_MATCHING, templates.WRONG_MARKUP])
    item = result_for(result, SLOT)
    assert (item.client_invoiced, item.expected_client_amount) == (None, None)


# --- splitting a client invoice over platforms ---------------------------------------------


def test_split_by_weight_uses_the_largest_remainder() -> None:
    parts = split_by_weight(money("100.00"), {"c": Decimal(1), "a": Decimal(1), "b": Decimal(1)})
    assert parts == {"a": money("33.34"), "b": money("33.33"), "c": money("33.33")}


def test_split_by_weight_gives_the_cent_to_the_largest_remainder_not_the_first_name() -> None:
    parts = split_by_weight(money("0.10"), {"a": Decimal(1), "b": Decimal(2)})
    assert parts == {"a": money("0.03"), "b": money("0.07")}


def test_split_by_weight_breaks_ties_by_name() -> None:
    assert split_by_weight(money("0.01"), {"b": Decimal(1), "a": Decimal(1)}) == {
        "a": money("0.01"),
        "b": money("0.00"),
    }


@pytest.mark.parametrize(
    "weights", [{"a": Decimal(0), "b": Decimal(0)}, {"a": Decimal(-1), "b": Decimal(3)}]
)
def test_split_by_weight_without_usable_weights_is_equal(weights: dict[str, Decimal]) -> None:
    assert split_by_weight(money("10.01"), weights) == {"a": money("5.01"), "b": money("5.00")}


def test_split_by_weight_of_a_negative_amount_sums_exactly() -> None:
    parts = split_by_weight(money("-0.10"), {"a": Decimal(1), "b": Decimal(1), "c": Decimal(1)})
    assert sum(parts.values()) == money("-0.10")


def test_client_invoiced_rows_sum_exactly_to_the_invoice() -> None:
    fee_invoice = client_invoice((META_REF, "1000.00"), (GOOGLE_REF, "333.33"), (None, "200.00"))
    result = two_platforms(fee_invoice)
    assert result.issues == []
    meta, google = result_for(result, SLOT), result_for(result, GOOGLE_SLOT)
    assert (meta.client_invoiced, google.client_invoiced) == (money("1150.00"), money("383.33"))
    assert meta.client_invoiced + google.client_invoiced == fee_invoice.net
    assert meta.expected_client_amount + google.expected_client_amount == fee_invoice.net
