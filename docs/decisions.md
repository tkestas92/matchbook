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
**Decision:** `Decimal` in Python, `NUMERIC(12,2)` in PostgreSQL for all billable amounts, single currency EUR. The one exception is internal pipeline cost (decision 014).
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

## 013 — Ad accounts carry the reporting timezone; IO unique includes start_date
**Context:** Meta and Google report in the *account* timezone (plan §5.3), and a campaign without an IO had no link to a client. The same `campaign_ref` can be reused on a later flight.
**Decision:** add `core.ad_accounts` (client, platform, external account id, timezone); every campaign belongs to an ad account. `core.platforms.timezone` stays as the platform default. `core.insertion_orders` is unique on `(client_id, platform_id, campaign_ref, start_date)`.
**Consequences:** delivery converts from the right timezone per account; every campaign has a client before IO matching; renewals do not collide with prior flights.

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

## 017 — Kaggle distributions: stdlib lognormal fits
**Context:** the generator needs realistic CTR, CPC, CPM and CVR; the Kaggle dataset has no dates, so only ratios can be used.
**Decision:** `generator/build_distributions.py` drops unusable rows (zero clicks, zero or negative spend, impressions below clicks), computes per-row ratios and stores moments, percentiles and a lognormal fit (MLE on ln x, stdlib only, no new dependency) in the committed `generator/distributions.json`. Exact zeros (CVR) are recorded as `zero_share` and excluded from the fit. The CSV is never committed and tests read only the JSON.
**Consequences:** CI needs no dataset; lognormal fits CPC loosely (see `ks_distance`), which is acceptable for synthetic data.

## 018 — Local Postgres bound to localhost only
**Context:** Docker Compose published Postgres on all interfaces (`0.0.0.0`), which is unnecessary for local development.
**Decision:** bind the host port to `127.0.0.1` only (`127.0.0.1:${POSTGRES_PORT}:5432` in `docker-compose.yml`).
**Consequences:** the database is reachable from the host machine but not from other devices on the network.

## 019 — Ruff + pre-commit as the lint gate
**Context:** stage 0 needs a single, reproducible style/lint check for agents and humans.
**Decision:** configure ruff in `pyproject.toml` (line-length 100, target `py312`, select `E,F,I,UP,B`, ignore `E501`); `make lint` / `make format` wrap ruff; `.pre-commit-config.yaml` runs ruff `--fix` and ruff format.
**Consequences:** one tool for lint and format; CI and local hooks share the same rules.

## 020 — CI on GitHub Actions with a throwaway Postgres
**Context:** PRs need `migrate` + `lint` + `test` without local Docker or repository secrets.
**Decision:** `.github/workflows/ci.yml` runs on push to `main` and on pull requests; one `test` job on `ubuntu-latest` with a Postgres 16 service container and throwaway credentials/passwords in the workflow env.
**Consequences:** every PR gets a green/red check; no secrets to rotate for the basic pipeline.

## 021 — Config default tests ignore the outer environment
**Context:** CI sets `APP_ENV=test`; a settings-default test failed because it inherited that value.
**Decision:** tests that assert Settings defaults clear those env vars with `monkeypatch.delenv(..., raising=False)` before constructing `Settings(_env_file=None)`.
**Consequences:** default-value tests pass locally and in CI regardless of workflow or shell env.

## 022 — Kaggle gives the shape, config.yaml gives the level
**Context:** the Kaggle dataset's absolute levels are unrealistic for EUR ad buying (CTR ≈ 0.02%, CPM ≈ 0.3), but its spread is still a useful hint of how much campaigns differ.
**Decision:** `generator/world.py` keeps the lognormal `sigma` from `distributions.json` and sets the mean per platform from `ratios` in `generator/config.yaml` (e.g. Meta CPM ≈ 6 EUR, CTR ≈ 1%) using `mu = ln(mean) − sigma²/2`. Base CPM/CTR/CVR are sampled once per campaign; days wobble around the base with `sigma × daily_ratio_sigma_factor`. CPC is derived (spend / clicks), never sampled, so ratios stay consistent. CVR is capped so conversions ≤ clicks; Kaggle `zero_share` sets the chance of a zero-conversion day.
**Consequences:** realistic magnitudes with data-driven variation; levels are tuned in one YAML file without rebuilding `distributions.json`.

## 023 — Deterministic generator: per-entity RNG streams, Decimal at generation
**Context:** the same seed must always produce the same world (plan §5.1), and later steps (documents, errors) must not shift when the client list changes.
**Decision:** every entity draws from its own `random.Random("<seed>:<kind>:<key>")` (string seeds are SHA-512-hashed, independent of `PYTHONHASHSEED`). Clients are listed explicitly in `config.yaml`. Money is quoted in YAML, parsed to `Decimal` and quantized to cents (`ROUND_HALF_UP`) when generated; the daily plan's rounding remainder goes to the last day so Σplan equals the IO budget exactly. One campaign per IO, `campaign_ref` = `{CLIENT}-{PLATFORM}-{YYMM}`. Delivery dates are the ad account's local dates. `world_digest()` (SHA-256 of canonical JSON) fingerprints a world. PyYAML becomes a runtime dependency to read `config.yaml`.
**Consequences:** reproducible worlds testable by digest; adding a client does not change other clients' data; pipeline and reconciliation can compare money exactly.
