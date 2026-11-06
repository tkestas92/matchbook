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

## True world (`world.py`)

Builds what was planned, delivered and spent, deterministically from `config.yaml`
(seed, clients, months, platforms, ratio levels, delivery noise). Run:

```
uv run python generator/world.py
```

It prints counts, totals and a digest; the same seed always gives the same digest.

- **Clients:** listed in `config.yaml` (fictional), each with a billing model (markup 10–20%,
  management fee, client-owned) and 2–3 platforms with budget shares.
- **Ad accounts:** one per client × platform, in the platform's timezone.
- **Insertion orders:** one per client × month × platform; budget = monthly budget × month
  multiplier × platform share; `campaign_ref` like `SAU-META-2605`.
- **Campaigns:** one per IO, with base CPM/CTR/CVR sampled per campaign.
- **Daily delivery:** plan = budget split by weekday weights (Σplan = budget exactly);
  spend = plan × (1 ± noise, 5–10% per campaign); impressions, clicks and conversions from
  CPM/CTR/CVR. Dates are ad-account local.

Ratio spread comes from `distributions.json`; mean levels from `config.yaml` (decision 022).
No documents and no planted errors yet; money is always `Decimal`.

## Documents (`generator/documents/`)

Renders the true world into the documents an agency sees. Run:

```
uv run python -m generator.documents
```

Output (gitignored, replaced on every run):

```
data/generated/
  manifest.json                                   # sha256 of every file
  plans/<CLIENT>_media_plan.xlsx                  # IOs: platform, campaign_ref, flight, budget
  invoices/platform/<platform>/<yyyy-mm>/<number>.pdf|.json
  invoices/client/<CLIENT>/<yyyy-mm>/<number>.pdf|.json
  credit_notes/<platform>/<yyyy-mm>/<number>.pdf|.json
```

- **Platform invoices** (platform → agency): one per ad account and month, lines = delivered spend
  per `campaign_ref` in the platform timezone. Three layouts: Meta-, Google- and TikTok-like.
- **Agency invoices** (agency → client): media at cost, credit-note lines, then the markup commission
  or management fee. Client-owned clients get only a media plan (the platform bills them directly).
- **Credit notes**: `documents.credit_notes.count` of them, each linked to a platform invoice and
  applied on the same period's client invoice.
- **Ground truth**: a JSON next to every PDF (vendor, number, dates, period, lines with
  `campaign_ref`, subtotal, VAT 21% `ROUND_HALF_UP`, total). Money is a string for exact `Decimal`.
- English only for now; no planted errors (task 1.3). Same seed → identical files (decision 024).
- Fonts: DejaVu Sans (`generator/fonts/`, see `LICENSE-DejaVu.txt`) for Lithuanian letters.
