# Stage 0 — foundation

Goal: `make up && make migrate && make test` is green, and a test proves `agent_ro` cannot write or read `eval`.

How to use: one task = one branch = one agent session. Start every Claude Code task in **plan mode**,
review the plan, then let it implement. Before merging: `make lint && make test`.

---

## 0.1 — Repo skeleton
**Tool:** Claude Code · **Branch:** `chore/skeleton`

```
Read AGENTS.md and docs/plan.md.
Create the repository skeleton for Matchbook:
- the folder layout from AGENTS.md, each package with an __init__.py where it is Python
- pyproject.toml managed by uv, Python 3.12, with dependencies: fastapi, uvicorn, sqlalchemy>=2, alembic,
  psycopg[binary], pydantic-settings, python-dotenv; dev dependencies: pytest, ruff, pre-commit
- backend/app/config.py using pydantic-settings reading DATABASE_URL etc. from .env
- .env.example (no real secrets), .gitignore (include .env, data/, __pycache__, .venv, generated outputs)
- Makefile with targets: help, up, down, migrate, test, lint, format (targets for later stages may print "not yet implemented")
- a minimal README.md: name, one-line tagline "Three-way media reconciliation & reporting for agencies", status "work in progress", link to docs/plan.md
Do not create database tables yet.
```
**Done when:** `uv sync` works, `make help` lists targets.

---

## 0.2 — Docker Compose with PostgreSQL
**Tool:** Cursor agent · **Branch:** `chore/docker-postgres`

```
Add docker-compose.yml with PostgreSQL 16 (named volume, healthcheck, port from .env, credentials from .env).
Wire `make up` / `make down` to it. Update .env.example. Do not add other services yet.
```
**Done when:** `make up` starts Postgres and the healthcheck is healthy.

---

## 0.3 — Alembic, schemas and tables
**Tool:** Claude Code · **Branch:** `feat/db-schema`

```
Read docs/plan.md sections 6.1 and 6.2.
Set up Alembic in backend/migrations and SQLAlchemy 2.0 typed models in backend/app/db/.
Create schemas raw, core, app, eval and all tables from section 6.2.
Rules: money columns NUMERIC(12,2) mapped to Decimal; timestamps timestamptz in UTC;
unique constraint on core.daily_delivery (campaign_id, date); app.audit_log designed as insert-only;
every core data table that the pipeline writes has a run_id column.
For raw: one table per platform storing the raw JSON payload, fetched_at and run_id.
Wire `make migrate`. Add a test that runs migrations on an empty database and checks all schemas/tables exist.
Propose the plan first; list any field you think is missing or unnecessary before implementing.
```
**Done when:** `make migrate` creates everything; the test passes.

---

## 0.4 — Database roles, grants, RLS skeleton
**Tool:** Claude Code · **Branch:** `feat/db-roles`

```
Read docs/plan.md section 6.3 and docs/decisions.md entry 004.
In a new Alembic migration create roles etl, app, agent_ro, eval_runner (passwords from env, never hardcoded).
Grants:
- etl: read/write raw and core
- app: read core, read/write app
- agent_ro: no access to tables directly; SELECT only on curated views in core
  (create views v_spend_daily, v_pacing_base, v_invoices, v_issues — simple for now);
  RLS skeleton so a session variable (e.g. app.client_ids) limits which clients are visible
- eval_runner: read everything including eval
Set statement_timeout for agent_ro.
Tests (connect as each role):
- agent_ro cannot INSERT/UPDATE/DELETE anything
- agent_ro cannot SELECT from eval.answer_key or any base table
- agent_ro with client scope X sees only client X rows in a view
- app cannot read eval
Explain the RLS approach in a new docs/decisions.md entry.
```
**Done when:** all role tests pass.

---

## 0.5 — Lint, tests, pre-commit
**Tool:** Cursor agent · **Branch:** `chore/tooling`

```
Configure ruff (lint + format) in pyproject.toml, pytest config (testpaths, a fixture that provides a test database
URL from env), and .pre-commit-config.yaml running ruff. Make sure `make lint` and `make test` work locally.
```
**Done when:** `make lint` and `make test` pass.

---

## 0.6 — Minimal CI
**Tool:** Cursor agent · **Branch:** `ci/basic`

```
Add .github/workflows/ci.yml: on push and pull_request, set up uv + Python 3.12, start a PostgreSQL 16 service,
run migrations, then `make lint` and `make test`. Use repository secrets only if needed; tests use throwaway credentials.
```
**Done when:** a PR shows a green check.

---

## 0.7 — Kaggle distributions
**Tool:** Claude Code · **Branch:** `feat/distributions`

Before this task: download the Kaggle "Clicks Conversion Tracking" dataset (loveall) into `data/kaggle/` (gitignored).

```
Write generator/build_distributions.py that reads the Kaggle CSV from data/kaggle/,
cleans obvious bad rows (e.g. conversions with zero clicks), and computes per-row CTR, CPC, CPM and conversion rate.
Save to generator/distributions.json: for each metric the mean, std, percentiles (p5, p25, p50, p75, p95)
and a suggested distribution (e.g. lognormal parameters). Commit the JSON, not the CSV.
Add a short test that the JSON loads and values are positive. Note in the README of generator/ that
the dataset has no dates, so only ratios are used.
```
**Done when:** `generator/distributions.json` is committed.

---

## 0.8 — Decisions log check
**Tool:** Cursor agent · **Branch:** `docs/decisions`

```
Review docs/decisions.md against what was actually built in stage 0. Update entries that changed,
add entries for any new decisions made during 0.1–0.7. Keep each entry short.
```
**Done when:** decisions match the code.
