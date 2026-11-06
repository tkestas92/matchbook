# AGENTS.md — rules for AI coding agents (Claude Code, Cursor)

## Project
**Matchbook** — three-way media reconciliation & reporting for agencies.
Every night it collects ad-platform data, reads invoices, reconciles plan ↔ delivery ↔ invoice,
tracks pacing, flags anomalies and prepares a morning report with proposed actions for a human to approve.
Full spec: `docs/plan.md`. Architecture decisions: `docs/decisions.md`. Current tasks: `docs/tasks/`.

## Stack
- Python 3.12, managed with **uv** (`uv sync`, `uv run ...`, `uv add ...`). Never use pip directly.
- FastAPI, SQLAlchemy 2.0 (typed ORM), Alembic, PostgreSQL 16, pytest, ruff.
- Later stages: Apache Airflow, Langfuse, scikit-learn, Next.js + Tailwind (frontend/), MCP Python SDK.
- Local infra via Docker Compose.

## Commands
- `make up` / `make down` — start/stop local services
- `make migrate` — apply Alembic migrations
- `make test` — run pytest
- `make lint` — ruff check + format check
- `make generate` — generate synthetic data (stage 1+)
- `make pipeline` — run the nightly pipeline without Airflow (stage 1+)
- `make eval` — run evals (stage 2+)

Always run `make lint` and `make test` before saying a task is done.

## Core rules
1. **Rules first, AI second.** Anything computable with plain code (sums, dates, percentages, matching by ID) is plain code. LLMs only where code cannot do it.
2. **AI proposes, a human approves.** The system never sends messages or changes financial data on its own.
3. **Every new function has a test.** Bug fix → first a failing test, then the fix.
4. **Money is `Decimal`, never `float`.** DB type `NUMERIC(12,2)`. Currency: EUR only.
5. **Time:** store timestamps in UTC. Reporting dates use the agency timezone `Europe/Vilnius`. Each platform has its own timezone in `core.platforms`.
6. **All LLM calls go through `backend/app/llm/`.** No direct SDK calls elsewhere.
7. **No secrets in code.** Only `.env` (gitignored); keep `.env.example` updated.
8. **The `eval` schema is for evals only.** Application, pipeline, agent and MCP code must never read `eval.*`.
9. **Do not change DB roles, grants, RLS policies or existing migrations** unless the task explicitly says so. New schema changes = new migration.
10. **Idempotency:** pipeline steps can run twice without duplicating data (upsert on natural keys, `run_id` on every row).

## Code style
- Type hints everywhere. Small functions. Clear names over comments.
- Code, comments, docs, commit messages: **English**.
- Commit messages: conventional style (`feat:`, `fix:`, `test:`, `docs:`, `chore:`).

## Scope discipline
- One task = one module. Do not refactor unrelated code.
- Do not add new dependencies without a stated reason in the PR/commit message.
- If the task is ambiguous, ask before writing code.
- If you make an architectural choice, add a short entry to `docs/decisions.md`.

## Repo layout
```
backend/app/{api,db,reconciliation,extraction,agent,anomalies,llm}
backend/migrations/   backend/tests/
generator/            mock_platforms/     pipelines/
mcp_server/           evals/              frontend/
docs/                 .github/workflows/
```
