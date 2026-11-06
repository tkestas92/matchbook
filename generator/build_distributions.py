"""Build generator/distributions.json from the Kaggle "Clicks Conversion Tracking" CSV.

Run: uv run python generator/build_distributions.py
"""

import csv
import json
import math
import statistics
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
KAGGLE_DIR = REPO_ROOT / "data" / "kaggle"
OUTPUT_PATH = Path(__file__).resolve().parent / "distributions.json"

REQUIRED_COLUMNS = {"Impressions", "Clicks", "Spent", "Total_Conversion"}
MIN_EXPECTED_ROWS = 1000
PERCENTILES = (5, 25, 50, 75, 95)
SIGNIFICANT_DIGITS = 6

SOURCE = {
    "dataset": "Clicks Conversion Tracking",
    "author": "loveall",
    "platform": "Kaggle",
    "note": "Dataset has no dates; only ratios are used.",
}


def find_csv(directory: Path) -> Path:
    matches = sorted(directory.glob("*.csv"))
    if len(matches) != 1:
        raise FileNotFoundError(f"expected exactly one CSV in {directory}, found {len(matches)}")
    return matches[0]


def read_rows(path: Path) -> list[dict[str, str]]:
    # newline=None enables universal newlines, so old Mac "\r"-only files split correctly.
    with path.open(newline=None, encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV is missing columns: {sorted(missing)}")
        return list(reader)


def _to_decimal(value: str) -> Decimal | None:
    try:
        return Decimal(value.strip())
    except (InvalidOperation, AttributeError):
        return None


def drop_reasons(row: dict[str, str]) -> list[str]:
    impressions = _to_decimal(row["Impressions"])
    clicks = _to_decimal(row["Clicks"])
    spent = _to_decimal(row["Spent"])
    conversions = _to_decimal(row["Total_Conversion"])
    if None in (impressions, clicks, spent, conversions):
        return ["unparseable"]
    assert impressions is not None and clicks is not None
    assert spent is not None and conversions is not None
    reasons = []
    if clicks == 0 and conversions > 0:
        reasons.append("conversions_without_clicks")
    if clicks <= 0:
        reasons.append("zero_clicks")
    if spent <= 0:
        reasons.append("zero_or_negative_spend")
    if impressions <= 0 or impressions < clicks:
        reasons.append("impressions_below_clicks")
    return reasons


def clean(rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], dict[str, int]]:
    kept: list[dict[str, str]] = []
    by_reason: dict[str, int] = {}
    for row in rows:
        reasons = drop_reasons(row)
        if not reasons:
            kept.append(row)
        for reason in reasons:
            by_reason[reason] = by_reason.get(reason, 0) + 1
    return kept, dict(sorted(by_reason.items()))


def compute_metrics(row: dict[str, str]) -> dict[str, float]:
    impressions = Decimal(row["Impressions"])
    clicks = Decimal(row["Clicks"])
    spent = Decimal(row["Spent"])
    conversions = Decimal(row["Total_Conversion"])
    return {
        "ctr": float(clicks / impressions),
        "cpc": float(spent / clicks),
        "cpm": float(spent / impressions * 1000),
        "cvr": float(conversions / clicks),
    }


def fit_lognormal(values: list[float]) -> dict[str, Any]:
    logs = [math.log(v) for v in values if v > 0]
    mu = statistics.fmean(logs)
    sigma = statistics.stdev(logs)
    return {
        "name": "lognormal",
        "mu": mu,
        "sigma": sigma,
        "ks_distance": _ks_lognormal(logs, mu, sigma),
    }


def _ks_lognormal(logs: list[float], mu: float, sigma: float) -> float:
    """Kolmogorov-Smirnov distance between the sample and the fitted lognormal."""
    ordered = sorted(logs)
    n = len(ordered)
    worst = 0.0
    for i, x in enumerate(ordered):
        cdf = 0.5 * (1 + math.erf((x - mu) / (sigma * math.sqrt(2))))
        worst = max(worst, abs(cdf - i / n), abs(cdf - (i + 1) / n))
    return worst


def summarize(values: list[float]) -> dict[str, Any]:
    cuts = statistics.quantiles(values, n=100, method="inclusive")
    summary: dict[str, Any] = {
        "n": len(values),
        "mean": statistics.fmean(values),
        "std": statistics.stdev(values),
    }
    for p in PERCENTILES:
        summary[f"p{p}"] = cuts[p - 1]
    positives = [v for v in values if v > 0]
    summary["zero_share"] = 1 - len(positives) / len(values)
    summary["suggested"] = fit_lognormal(positives)
    return summary


def _round(value: Any) -> Any:
    if isinstance(value, float):
        return float(f"{value:.{SIGNIFICANT_DIGITS}g}")
    if isinstance(value, dict):
        return {k: _round(v) for k, v in value.items()}
    return value


def build(rows: list[dict[str, str]]) -> dict[str, Any]:
    if len(rows) < MIN_EXPECTED_ROWS:
        raise ValueError(f"only {len(rows)} rows read; expected ~1143 (line endings?)")
    kept, by_reason = clean(rows)
    metrics = [compute_metrics(row) for row in kept]
    return _round(
        {
            "source": SOURCE,
            "rows": {
                "read": len(rows),
                "used": len(kept),
                "dropped": len(rows) - len(kept),
                "dropped_by_reason": by_reason,
            },
            "metrics": {
                name: summarize([m[name] for m in metrics]) for name in ("ctr", "cpc", "cpm", "cvr")
            },
        }
    )


def main() -> None:
    path = find_csv(KAGGLE_DIR)
    result = build(read_rows(path))
    OUTPUT_PATH.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    rows = result["rows"]
    print(f"{path.name}: read {rows['read']}, used {rows['used']}, dropped {rows['dropped']}")
    print(f"wrote {OUTPUT_PATH.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    sys.exit(main())
