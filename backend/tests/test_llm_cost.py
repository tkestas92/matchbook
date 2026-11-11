"""Pure cost arithmetic and cap checks (no database; `monthly_spend` is in test_llm_cost_db.py)."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from app.config import ModelPrice
from app.llm.cost import (
    CostCapExceeded,
    CostLedger,
    UnknownModelPrice,
    cost_of,
    estimate_input_tokens,
    month_start_utc,
    price_for,
    worst_case_cost,
)

PRICE = ModelPrice(input_eur_per_mtok=Decimal("0.10"), output_eur_per_mtok=Decimal("0.40"))


def test_cost_is_exact_decimal() -> None:
    assert cost_of(PRICE, 1_000_000, 0) == Decimal("0.10")
    assert cost_of(PRICE, 1234, 567) == Decimal("0.0003502")
    assert isinstance(cost_of(PRICE, 1, 1), Decimal)


def test_unknown_model_has_no_price() -> None:
    with pytest.raises(UnknownModelPrice):
        price_for({"known": PRICE}, "other")
    assert price_for({"known": PRICE}, "known") is PRICE


def test_input_estimate_is_an_upper_bound() -> None:
    assert estimate_input_tokens("") == 0
    assert estimate_input_tokens("abcde") == 3
    assert estimate_input_tokens("ąč") == 2  # four UTF-8 bytes


def test_worst_case_assumes_the_full_output_limit() -> None:
    assert worst_case_cost(PRICE, "ab", 1000) == cost_of(PRICE, 1, 1000)


def test_run_cap_allows_exactly_the_cap() -> None:
    ledger = CostLedger(run_cap=Decimal("0.20"), monthly_cap=Decimal("1"))
    ledger.record(Decimal("0.15"), 10)
    ledger.check(Decimal("0.05"))
    with pytest.raises(CostCapExceeded, match="per-run"):
        ledger.check(Decimal("0.050001"))


def test_monthly_cap_counts_earlier_runs_and_this_run() -> None:
    ledger = CostLedger(
        run_cap=Decimal("0.20"),
        monthly_cap=Decimal("1"),
        spent_this_month_before_run=Decimal("0.90"),
    )
    ledger.record(Decimal("0.05"), 10)
    ledger.check(Decimal("0.05"))
    with pytest.raises(CostCapExceeded, match="monthly"):
        ledger.check(Decimal("0.050001"))


def test_record_accumulates() -> None:
    ledger = CostLedger(run_cap=Decimal("1"), monthly_cap=Decimal("1"))
    ledger.record(Decimal("0.001"), 10)
    ledger.record(Decimal("0.002"), 20)
    assert (ledger.total, ledger.tokens, ledger.calls) == (Decimal("0.003"), 30, 2)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        # Vilnius is UTC+3 in summer: 1 July 00:00 local = 30 June 21:00 UTC.
        (datetime(2026, 7, 15, 12, tzinfo=UTC), datetime(2026, 6, 30, 21, tzinfo=UTC)),
        # UTC+2 in winter.
        (datetime(2026, 1, 10, tzinfo=UTC), datetime(2025, 12, 31, 22, tzinfo=UTC)),
        # 22:30 UTC on 30 Nov is already 1 Dec in Vilnius.
        (datetime(2026, 11, 30, 22, 30, tzinfo=UTC), datetime(2026, 11, 30, 22, tzinfo=UTC)),
        # March: the month starts in winter time even though "now" is after the DST switch.
        (datetime(2026, 3, 31, 12, tzinfo=UTC), datetime(2026, 2, 28, 22, tzinfo=UTC)),
    ],
)
def test_month_start_is_in_agency_time(now: datetime, expected: datetime) -> None:
    assert month_start_utc(now, "Europe/Vilnius") == expected
