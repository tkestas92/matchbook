# Generator

Synthetic data for Matchbook (see `docs/plan.md` §5.1).

## Distributions (`build_distributions.py`)

Reads the Kaggle CSV from `data/kaggle/` (gitignored; found by glob) and writes
`distributions.json`, which **is** committed. Run:

```
uv run python generator/build_distributions.py
```

- The CSV uses old Mac `\r` line endings; the reader handles it (1143 rows expected).
- Rows are dropped when clicks are zero (including conversions without clicks), spend is zero or
  negative, or impressions are below clicks. Counts per reason are stored in the JSON.
- Per row: CTR = clicks / impressions, CPC = spent / clicks, CPM = spent / impressions × 1000,
  CVR = Total_Conversion / clicks.
- Per metric: mean, std, p5/p25/p50/p75/p95 and a suggested lognormal (`mu`, `sigma` of ln x,
  plus `ks_distance` as a rough goodness-of-fit; lower is better). `zero_share` is the share of
  exact zeros, excluded from the fit, so sample zeros explicitly.

## Limits

The dataset has **no dates**, so only ratios are used. Daily time series (weekday rhythm,
seasonality, noise) are generated. The dataset's currency is not EUR; absolute values (CPC,
CPM) are scale hints only. CVR can exceed 1 in a few rows because conversions are not capped by clicks.

## Attribution

Dataset: "Clicks Conversion Tracking" by loveall, from Kaggle:
https://www.kaggle.com/datasets/loveall/clicks-conversion-tracking
