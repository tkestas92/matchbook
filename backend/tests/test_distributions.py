import json
import math
from pathlib import Path

import pytest

from generator.build_distributions import clean, compute_metrics, fit_lognormal, summarize

DISTRIBUTIONS_PATH = Path(__file__).resolve().parents[2] / "generator" / "distributions.json"
METRICS = ("ctr", "cpc", "cpm", "cvr")
STATS = ("mean", "std", "p5", "p25", "p50", "p75", "p95")


def _row(impressions: str, clicks: str, spent: str, conversions: str) -> dict[str, str]:
    return {
        "Impressions": impressions,
        "Clicks": clicks,
        "Spent": spent,
        "Total_Conversion": conversions,
    }


@pytest.fixture(scope="module")
def distributions() -> dict:
    return json.loads(DISTRIBUTIONS_PATH.read_text())


def test_distributions_json_has_all_metrics(distributions: dict) -> None:
    assert set(distributions["metrics"]) == set(METRICS)


def test_distributions_values_are_positive(distributions: dict) -> None:
    for name in METRICS:
        metric = distributions["metrics"][name]
        for stat in STATS:
            assert metric[stat] > 0, f"{name}.{stat}"
        suggested = metric["suggested"]
        assert suggested["name"] == "lognormal"
        assert suggested["sigma"] > 0
        assert 0 <= metric["zero_share"] < 1


def test_distributions_percentiles_are_ordered(distributions: dict) -> None:
    for name in METRICS:
        metric = distributions["metrics"][name]
        percentiles = [metric[p] for p in ("p5", "p25", "p50", "p75", "p95")]
        assert percentiles == sorted(percentiles)


def test_distributions_row_counts_add_up(distributions: dict) -> None:
    rows = distributions["rows"]
    assert rows["read"] == rows["used"] + rows["dropped"]
    assert rows["used"] > 0


def test_clean_drops_bad_rows() -> None:
    good = _row("1000", "10", "15.5", "2")
    rows = [
        good,
        _row("1000", "0", "0", "3"),  # conversions without clicks
        _row("1000", "10", "0", "1"),  # zero spend
        _row("1000", "10", "-1", "1"),  # negative spend
        _row("5", "10", "3", "1"),  # impressions < clicks
    ]
    kept, by_reason = clean(rows)
    assert kept == [good]
    assert by_reason["conversions_without_clicks"] == 1
    assert by_reason["zero_or_negative_spend"] == 3
    assert by_reason["impressions_below_clicks"] == 1


def test_compute_metrics() -> None:
    metrics = compute_metrics(_row("1000", "10", "15", "2"))
    assert metrics == pytest.approx({"ctr": 0.01, "cpc": 1.5, "cpm": 15.0, "cvr": 0.2})


def test_fit_lognormal_recovers_parameters() -> None:
    values = [math.exp(1 + 0.5 * z) for z in (-1.5, -1, -0.5, 0, 0.5, 1, 1.5)]
    fit = fit_lognormal(values)
    assert fit["mu"] == pytest.approx(1.0)
    assert fit["sigma"] > 0


def test_summarize_reports_zero_share() -> None:
    summary = summarize([0.0, 1.0, 2.0, 3.0])
    assert summary["zero_share"] == pytest.approx(0.25)
    assert summary["p50"] == pytest.approx(1.5)
