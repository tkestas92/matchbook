# Architecture decisions

Short records of important choices: what was decided and why.
New entries go at the bottom. Format: context → decision → consequences.

---

## 001 — Monorepo
**Context:** backend, generator, mock APIs, pipelines, MCP server, evals and frontend are tightly related.
**Decision:** one repository.
**Consequences:** one CI, one set of agent rules, coding agents see the whole system.

## 002 — uv and Python 3.12
**Context:** need reproducible environments; 3.11/3.12 are the most common production versions.
**Decision:** manage dependencies and the Python version with uv; target Python 3.12.
**Consequences:** fast installs, lockfile, no separate pyenv setup.

## 003 — Four database schemas: raw, core, app, eval
**Context:** need to re-normalize without re-fetching, and evals must not leak into the system under test.
**Decision:** `raw` (as received), `core` (normalized), `app` (application state), `eval` (answer key).
**Consequences:** normalization bugs are fixable from `raw`; no module can "find" errors by reading `eval`.

## 004 — Least-privilege database roles
**Context:** an LLM agent will generate SQL.
**Decision:** separate roles `etl`, `app`, `agent_ro`, `eval_runner`; `agent_ro` is SELECT-only on curated views with RLS.
**Consequences:** safety is enforced by the database, not by prompts. A test proves `agent_ro` cannot write or read `eval`.

## 005 — Money as Decimal / NUMERIC(12,2), EUR only
**Context:** reconciliation compares amounts to the cent; floats introduce rounding errors.
**Decision:** `Decimal` in Python, `NUMERIC(12,2)` in PostgreSQL, single currency EUR.
**Consequences:** exact comparisons; multi-currency is out of scope.

## 006 — UTC storage, agency timezone for reporting
**Context:** platforms report in different timezones than invoices; month boundaries shift spend.
**Decision:** store UTC; compute reporting dates in `Europe/Vilnius`; keep each platform's timezone in `core.platforms`.
**Consequences:** timezone shifts become detectable and explainable instead of looking like overcharges.

## 007 — Rules first, AI second
**Context:** LLMs are costly, slow and can be wrong; most reconciliation is arithmetic.
**Decision:** deterministic code for everything computable; LLMs only for messy documents, fuzzy name matching, summaries and natural-language questions.
**Consequences:** stage 1 runs with no AI and gives a measurable baseline; AI value is shown as an improvement over it.

## 008 — Pipeline steps are plain functions; Airflow only orchestrates
**Context:** Airflow is heavy to develop and test against.
**Decision:** each step is a plain Python function runnable via `make pipeline`; Airflow DAG just calls them.
**Consequences:** fast unit tests; orchestrator can be swapped.

## 009 — Synthetic data with a planted-error answer key
**Context:** no access to real agency data; need ground truth to measure quality.
**Decision:** deterministic generator creates a true world, renders messy documents, plants errors and records them in `eval.answer_key`. Ratios come from a public Kaggle ad dataset.
**Consequences:** every module gets found / missed / false-alarm metrics. README states clearly that data is synthetic.

## 010 — Single LLM access layer
**Context:** need model switching, fallback, cost tracking and tracing.
**Decision:** all LLM calls go through `backend/app/llm/`.
**Consequences:** one place for provider adapters, retries, cost caps and Langfuse.

## 011 — Public repository, English
**Context:** the project is a portfolio piece for international and Lithuanian employers.
**Decision:** public from day one; code, docs and commits in English.
**Consequences:** commit history shows how the project grew.
