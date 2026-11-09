# Matchbook — project plan

> Three-way media reconciliation & reporting for agencies.

## 1. Problem

Media agencies still do a lot of this by hand every week or month:

- log into each ad platform (Meta, Google, TikTok…) and export spend;
- match vendor invoices against what was actually delivered and what was planned in the media plan / insertion order (IO);
- apply the client's billing model (markup or management fee) and issue client invoices;
- rebuild the same report for every client;
- notice pacing problems (over/under-spend) while campaigns are still running.

Typical pain points:

- **Three-way match:** plan/IO ↔ platform delivery ↔ invoice. The IO acts as the agency's purchase order.
- **Two invoice layers:** platform → agency, and agency → client.
- **Billing models:** markup on spend (typically 10–20%), pass-through + management fee, or client-owned ad accounts.
- **Messy data:** campaign names change mid-month; platforms report in a different timezone than invoices; every platform names metrics differently.
- **Credits / make-goods** must be linked to the original invoice.
- **Plans live in Excel** on a planner's laptop, delivery data lives in platforms.

Commercial tools exist (Basis, Improvado, Funnel, Cassidy…), which confirms the problem is real.
Matchbook is an open, simplified version built to show production-grade applied AI engineering.

## 2. Principles

1. **Rules first, AI second.** Deterministic code for everything computable; LLMs only for messy documents, fuzzy name matching, summaries and natural-language questions.
2. **AI proposes, a human approves.** Nothing is sent or changed without approval; every decision is audited.
3. **Measured, not assumed.** Synthetic data with a planted-error answer key makes every module measurable (found / missed / false alarms).
4. **Secure by design.** Least privilege in the database, not in prompts.

## 3. Architecture (nightly flow)

```
[Ad platform APIs]   [Excel media plans]   [PDF invoices]
        |                    |                   |
   1 Ingest  ->  2 Normalize + quality  ->  3 Invoice extraction (LLM, stage 2)
                                                 |
        4 Reconciliation (rules + AI)  ->  5 Pacing
                                                 |
        6 Anomalies (ML)  ->  7 Report (Excel + summary)
                                                 |
        8 Approval queue  ->  9 Notification (email/Slack)

10 Agent + MCP server  -- read-only access to the same data
11 Reliability: evals, tracing, cost, audit      12 ROI dashboard
```

## 4. Modules

| # | Module | What it does | Stage |
|---|---|---|---|
| 1 | Ingest | Mock platform APIs (different field names, pagination, date formats, 429/500, auth token) + Excel plan upload | 1 |
| 2 | Normalize & quality | Metric dictionary → one schema; checks for missing days, duplicates, negatives, timezone shift; idempotent upserts | 1 |
| 3 | Invoice extraction | PDF → strict JSON schema with per-field confidence; PII masking before LLM; low confidence → human review | 2 |
| 4 | Reconciliation | IO ↔ delivery ↔ platform invoice ↔ client invoice; billing models; credit notes; AI only for campaign-name matching | 1 (rules), 2 (AI) |
| 5 | Pacing | Daily plan curve, cumulative actual vs plan, end-of-month forecast, green/yellow/red | 1 |
| 6 | Anomalies | Isolation Forest + statistics on spend, CPC, CTR | 4 |
| 7 | Report | Excel per client/platform (invoice, delivered, variance, status, notes) + LLM summary with number checks; Power BI | 4–5 |
| 8 | Approval queue | Issue cards with proposed actions; approve / edit / reject; audit log | 4 |
| 9 | Notifications | Morning digest | 4 |
| 10 | Agent + MCP | Tool-calling agent (get_spend, get_pacing, list_issues, run_readonly_sql); same tools via authenticated MCP server | 3 |
| 11 | Reliability | Evals in CI, Langfuse tracing, model comparison (quality / cost / latency), fallback model, cost caps | 2–5 |
| 12 | ROI dashboard | Documents processed, € discrepancies found, hours saved (user-editable hours per client) | 5 |

## 5. Data

### 5.1 Synthetic data generator
The generator works in three steps:
1. **Create the true world** — what was planned, delivered and spent.
2. **Produce documents** the way an agency would see them — API responses, Excel plans, PDF invoices.
3. **Plant errors** into the documents and record each one in `eval.answer_key`.

- Deterministic: same `seed` → same world.
- Configured in `generator/config.yaml` (clients, period, platforms, error rate).
- Ratios (CTR, CPC, conversion rate) come from the Kaggle "Clicks Conversion Tracking" dataset (`generator/distributions.json`); daily time series are generated (weekday rhythm, seasonality, noise).

Default world: **8 fictional Lithuanian clients**, **May–October 2026** (November = current month for pacing), **Meta, Google, TikTok**.

1. Clients — industry, billing model (markup 10–20% / management fee / client-owned), 2–3 platforms each.
2. Media plans / IOs — budget, dates, campaign codes per client/month/platform; one Excel file per client.
3. Daily delivery — budget split by day weights; actual = plan × noise (±5–10%); impressions/clicks/conversions from CPM/CTR/CVR.
4. Platform invoices to the agency (PDF) — monthly per platform, computed in the platform timezone; 3 layouts in stage 1.
5. Agency invoices to clients (PDF) — per billing model.
6. Credit notes — linked to originals.
7. A "ground-truth" JSON per invoice (used by the stage-1 pipeline instead of LLM extraction; later used to score extraction).

### 5.2 Planted errors (`eval.answer_key`)

| # | Error | How planted | Detected by | Detectable in stage 1 |
|---|---|---|---|---|
| 1 | Invoice above delivery | Invoice total +3–15% | Reconciliation | yes |
| 2 | Duplicate invoice | Same invoice, new number | Reconciliation | yes |
| 3 | Campaign renamed mid-month | API returns new name from mid-month | Reconciliation (AI) | no — stage 2 |
| 4 | Timezone shift | Last-day spend lands in next month | Reconciliation | yes |
| 5 | Wrong markup | Wrong % on client invoice | Reconciliation | yes |
| 6 | Credit not applied | Credit note issued, client invoice ignores it | Reconciliation | yes |
| 7 | Pacing drift | Campaign spends its budget in 60% of the month | Pacing | yes |
| 8 | CPC spike | Cost ×1.8 for a few days | Anomalies | no — stage 4 |
| 9 | Missing days | API omits some days | Data quality | yes |
| 10 | Prompt-injection invoice | Hidden text in PDF | Security / extraction | no — stage 2 |

Each record: error type, affected entity, period, € magnitude, expected detector.

### 5.3 Mock platform APIs

| | Meta | Google | TikTok |
|---|---|---|---|
| Spend field | `spend` | `cost_micros` (EUR × 1,000,000) | `cost` |
| Pagination | cursor | page token | page + page_size |
| Date format | `2026-10-15` | `20261015` | `2026-10-15 00:00:00` |
| Timezone | account tz | account tz | UTC |

All: bearer token required; occasional `429` (must back off and retry) and `500` (one failing platform must not stop the run).

## 6. Data model

### 6.1 Schemas
| Schema | Contents | Access |
|---|---|---|
| `raw` | Data exactly as received from platforms/files | ETL only |
| `core` | Normalized clients, platforms, IOs, campaigns, daily delivery, invoices | App; agent via views only |
| `app` | Users/roles, issues (approval queue), audit log, pipeline runs | App |
| `eval` | `answer_key` | Evals only |

`eval` is isolated so that no module can "find" errors by reading the answers.

### 6.2 Tables
Defined in `backend/app/db/models/`, created by `backend/migrations/`.

| Table | Key fields |
|---|---|
| `raw.meta_responses`, `raw.google_responses`, `raw.tiktok_responses` | id, run_id, account_id, period_start, period_end, page, fetched_at, payload (JSONB), body (exact bytes) — unique (account_id, period_start, period_end, page) |
| `core.clients` | id, code (unique), name (unique), industry, billing_model (markup/mgmt_fee/client_owned), markup_pct, mgmt_fee |
| `core.platforms` | id, name (unique), timezone (default), metric_map |
| `core.ad_accounts` | id, client_id, platform_id, external_account_id, timezone — unique (platform_id, external_account_id) |
| `core.insertion_orders` | id, client_id, platform_id, campaign_ref, start_date, end_date, budget, rate_type (cpm/cpc/flat) — unique (client_id, platform_id, campaign_ref, start_date) |
| `core.campaigns` | id, platform_id, ad_account_id, platform_campaign_id, name, io_id (nullable) — unique (platform_id, platform_campaign_id) |
| `core.campaign_aliases` | campaign_id, alias (PK), source (rule/ai/human), confidence |
| `core.daily_delivery` | id, campaign_id, date, spend, impressions, clicks, conversions — unique (campaign_id, date); index (date) |
| `core.invoices` | id, kind (platform/client), vendor, number, client_id, platform_id, issue_date, period_start, period_end, subtotal, vat, total, file_path, status — unique (vendor, number); platform needs platform_id, client needs client_id |
| `core.invoice_lines` | id, invoice_id, line_no, campaign_ref, campaign_id (resolved), description, amount, extracted_confidence — unique (invoice_id, line_no) |
| `core.credit_notes` | id, original_invoice_id, vendor, number, issue_date, amount, reason, file_path — unique (vendor, number) |
| `core.reconciliation_results` | id, period (1st of month), client_id, platform_id, planned, delivered, invoiced, client_invoiced, expected_client_amount, variance, status — unique (period, client_id, platform_id) |
| `app.users` | id, email (unique), role (admin/planner/finance) |
| `app.user_client_scope` | user_id, client_id (PK) — client scope per user |
| `app.issues` | id, dedup_key (unique), type, severity, client_id, platform_id, period, entity_type, entity_id, amount, explanation, proposed_action, status, decided_by, decided_at, decision_note |
| `app.audit_log` | id, actor, action, entity, entity_id, payload (insert-only, enforced by trigger) |
| `app.pipeline_runs` | id (UUID), started_at, finished_at, status, cost_eur, tokens, details |
| `eval.answer_key` | id, error_type, entity_ref, period_start, period_end, magnitude, expected_detector, details — no FKs |

All tables: `created_at`. Pipeline-upserted tables: `updated_at` (trigger). Pipeline-written tables: `run_id` (UUID, no FK).
Money: `NUMERIC(12,2)`, EUR (exception: `cost_eur` is `NUMERIC(12,6)`, decision 014). Timestamps: `timestamptz`, UTC.
Financial FKs use `ON DELETE RESTRICT`. Conventions: decisions 012–014.

### 6.3 Database roles
| Role | Rights |
|---|---|
| `etl` | write `raw`, `core` |
| `app` | read `core`, write `app` |
| `agent_ro` | read-only on curated `core` views, with RLS |
| `eval_runner` | read everything incl. `eval` |

## 7. Pipeline (stage 1)
Each step is a plain Python function runnable via `make pipeline`; Airflow only orchestrates them.

0. Load reference data (clients, platforms, ad accounts) → `core` (decision 028)
1. Ingest each platform → `raw` (retries with backoff; failures recorded, other platforms continue)
2. Normalize via metric dictionary → `core.daily_delivery` (upsert on (campaign_id, date))
3. Quality checks
4. Load Excel plans → `core.insertion_orders`; link `core.campaigns.io_id` (decision 029)
5. Load invoices (stage 1: ground-truth JSON; stage 2: LLM extraction)
6. Reconcile → `core.reconciliation_results` + `app.issues`
7. Pacing
8. Record run → `app.pipeline_runs`

Idempotency test: running the pipeline twice yields identical row counts.

## 8. Reconciliation rules (stage 1)
Per client × platform × month:
1. Link invoice lines to campaigns by code; unlinked lines → issue "needs matching".
2. Compute delivered spend in both platform timezone and agency timezone.
3. Invoice vs delivered: variance > €1 **and** > 0.5% → issue. If the timezone difference explains it → classify as "timezone shift", not "overcharge".
4. Delivered vs IO budget: overspend or large underspend.
5. Client invoice vs expected amount from billing model (wrong markup, credit not applied).
6. Duplicates: same vendor, amount and period with a different number.

Issue explanations in stage 1 come from templates (no LLM).

## 9. Pacing
- Daily plan = monthly budget × day weights.
- Compare cumulative actual vs cumulative plan daily.
- Forecast = actual to date + (last-7-day average × remaining days).
- Status (configurable): green 98–115% of budget; yellow 85–98% or 115–125%; red otherwise.

## 10. Security (summary — full threat model in `docs/threat-model.md` from stage 2)
Framework: OWASP Top 10 for LLM Applications.
- Prompt injection via invoices: extracted text is data only; extractor has no tools; strict JSON output; human approves; malicious invoices in evals.
- Text-to-SQL: `agent_ro` SELECT-only on views, RLS, `statement_timeout`, mandatory LIMIT, SQL validation. Protection lives in the DB, not the prompt.
- MCP: authenticated; read-only tools only; actions go through the approval queue.
- Privacy: mask IBAN/emails/phones before LLM; Azure OpenAI EU region; data minimization.
- Auth: roles admin / planner / finance; insert-only audit log.
- Uploads: size limits, real type check, never executed.
- Secrets: `.env`, Azure Key Vault, gitleaks in CI.
- Cost caps per run and per user.
- Dependencies: Dependabot, pip-audit, Trivy.

## 11. Stages

| Stage | Scope | Done when |
|---|---|---|
| 0 | Repo, Docker Postgres, Alembic schemas/tables, DB roles + RLS skeleton, lint/test, minimal CI, Kaggle distributions | `make up && make migrate && make test` green; test proves `agent_ro` cannot write or read `eval` |
| 1 | Generator + answer key, mock APIs, ingest/normalize/quality, reconciliation rules, pacing, integration eval, Airflow wrapper | Pipeline finds 100% of rule-detectable planted errors with ~no false alarms |
| 2 | LLM invoice extraction with confidence, PII masking, AI name matching, eval runner, malicious invoices, Langfuse | Extraction accuracy reported; all malicious invoices rejected; error #3 now detected |
| 3 | Agent with tools, SQL guards, authenticated MCP server, SQL eval set | Correct answers from Claude Desktop via MCP |
| 4 | Anomalies, Excel report + checked summary, approval queue + audit, notifications, Next.js UI | Morning email → approve/reject issues in UI |
| 5 | Azure deploy (Container Apps, Azure OpenAI EU, Key Vault), CI with evals + security scans, model comparison, cost caps, ROI dashboard, Power BI | Public demo; PRs blocked when evals regress |
| 6 | README (problem, architecture, before/after, threat model, eval results), 2-min demo video, portfolio card | One link explains the project in 2 minutes |

## 12. Tooling & workflow
- Editor: Cursor. Agents: Claude Code (multi-file features, planning) + Cursor agent (UI, small fixes).
- One task = one session = one branch → plan mode → code + tests → `make lint test` → PR → merge.
- Agent rules: `AGENTS.md` (`CLAUDE.md` imports it).
