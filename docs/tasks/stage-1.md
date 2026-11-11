# Stage 1 — data, pipeline, rules (no AI yet)

Goal: `make generate && make pipeline && make eval-stage1` finds 100% of the rule-detectable planted errors
(#1, #2, #4, #5, #6, #7, #9) with ~no false alarms. Errors #3, #8, #10 are expected to be missed (stages 2 and 4).

How to use: one task = one branch = one agent session. Start every Claude Code task in **plan mode**.
Before merging: `make lint && make test` and a green CI check.
Read `docs/plan.md` sections 5 (data), 7 (pipeline), 8 (reconciliation), 9 (pacing) for the details.

---

## 1.1 — Generator: config and the true world
**Tool:** Claude Code (Opus) · **Branch:** `feat/generator-world`

```
Read AGENTS.md and docs/plan.md section 5.1, and generator/README.md + generator/distributions.json.
Create generator/config.yaml (seed, 8 fictional Lithuanian clients with industry and billing model
[markup 10-20% / management fee / client-owned], months May-October 2026, platforms Meta/Google/TikTok,
2-3 platforms per client, error rate) and generator/world.py that builds the "true world" deterministically
from the seed: clients, insertion orders (budget, dates, campaign codes per client/month/platform),
campaigns, and daily delivery (budget split by day weights, actual = plan x noise +-5-10%,
impressions/clicks/conversions derived from CPM/CTR/CVR).
Ratios: use distributions.json for the SHAPE and spread (sigma, relative variation). The Kaggle absolute levels
are unrealistically low (CTR ~0.02%, CPM ~0.3), so rescale means to realistic levels set in config.yaml
(e.g. CTR ~1%, CPM ~5 EUR) and record this in docs/decisions.md.
Money is Decimal. Same seed -> identical world (test it). No documents and no errors yet.
Propose the plan first.
```
**Done when:** same seed gives identical output; tests pass.

---

## 1.2 — Generator: documents
**Tool:** Claude Code · **Branch:** `feat/generator-documents`

```
Read docs/plan.md section 5.1 (steps 2, 4-7). Using the world from generator/world.py, produce:
- one Excel media plan per client (IOs)
- monthly platform invoices to the agency as PDF, computed in the platform timezone, 3 different layouts
- agency invoices to clients as PDF, per billing model
- credit notes linked to originals
- a ground-truth JSON next to every invoice (vendor, number, period, lines, totals)
Write files under data/generated/ (gitignored). No errors planted yet: all documents must be consistent with the world.
Add tests that the ground-truth JSON totals equal the world's numbers.
Propose the plan first and list the libraries you want (reason for each).
```
**Done when:** `make generate` produces the files; consistency tests pass.

---

## 1.3 — Generator: planted errors and answer key
**Tool:** Claude Code · **Branch:** `feat/planted-errors`

```
Read docs/plan.md section 5.2. Add generator/errors.py that plants errors #1-#9 into the documents/API data
(error #10, prompt injection, comes in stage 2) and writes one record per error to eval.answer_key
(error type, affected entity, period, EUR magnitude, expected detector).
Rules: deterministic from seed, error rate from config.yaml, every error recorded exactly once,
errors #3 and #8 are planted too even though stage 1 will not detect them.
Tests: each error type is planted and recorded; magnitudes match the documents.
```
**Done when:** answer key rows exist for every planted error; tests pass.

---

## 1.4 — Mock platform APIs
**Tool:** Claude Code · **Branch:** `feat/mock-platforms`

```
Read docs/plan.md section 5.3. Create mock_platforms/ as a FastAPI app serving Meta-, Google- and TikTok-style
endpoints from the generated data, with the different field names, pagination, date formats and timezones in the table.
All endpoints require a bearer token. Inject occasional 429 and 500 responses (configurable, deterministic by seed).
Missing days (error #9) and renamed campaigns (error #3) come from the planted errors.
Tests with FastAPI TestClient. Add a `make mock-apis` target.
```
**Done when:** each platform answers with its own format; 429/500 appear as configured.

---

## 1.5 — Ingest to raw
**Tool:** Cursor agent or Claude Code · **Branch:** `feat/ingest`

```
Create pipelines/ingest.py: fetch each platform from the mock APIs, store the raw JSON in raw.* tables
with fetched_at and run_id. Retry 429 with exponential backoff; if one platform keeps failing (500),
record the failure and continue with the others. Plain functions, no Airflow. Tests for retry, backoff and
the "one platform fails, run continues" case. Use the etl DB role.
```

---

## 1.6 — Normalize and quality checks
**Tool:** Claude Code · **Branch:** `feat/normalize`

```
Create pipelines/normalize.py: metric dictionary mapping each platform's fields to core.daily_delivery
(spend as Decimal, cost_micros -> EUR, date formats, timezones -> UTC and agency date),
upsert on (campaign_id, date). Create pipelines/quality.py: missing days, negative or null values,
sudden zero spend; write findings to app.issues. Idempotent. Tests for every mapping and for running twice.
```

---

## 1.7 — Load plans and invoices
**Tool:** Cursor agent · **Branch:** `feat/load-documents`

```
Create pipelines/load_plans.py (Excel -> core.insertion_orders, upsert on its unique key) and
pipelines/load_invoices.py (ground-truth JSON -> core.invoices, invoice_lines, credit_notes).
Idempotent, with run_id. Tests with the generated sample files.
```

---

## 1.8 — Reconciliation rules
**Tool:** Claude Code (Opus) · **Branch:** `feat/reconciliation`

```
Read docs/plan.md section 8. Implement backend/app/reconciliation/ rules per client x platform x month:
link invoice lines to campaigns by code; delivered spend in platform tz AND agency tz;
invoice vs delivered (variance > 1 EUR and > 0.5%; classify "timezone shift" when the tz difference explains it);
delivered vs IO budget; client invoice vs expected amount from billing model (wrong markup, credit not applied);
duplicate invoices. Write core.reconciliation_results and app.issues with template-based explanations (no LLM).
Pure functions, Decimal money, one test per rule plus edge cases (rounding, month boundaries).
```
**Done when:** every rule has passing tests.

---

## 1.9 — Pacing
**Tool:** Claude Code · **Branch:** `feat/pacing`

```
Read docs/plan.md section 9. Implement pacing in backend/app/reconciliation/pacing.py: daily plan from budget x
day weights, cumulative actual vs plan, forecast = actual + last-7-day average x remaining days, status
thresholds from config. Create issues for red/yellow. Tests incl. a campaign that spends its budget in 60% of the month.
```

---

## 1.10 — Pipeline runner and run record
**Tool:** Claude Code · **Branch:** `feat/pipeline-runner`

```
Create pipelines/run.py wiring steps 1-8 from docs/plan.md section 7 and wire `make pipeline`
(and `make generate`). Record every run in app.pipeline_runs (status, duration, per-step counts).
Idempotency test: running the pipeline twice gives identical row counts.
```

---

## 1.11 — Stage 1 integration eval
**Tool:** Claude Code · **Branch:** `feat/eval-stage1`

```
Create evals/stage1.py (runs as eval_runner, the only code allowed to read eval.answer_key):
compare app.issues with eval.answer_key and print found / missed / false alarms per error type and overall.
Expected missed in stage 1: #3, #8, #10. Wire `make eval-stage1` and fail if any rule-detectable error is missed.
Write the results table to docs/eval-results.md.
```
**Done when:** 100% of #1, #2, #4, #5, #6, #7, #9 found, false alarms ~0.

---

## 1.12 — Airflow wrapper
**Tool:** Cursor agent · **Branch:** `feat/airflow-dag`

```
Add an Airflow DAG (docker compose profile, not part of default `make up`) that only calls the existing
plain functions from pipelines/, one task per step, nightly schedule. No logic inside the DAG.
```
**Status:** done — DAG `matchbook_nightly` (compose profile `airflow`, decision 034).

---

## 1.13 — Live demo on macserver
**Tool:** by hand with Claude Code help · **Branch:** `chore/deploy-macserver`

Docker Compose on the home server, Cloudflare Tunnel to `demo-matchbook.kantrybes.lt`, read-only demo data.
Plan this task separately once 1.11 is green.
**Status:** done — read-only demo API (decision 035) and server deploy (decision 036, `docs/deploy-demo.md`);
public demo at https://demo-matchbook.kantrybes.lt/docs behind a Cloudflare Tunnel with a Cloudflare rate limiting rule.
