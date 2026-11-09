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

## 024 — Generated documents: deterministic PDFs/xlsx, ground truth as exact strings
**Context:** task 1.2 renders the true world into what an agency sees (Excel plans, platform and agency invoices, credit notes). Later steps load the ground truth (stage 1), score LLM extraction against it (stage 2) and plant errors into the documents (task 1.3), so documents must be reproducible and exactly consistent with the world.
**Decision:**
- Libraries: `reportlab` (PDF; `invariant=1` drops timestamps and random IDs), `openpyxl` (xlsx; also needed to read plans in task 1.7), `pypdf` (dev only, tests read PDF text). DejaVu Sans is committed under `generator/fonts/` because the built-in PDF fonts cannot show Lithuanian letters.
- xlsx determinism: write via `ExcelWriter` with fixed core properties (`Workbook.save` stamps "now") and re-zip with fixed entry timestamps. `manifest.json` lists the sha256 of every file.
- Ground-truth JSON stores money as strings (`"1234.56"`) so it parses back to the exact `Decimal`. VAT = `subtotal × 21%`, `ROUND_HALF_UP` to cents.
- Platform invoices: one per ad account × month, lines = delivered spend per `campaign_ref` in the account (= platform) timezone. Three layouts (Meta-, Google-, TikTok-like) differ in headers, column order, number format and wording. Vendors are the configured platform names only (no real legal entities, addresses or logos), and every PDF has a "synthetic test document" footer.
- Client-owned clients get only a media plan: the platform bills them directly, so there is no platform invoice to the agency and no agency invoice.
- Agency invoices: media lines at platform cost per `campaign_ref`, then a negative line per credit note of that period, then one fee line: "Agency commission X% on media" (markup on media net of credits) or "Management fee". Credit notes (count in config) are issued after the platform invoice and before the client invoice of the same period.
- Numbers come from per-entity RNG streams (decision 023) in platform-specific formats; agency numbers are `{prefix}-{CLIENT}-{YYMM}`. Which invoices get a credit note is drawn from one stream over all platform invoices, so changing the client list can change that choice.
**Consequences:** same seed → byte-identical `data/generated/`; reconciliation can compare JSON amounts exactly; error #5 (wrong markup) and #6 (credit not applied) have a dedicated line to corrupt.

## 025 — Planted errors: separate pass, one error per slot, answer key as a file
**Context:** task 1.3 plants errors #1–#9 (plan §5.2) and records each in an answer key. Some errors need a daily-granularity representation; the answer key must stay out of reach of application code (rule 8), and no DB role may write `eval` (decision 004: `eval_runner` is read-only).
**Decision:**
- `generator/errors.py` is a pure pass over the clean world and documents; it returns planted copies and never mutates its inputs. `make generate` (`python -m generator`) runs world → documents → errors → files. The platform API data is written as reported delivery per ad account (`data/generated/delivery/<platform>/<account_id>.json`, no `campaign_ref`); mock APIs (task 1.4) serve it.
- Representation: **#4** the campaign's last-day row is re-dated to the 1st of the next month (whole day; the invoice still bills it; final month excluded). **#9** 1–3 consecutive mid-month rows are absent from the API; the invoice still bills them, so the answer key notes the invoice-vs-delivery gap as a side effect. **#8** clicks ÷ 1.8 at unchanged spend for 3 days (CPC ×1.8, invoices unaffected); magnitude = spend × (1 − 1/1.8). **#7** the same monthly total is re-split over the first 60% of days (zero-spend rows after), so invoices are unaffected; magnitude = peak cumulative overpacing. **#3** from a mid-month day the API reports a new name without the `[campaign_ref]` tag, same campaign id. **#5** the fee line uses contract % ± 2–5 pp and shows that %; magnitude is signed. **#6** the credit line is removed and the markup recomputed.
- Cascade rule: client invoices come from true delivery, so platform-side errors (#1, #2) do not spill into them. Magnitudes are EUR net of VAT.
- Counts: per type `max(errors.min_per_type, round(error_rate × candidates))` (half up; `min_per_type` default 3), capped at the candidates still available; all of them are used if fewer than `min_per_type`; `ConfigError` only when a type has zero candidates. Each error claims `(client, platform, month)` slots (#5/#6 the whole client-month, #4 also the next month); platform invoices with a credit note are excluded from #1, #2, #4. One RNG stream per type and per entity (decision 023); allocation order is fixed, most constrained first.
- The answer key is written only to `data/generated/answer_key.json`. In task 1.11 `evals/load_answer_key.py` loads it into `eval.answer_key` as the DB owner (`DATABASE_URL`), delete + insert in one transaction, run by `make eval-stage1`. Roles and grants are unchanged.
**Consequences:** each planted entity has exactly one answer-key row and the changed PDF/JSON stay consistent; the owner-run loader is the only writer of `eval`. Pipeline loaders must read only `invoices/`, `credit_notes/`, `plans/` and `delivery/`, never `answer_key.json`. `round(0.05 × candidates)` means small pools get exactly `min_per_type`.

## 026 — Mock platform APIs: file-backed, deterministic faults per platform
**Context:** task 1.4 needs Meta-, Google- and TikTok-style APIs (plan §5.3) so the pipeline practises real ingestion. The app must serve the reported delivery files and stay away from the database and the answer key.
**Decision:**
- `mock_platforms/` is a separate FastAPI app with its own `MOCK_PLATFORMS_*` settings (`app.config` requires `DATABASE_URL`). It is configured with the `delivery/` directory itself and only reads `<platform>/*.json` below it, once at startup.
- Dates in the files are already account-local (decision 023) and TikTok accounts are UTC, so the app reformats dates and never converts timezones. Request date params use the same format as the platform's rows. Ranges are inclusive at both ends.
- Rows share field names across platforms; only spend and the date format differ. `spend` and `cost` are decimal strings, `cost_micros` a JSON integer, so money never passes through a float.
- Faults: the nth authorised request on a platform draws from `random.Random("<seed>:<platform>:<n>")`. Auth runs first, so rejected calls do not advance the sequence, and counters are per platform, so one platform's traffic cannot shift another's faults. `MOCK_PLATFORMS_FAULTS_ENABLED=false` turns them off.
- `httpx` is added to the dev group because FastAPI's `TestClient` requires it.
**Consequences:** fault sequences repeat only for the same request order since startup, and a restart resets them. Regenerating data requires restarting the app. Tests build their own files because `data/` is gitignored and absent in CI.

## 027 — Raw responses: one replaceable snapshot per account and period, exact bytes kept
**Context:** task 1.5 stores every API response page. Accounts each restart at page 1, so `UNIQUE (run_id, page)` could hold only one account per run, and the payload does not say which account it came from. JSONB reorders keys and drops whitespace, so it cannot keep a response as received. Rule 10 and plan §7 require that running twice gives identical row counts.
**Decision:**
- Migration `0003` adds `account_id`, `period_start`, `period_end` and `body` (BYTEA, the exact response bytes) to the three raw tables. `payload` stays for querying and is cast from the response text in SQL, so numbers never pass through a Python float. The unique key becomes `(account_id, period_start, period_end, page)`.
- A run replaces the snapshot: all pages of an account are fetched first, then one transaction deletes that account + exact period and inserts them. A failed account stores nothing and keeps its previous snapshot, because a partial account would look like missing delivery days downstream.
- Retries: `backoff = min(cap, base × 2^(n-1))` per error kind and request, no jitter. 429 waits `max(Retry-After, backoff)`, so the cap never shortens the server's wait; 5xx and network errors wait for the backoff. Other statuses and malformed bodies fail at once. Defaults: base 1 s, cap 30 s, 6 attempts for 429, 4 for 5xx.
- Platform and account problems become entries in `IngestResult.failures`; only infrastructure errors (database down) abort the run. A body Postgres refuses (`DataError`, e.g. `\u0000` in JSONB) is an account failure.
- `httpx` moves to the runtime dependencies because `pipelines/ingest.py` imports it in production. The client is synchronous to match plain-function steps (decision 008); `httpx.ASGITransport` is async-only, so tests call the app in-process through FastAPI's `TestClient`, which is an `httpx.Client`.
- `create_etl_engine()` derives the `etl` login from `DATABASE_URL` and `ETL_DB_PASSWORD`.
**Consequences:** raw row counts are stable across runs but only the latest fetch is kept, so earlier responses cannot be replayed. Replacement matches the exact period: a run over a different period is a separate snapshot, and normalization must not sum overlapping ones. `pages_fetched` and `rows_seen` include pages of an account that later failed, so they can exceed what is stored. `etl` cannot write `app.*`, so the runner (task 1.10) needs another role for `app.pipeline_runs`.

## 028 — Normalization: reference file, newest snapshot owns its period, findings written by `app`
**Context:** task 1.6 fills `core.campaigns` and `core.daily_delivery`, but platform data does not say which client owns an account, and no document covers every client: Excel plans have no account ids, industry or fees, and client-owned accounts have no platform invoices. `etl` cannot write `app.*`. Metric columns are `NOT NULL`, so a null metric cannot reach `core`. Snapshots of different periods can overlap (decision 027).
**Decision:**
- The generator writes `data/generated/reference.json` (clients, platforms, ad accounts), standing in for the agency's registry. It is built from the world and carries no error information. This adds one file to the list in decision 025 that pipeline loaders may read. `pipelines/reference.py` upserts it as `etl` before normalizing.
- `METRIC_MAPS` in `pipelines/normalize.py` is the source of truth for field mapping; `core.platforms.metric_map` stores a copy. Spend is strict: decimal strings or integer micros, never a float, quantized to cents with `ROUND_HALF_UP`.
- Dates stay account-local as reported, with no conversion (decision 023). This replaces the "timezones -> UTC and agency date" wording in task 1.6; reconciliation joins `core.ad_accounts.timezone` when it needs it.
- Snapshots of an account are read newest first, and a newer snapshot owns every date in its period, so a day it omits stays missing rather than being filled from an older one.
- A campaign is keyed by `platform_campaign_id`. `name` is the earliest reported name, which carries `[campaign_ref]` for task 1.7; other names become aliases (`source='rule'`, confidence 1). `io_id` stays NULL and is outside the upsert's `SET`.
- Null metrics stay on the in-memory record, are not upserted and still reach quality. Unreadable rows and accounts missing from the reference data are reported, never guessed.
- Quality checks are pure. `quality.write_findings` logs in as `app` and upserts on `dedup_key` (with the reason appended for rejected rows); status and decision columns are never updated. Grants are unchanged. A one-day gap at month end whose only later row is the 1st of the next month is `month_boundary_shift` rather than `missing_days`.
**Consequences:** the active span is first to last reported day, so a gap at the very start or end of a flight is invisible until IO dates exist. Upsert-only means a day that disappears from a later snapshot stays in `core`, though quality still reports it. An issue whose problem goes away stays open. Trailing zero-spend runs also fit pacing drift, so task 1.11 must decide how `quality_zero_spend` counts. `core.clients` has no `code` column while plans and invoices use `client_code`; `reference.json` keeps the code and task 1.7 must resolve it.

## 029 — Plan and invoice loading: client codes, documents as written, ownership of columns
**Context:** task 1.7 loads Excel plans and invoice documents into `core`. Both identify clients by `client_code`, which `core.clients` did not have, and the local database already held clients. A real agency has only PDFs, but extraction arrives in stage 2. Planted errors must reach `core` untouched for task 1.8 to find them.
**Decision:**
- Migration `0004` adds `core.clients.code` (unique, NOT NULL). Existing rows are matched by name to `reference.json` and the migration aborts if one cannot be matched; an empty table never reads the file. `upsert_clients` now conflicts on `code`, so a renamed client stays the same row. Roles, grants and RLS are unchanged.
- Stage 1 reads the `.json` next to each PDF, under `invoices/` and `credit_notes/` only. It is a stand-in for extraction and holds what the document says, planted errors included. `file_path` stores the PDF path relative to the data directory.
- Nothing is validated against anything else: duplicates, wrong markup and unapplied credits load as written. `credit_notes.amount` is the note's net `subtotal`, positive as printed, because the credit line on client invoices is `-subtotal` and reconciliation is net of VAT.
- openpyxl returns numeric cells as int or float, so `Decimal(str(value))` runs at that boundary and is quantized to cents; no float goes further. JSON money must be a decimal string.
- An unreadable document is rejected whole and reported in the result, and the run continues. Two files with the same vendor and number cannot both be stored: the first in path order is kept and the other is reported.
- Upserts leave `invoices.status`, `invoice_lines.campaign_id`, `invoice_lines.extracted_confidence` and `insertion_orders.rate_type` alone. Lines a re-loaded invoice no longer has are deleted.
- Plan loading sets `campaigns.io_id` from the `[campaign_ref]` tag in `campaigns.name`, matched on client, platform and ref. A campaign with no tag, no matching IO, or a ref fitting several IOs is counted as unlinked and not guessed.
**Consequences:** a migration can depend on a gitignored file when the table has rows. An invoice whose file disappears stays in `core`. A credit note's `campaign_ref`, period and VAT are not stored; the original invoice gives the period. Rejections live only in the result objects until the runner (task 1.10) records them. Plans must load after normalization for campaigns to link. `invoice_lines.campaign_id` stays NULL until task 1.8.
