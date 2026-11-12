"""Run the nightly pipeline (plan section 7) once, with one `run_id`, and record it.

Each `step_*` function is one step and can be called on its own; `STEPS` is the only place
that fixes their order. The run record in `app.pipeline_runs` is written as `app`, because
`etl` cannot write `app.*` (decision 027).

Run: `make pipeline` (needs `make mock-apis` running). Exit codes: 0 succeeded, 1 failed,
2 partial (decision 032).
"""

import argparse
import sys
import time
import traceback
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from app.config import get_settings
from app.db.models.app import PipelineRun
from app.db.session import create_app_engine, create_etl_engine
from app.extraction.extract import ExtractionConfig
from app.llm.client import LLMClient, create_llm_client, new_run_ledger
from app.llm.cost import monthly_spend
from sqlalchemy import Engine, func, insert, update

from pipelines.extract_invoices import (
    LoadExtractedResult,
    known_vendors_from_config,
    load_invoices_llm,
    review_list,
)
from pipelines.ingest import (
    PLATFORMS,
    REQUEST_TIMEOUT_SECONDS,
    IngestResult,
    IngestSettings,
    run_ingest,
)
from pipelines.load_invoices import LLM, InvoiceSettings, load_invoices
from pipelines.load_plans import DEFAULT_DATA_DIR, RejectedDocument, load_plans
from pipelines.match_names import LLM as NAME_LLM
from pipelines.match_names import NameSettings, match_names
from pipelines.normalize import NormalizeResult, run_normalize
from pipelines.pacing import pace
from pipelines.quality import run_quality_checks, write_findings
from pipelines.reconcile import reconcile
from pipelines.reference import load_reference

REFERENCE_FILE = "reference.json"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
PARTIAL = "partial"
EXIT_CODES = {SUCCEEDED: 0, FAILED: 1, PARTIAL: 2}
# Counts that mean some input was not loaded, so the run is partial rather than succeeded.
# Extraction review items are not here: like quality findings, they are reviewed outcomes.
PARTIAL_COUNTS = (
    "failures",
    "rejected_documents",
    "skipped_cost_cap",
    "skipped_provider_error",
)
MAX_ERROR_LENGTH = 2000

Counts = dict[str, Any]


class IngestUnavailable(RuntimeError):
    """No platform could list its accounts, so nothing new can be ingested."""


@dataclass
class RunContext:
    run_id: uuid.UUID
    data_dir: Path
    etl: Engine
    app: Engine
    client: httpx.Client
    ingest_settings: IngestSettings
    agency_timezone: str
    as_of: date | None = None
    normalized: NormalizeResult | None = None  # Set by normalize, read by quality.
    invoice_settings: InvoiceSettings = field(default_factory=InvoiceSettings)
    name_settings: NameSettings = field(default_factory=NameSettings)
    # Called only by the llm invoice source and the llm name judge, so the default modes need
    # no API key; through `llm_client` it is called at most once per run.
    llm_client_factory: "Callable[[RunContext], LLMClient] | None" = None
    _llm_client: LLMClient | None = field(default=None, init=False, repr=False)

    def llm_client(self) -> LLMClient:
        """The run's one client, built on first use, so one ledger caps every step's calls."""
        if self._llm_client is None:
            factory = self.llm_client_factory or default_llm_client
            self._llm_client = factory(self)
        return self._llm_client


StepFn = Callable[[RunContext], Counts]


def default_llm_client(ctx: RunContext) -> LLMClient:
    """The production client; this month's spend before the run feeds the monthly cap."""
    settings = get_settings()
    spent = monthly_spend(
        ctx.app, datetime.now(UTC), ctx.agency_timezone, exclude_run_id=ctx.run_id
    )
    return create_llm_client(settings, new_run_ledger(settings, spent))


@dataclass
class RunOutcome:
    run_id: uuid.UUID
    status: str = RUNNING
    steps: list[dict[str, Any]] = field(default_factory=list)
    error: dict[str, str] | None = None

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.status]


def rejected_list(rejected: Sequence[RejectedDocument]) -> list[dict[str, str]]:
    return [{"path": r.path, "reason": r.reason} for r in rejected]


def check_ingest_reachable(result: IngestResult) -> None:
    unreachable = {f.platform for f in result.failures if f.account is None}
    if unreachable == {platform.name for platform in PLATFORMS}:
        reasons = "; ".join(f"{f.platform}: {f.reason}" for f in result.failures)
        raise IngestUnavailable(
            f"No platform API answered ({reasons}). Is it running? Start it with `make mock-apis`."
        )


def step_reference(ctx: RunContext) -> Counts:
    result = load_reference(ctx.run_id, ctx.data_dir / REFERENCE_FILE, ctx.etl)
    return {
        "clients": result.clients,
        "platforms": result.platforms,
        "ad_accounts": result.ad_accounts,
    }


def step_ingest(ctx: RunContext) -> Counts:
    result = run_ingest(ctx.run_id, ctx.ingest_settings, ctx.client, ctx.etl)
    check_ingest_reachable(result)
    return {
        "pages_fetched": result.pages_fetched,
        "rows_seen": result.rows_seen,
        "retries": result.retries,
        "failures": len(result.failures),
        "failure_list": [
            {"platform": f.platform, "account": f.account, "reason": f.reason}
            for f in result.failures
        ],
    }


def step_normalize(ctx: RunContext) -> Counts:
    result = run_normalize(ctx.run_id, ctx.etl)
    ctx.normalized = result
    return {
        "records": len(result.records),
        "rejected_rows": len(result.rejected),
        "unknown_accounts": len(result.unknown_accounts),
        "campaigns_written": result.campaigns_written,
        "aliases_written": result.aliases_written,
        "delivery_written": result.delivery_written,
    }


def step_quality(ctx: RunContext) -> Counts:
    """Needs this run's normalize result: rejected rows and unknown accounts are not stored."""
    if ctx.normalized is None:
        raise RuntimeError("Quality needs the normalize result of the same run")
    written = write_findings(ctx.run_id, run_quality_checks(ctx.normalized), ctx.app)
    return {"findings_written": written}


def step_load_plans(ctx: RunContext) -> Counts:
    result = load_plans(ctx.run_id, ctx.data_dir, ctx.etl)
    return {
        "orders_written": result.orders_written,
        "campaigns_linked": result.campaigns_linked,
        "campaigns_unlinked": result.campaigns_unlinked,
        "rejected_documents": len(result.rejected),
        "rejected_list": rejected_list(result.rejected),
    }


def step_match_names(ctx: RunContext) -> Counts:
    client = ctx.llm_client() if ctx.name_settings.judge == NAME_LLM else None
    result = match_names(ctx.run_id, ctx.etl, ctx.app, client)
    return {
        "candidates": result.candidates,
        "already_known": result.already_known,
        "judged": result.judged,
        "issues_written": result.issues_written,
        "skipped_cost_cap": result.skipped_cost_cap,
        "skipped_provider_error": result.skipped_provider_error,
        "llm_cost_eur": str(result.llm_cost_eur),  # A string, so the JSON keeps it exact.
        "llm_tokens": result.llm_tokens,
        "llm_calls": result.llm_calls,
    }


def extracted_counts(result: LoadExtractedResult) -> Counts:
    return {
        "documents_seen": result.documents_seen,
        "skipped_unchanged": result.skipped_unchanged,
        "skipped_cost_cap": result.skipped_cost_cap,
        "skipped_provider_error": result.skipped_provider_error,
        "invoices_written": result.invoices_written,
        "lines_written": result.lines_written,
        "lines_removed": result.lines_removed,
        "credit_notes_written": result.credit_notes_written,
        "extracted": result.statuses["extracted"],
        "needs_review": result.statuses["needs_review"],
        "rejected": result.statuses["rejected"],
        "failed": result.statuses["failed"],
        "review_list": review_list(result.review),
        "llm_cost_eur": str(result.llm_cost_eur),  # A string, so the JSON keeps it exact.
        "llm_tokens": result.llm_tokens,
        "llm_calls": result.llm_calls,
    }


def step_load_invoices_llm(ctx: RunContext) -> Counts:
    result = load_invoices_llm(
        ctx.run_id,
        ctx.data_dir,
        ctx.etl,
        ctx.app,
        ctx.llm_client(),
        ExtractionConfig.from_settings(get_settings()),
        known_vendors_from_config(ctx.invoice_settings.known_vendors_config),
    )
    return extracted_counts(result)


def step_load_invoices(ctx: RunContext) -> Counts:
    if ctx.invoice_settings.source == LLM:
        return step_load_invoices_llm(ctx)
    result = load_invoices(ctx.run_id, ctx.data_dir, ctx.etl)
    return {
        "invoices_written": result.invoices_written,
        "lines_written": result.lines_written,
        "lines_removed": result.lines_removed,
        "credit_notes_written": result.credit_notes_written,
        "rejected_documents": len(result.rejected),
        "rejected_list": rejected_list(result.rejected),
    }


def step_reconcile(ctx: RunContext) -> Counts:
    result = reconcile(ctx.run_id, ctx.etl, ctx.app, ctx.agency_timezone)
    return {
        "lines_linked": result.lines_linked,
        "results_written": result.results_written,
        "issues_written": result.issues_written,
        "gap_notes_attached": result.gap_notes_attached,
        "gap_notes_unattached": result.gap_notes_unattached,
    }


def step_pacing(ctx: RunContext) -> Counts:
    result = pace(ctx.run_id, ctx.app, ctx.as_of)
    return {
        "as_of": result.as_of.isoformat() if result.as_of else None,
        "orders_paced": result.orders_paced,
        "orders_skipped": result.orders_skipped,
        "statuses": result.statuses,
        "issues_written": result.issues_written,
        "quality_issues_taken_over": result.quality_issues_taken_over,
    }


# Quality runs before reconcile and pacing, which update the issues it writes (decisions 030,
# 031); plans load after normalize, which creates the campaigns they link (decision 029);
# renames are matched once plans are linked and before reconcile (decision 047).
STEPS: tuple[tuple[str, StepFn], ...] = (
    ("reference", step_reference),
    ("ingest", step_ingest),
    ("normalize", step_normalize),
    ("quality", step_quality),
    ("load_plans", step_load_plans),
    ("match_names", step_match_names),
    ("load_invoices", step_load_invoices),
    ("reconcile", step_reconcile),
    ("pacing", step_pacing),
)


def start_run(app: Engine, run_id: uuid.UUID) -> None:
    with app.begin() as conn:
        conn.execute(insert(PipelineRun).values(id=run_id, status=RUNNING))


def finish_run(
    app: Engine,
    run_id: uuid.UUID,
    status: str,
    details: dict[str, Any],
    cost: tuple[Decimal, int] = (Decimal(0), 0),
) -> None:
    cost_eur, tokens = cost
    with app.begin() as conn:
        conn.execute(
            update(PipelineRun)
            .where(PipelineRun.id == run_id)
            .values(
                status=status,
                finished_at=func.now(),
                details=details,
                cost_eur=cost_eur,
                tokens=tokens,
            )
        )


def run_cost(steps: Sequence[dict[str, Any]]) -> tuple[Decimal, int]:
    """LLM spend (EUR) and tokens over the steps' counts; works from Airflow's step records too."""
    cost_eur = sum((Decimal(step["counts"].get("llm_cost_eur", "0")) for step in steps), Decimal(0))
    tokens = sum(int(step["counts"].get("llm_tokens", 0)) for step in steps)
    return cost_eur, tokens


def run_status(steps: Sequence[dict[str, Any]]) -> str:
    if any(step["status"] == FAILED for step in steps):
        return FAILED
    if any(step["counts"].get(name) for step in steps for name in PARTIAL_COUNTS):
        return PARTIAL
    return SUCCEEDED


def error_text(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"[:MAX_ERROR_LENGTH]


def run_details(ctx: RunContext, outcome: RunOutcome) -> dict[str, Any]:
    details: dict[str, Any] = {"data_dir": str(ctx.data_dir), "steps": outcome.steps}
    if outcome.error is not None:
        details["error"] = outcome.error
    return details


def execute(ctx: RunContext, steps: Sequence[tuple[str, StepFn]] = STEPS) -> RunOutcome:
    """Run the steps in order; the first failing step stops the run. The record is always kept."""
    outcome = RunOutcome(run_id=ctx.run_id)
    start_run(ctx.app, ctx.run_id)
    try:
        for name, step in steps:
            started = time.perf_counter()
            try:
                counts = step(ctx)
            except Exception as error:
                traceback.print_exc()
                outcome.error = {"step": name, "message": error_text(error)}
                outcome.steps.append(
                    {"name": name, "status": FAILED, "duration_s": elapsed(started), "counts": {}}
                )
                break
            outcome.steps.append(
                {
                    "name": name,
                    "status": SUCCEEDED,
                    "duration_s": elapsed(started),
                    "counts": counts,
                }
            )
        outcome.status = run_status(outcome.steps)
    finally:
        finish_run(
            ctx.app,
            ctx.run_id,
            outcome.status,
            run_details(ctx, outcome),
            run_cost(outcome.steps),
        )
    return outcome


def elapsed(started: float) -> float:
    return round(time.perf_counter() - started, 3)


def summary_counts(counts: Counts) -> str:
    return " ".join(f"{key}={value}" for key, value in counts.items() if not key.endswith("_list"))


def print_outcome(outcome: RunOutcome) -> None:
    for step in outcome.steps:
        print(
            f"{step['name']:<14} {step['status']:<9} {step['duration_s']:>8.3f}s  "
            f"{summary_counts(step['counts'])}"
        )
    if outcome.error is not None:
        print(f"failed at {outcome.error['step']}: {outcome.error['message']}")
    print(f"run {outcome.run_id}: {outcome.status}")


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the nightly Matchbook pipeline once.")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=None,
        help="Pacing date (YYYY-MM-DD); defaults to the last delivered date.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    ingest_settings = IngestSettings()
    etl, app = create_etl_engine(), create_app_engine()
    try:
        with httpx.Client(
            base_url=ingest_settings.base_url, timeout=REQUEST_TIMEOUT_SECONDS
        ) as client:
            ctx = RunContext(
                run_id=uuid.uuid4(),
                data_dir=args.data_dir,
                etl=etl,
                app=app,
                client=client,
                ingest_settings=ingest_settings,
                agency_timezone=settings.agency_timezone,
                as_of=args.as_of,
            )
            outcome = execute(ctx)
    finally:
        app.dispose()
        etl.dispose()
    print_outcome(outcome)
    return outcome.exit_code


if __name__ == "__main__":
    sys.exit(main())
