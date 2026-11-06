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

## 012 — Schema conventions: TEXT + CHECK enums, run_id without FK, updated_at by trigger
**Context:** the schema will evolve every stage; the pipeline upserts with `INSERT … ON CONFLICT`; `core` and `app` are written by different DB roles.
**Decision:** enum-like columns are `TEXT` with a named `CHECK` (not PG `ENUM`). `run_id` is a `UUID NOT NULL` on every pipeline-written row with **no** FK to `app.pipeline_runs`. `updated_at` is set by a `BEFORE UPDATE` trigger (`core.set_updated_at()`), not by the ORM. `app.audit_log` is insert-only via a trigger that rejects UPDATE, DELETE and TRUNCATE. FKs from financial tables use `ON DELETE RESTRICT`; `eval` has no FKs into `core`/`app`.
**Consequences:** adding an enum value is a one-line migration; `core` does not depend on `app`; upserts keep `updated_at` correct; audit history and financial rows cannot vanish by accident.

## 013 — Ad accounts carry the reporting timezone
**Context:** Meta and Google report in the *account* timezone (plan §5.3), and a campaign without an IO had no link to a client.
**Decision:** add `core.ad_accounts` (client, platform, external account id, timezone); every campaign belongs to an ad account. `core.platforms.timezone` stays as the platform default.
**Consequences:** delivery can be converted from the right timezone per account; every campaign has a client even before it is matched to an IO.

## 014 — Pipeline cost uses NUMERIC(12,6)
**Context:** money is `NUMERIC(12,2)` (decision 005), but a single LLM call costs fractions of a cent.
**Decision:** `app.pipeline_runs.cost_eur` is `NUMERIC(12,6)` — the one exception. It is an internal cost metric, never billed.
**Consequences:** per-run cost tracking and cost caps stay accurate; all billable amounts remain `NUMERIC(12,2)`.

## 015 — Login roles via `make roles`, privileges via migrations
**Context:** decision 004 needs roles `etl`, `app`, `agent_ro`, `eval_runner` with passwords from env. Roles are cluster-wide (shared by every database, incl. throwaway test databases); passwords are per-environment secrets that rotate.
**Decision:** `make roles` (`backend/app/db/roles.py`) idempotently creates the login roles and sets `LOGIN`, `PASSWORD` and explicit no-privilege attributes from `.env`; the password is quoted server-side with `format('%L')`. Migration `0002` owns everything database-scoped and non-secret: grants, default privileges, views, RLS, `statement_timeout`. It creates missing roles as `NOLOGIN`, so `make roles` and `make migrate` work in either order. Downgrade revokes but never drops roles.
**Consequences:** no secrets in migration files or `alembic --sql` output; rotating a password is `make roles`; a fresh environment runs `make up && make roles && make migrate`.

## 016 — Agent access: owner-rights views + RLS keyed on `app.user_id`
**Context:** `agent_ro` runs LLM-written SQL. It must read only curated views, never base tables, and only for clients its user may see. `security_invoker` views would require `agent_ro` to have SELECT on base tables; `FORCE ROW LEVEL SECURITY` does not help because the table owner is a superuser and superusers always bypass RLS.
**Decision:**
- A `NOLOGIN` role `agent_views` (not superuser, not table owner) has SELECT on the base tables the views need and **owns** `core.v_spend_daily`, `v_pacing_base`, `v_invoices`, `v_issues` (`security_barrier`). Postgres applies base-table RLS as the view owner, so policies `TO agent_views` fire for every view read. `agent_ro` has only `USAGE` on `core` + `SELECT` on the four views.
- Policies filter by `core.agent_client_ids()`: a `SECURITY DEFINER` function returning the client ids in `app.user_client_scope` for `current_setting('app.user_id', true)`. Unset, empty, unknown or unscoped user → empty array → **zero rows** (fail closed). Other roles get `USING (true)` policies; rows without a client (platform invoices, client-less issues) are hidden from the agent.
- `EXECUTE` on `set_config(text, text, boolean)` is revoked from `PUBLIC` and granted only to `etl`, `app`, `eval_runner`, so a `SELECT set_config('app.user_id', …)` from the agent fails. The trusted caller sets scope with `SET LOCAL app.user_id = …` in the transaction before the agent's query.
- `agent_ro` has `statement_timeout = 5s` in this database; `TEMPORARY` is revoked from `PUBLIC`.
**Consequences:** access rules live in the database and are proven by `test_roles.py`. `SET`/`RESET` are commands, not functions, so the revoke alone cannot stop them: the stage-3 SQL executor **must** accept only a single `SELECT` statement and reject everything else before it reaches the DB. Revoking a `pg_catalog` function needs a superuser; on a managed Postgres (stage 5) this must be re-checked. New tables read by agent views need a policy.
