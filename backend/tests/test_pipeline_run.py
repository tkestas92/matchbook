"""The pipeline runner: step order, one run_id, the run record, failures and idempotency.

The end-to-end tests generate data into a temporary directory and call the mock APIs in-process
with faults off. Needs the local Postgres (`make up`) and the role passwords.
"""

import shutil
import uuid
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from tests.conftest import PipelineDb

from generator.documents.billing import build_documents
from generator.documents.config import load_documents_config
from generator.errors import generate_with_errors, load_errors_config
from generator.world import build_world, load_config
from mock_platforms.app import create_app
from mock_platforms.config import MockPlatformsSettings
from pipelines import run as runner
from pipelines.ingest import IngestFailure, IngestResult, IngestSettings
from pipelines.load_invoices import GROUND_TRUTH, InvoiceSettings
from pipelines.run import (
    FAILED,
    PARTIAL,
    STEPS,
    SUCCEEDED,
    IngestUnavailable,
    RunContext,
    RunOutcome,
    check_ingest_reachable,
    execute,
    print_outcome,
    run_status,
    summary_counts,
)

TOKEN = "test-token"
CONFIRMED_ORDER = [
    "reference",
    "ingest",
    "normalize",
    "quality",
    "load_plans",
    "match_names",
    "load_invoices",
    "reconcile",
    "pacing",
]
PACING_COUNTS = {
    "as_of": "2026-10-31",
    "orders_paced": 114,
    "orders_skipped": 0,
    "statuses": {"green": 104, "yellow": 10},
    "issues_written": 6,
    "quality_issues_taken_over": 6,
}
PACING_SUMMARY = (
    "as_of=2026-10-31 orders_paced=114 orders_skipped=0 "
    "statuses={'green': 104, 'yellow': 10} issues_written=6 quality_issues_taken_over=6"
)
COUNTED_TABLES = (
    "raw.meta_responses",
    "raw.google_responses",
    "raw.tiktok_responses",
    "core.clients",
    "core.platforms",
    "core.ad_accounts",
    "core.campaigns",
    "core.campaign_aliases",
    "core.daily_delivery",
    "core.insertion_orders",
    "core.invoices",
    "core.invoice_lines",
    "core.credit_notes",
    "core.reconciliation_results",
    "app.issues",
)


def step(name: str, status: str = SUCCEEDED, **counts: Any) -> dict[str, Any]:
    return {"name": name, "status": status, "duration_s": 0.0, "counts": counts}


# --- Order and status rules (no database) ---


def test_steps_run_in_the_confirmed_order() -> None:
    names = [name for name, _ in STEPS]
    assert names == CONFIRMED_ORDER
    assert names.index("quality") < names.index("reconcile") < names.index("pacing")
    assert names.index("normalize") < names.index("load_plans")
    assert names.index("load_plans") < names.index("match_names") < names.index("reconcile")


def test_status_is_succeeded_when_every_step_is_clean() -> None:
    steps = [step("ingest", failures=0), step("load_plans", rejected_documents=0)]
    assert run_status(steps) == SUCCEEDED
    assert RunOutcome(uuid.uuid4(), status=SUCCEEDED).exit_code == 0


@pytest.mark.parametrize(
    "steps",
    [
        [step("ingest", failures=1)],
        [step("load_plans", rejected_documents=2)],
        [step("load_invoices", rejected_documents=1)],
    ],
)
def test_status_is_partial_when_some_input_was_not_loaded(steps: list[dict[str, Any]]) -> None:
    assert run_status(steps) == PARTIAL
    assert RunOutcome(uuid.uuid4(), status=PARTIAL).exit_code == 2


def test_rejected_rows_alone_do_not_make_a_run_partial() -> None:
    assert run_status([step("normalize", rejected_rows=3, unknown_accounts=1)]) == SUCCEEDED


def test_a_failed_step_fails_the_run() -> None:
    steps = [step("ingest", failures=1), step("normalize", status=FAILED)]
    assert run_status(steps) == FAILED
    assert RunOutcome(uuid.uuid4(), status=FAILED).exit_code == 1


def test_ingest_is_unavailable_only_when_every_platform_failed_to_list() -> None:
    down = [IngestFailure(name, None, "connection refused") for name in ("meta", "google")]
    result = IngestResult(run_id=uuid.uuid4(), failures=down)
    check_ingest_reachable(result)  # TikTok still answered.

    result.failures.append(IngestFailure("tiktok", None, "connection refused"))
    with pytest.raises(IngestUnavailable, match="make mock-apis"):
        check_ingest_reachable(result)


def test_account_failures_do_not_make_ingest_unavailable() -> None:
    failures = [IngestFailure(name, "acc-1", "HTTP 500") for name in ("meta", "google", "tiktok")]
    check_ingest_reachable(IngestResult(run_id=uuid.uuid4(), failures=failures))


def test_quality_needs_the_normalize_result_of_the_same_run() -> None:
    ctx = RunContext(
        run_id=uuid.uuid4(),
        data_dir=Path("unused"),
        etl=None,  # type: ignore[arg-type]
        app=None,  # type: ignore[arg-type]
        client=None,  # type: ignore[arg-type]
        ingest_settings=None,  # type: ignore[arg-type]
        agency_timezone="Europe/Vilnius",
    )
    with pytest.raises(RuntimeError, match="normalize result"):
        runner.step_quality(ctx)


def test_pacing_summary_separates_every_count() -> None:
    assert summary_counts(PACING_COUNTS) == PACING_SUMMARY


def test_print_outcome_prints_the_pacing_line(capsys: pytest.CaptureFixture[str]) -> None:
    pacing = {"name": "pacing", "status": SUCCEEDED, "duration_s": 0.5, "counts": PACING_COUNTS}
    print_outcome(RunOutcome(uuid.uuid4(), status=SUCCEEDED, steps=[pacing]))
    first_line = capsys.readouterr().out.splitlines()[0]
    assert first_line == f"pacing         succeeded    0.500s  {PACING_SUMMARY}"


# --- End to end (Postgres) ---


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out_dir = tmp_path_factory.mktemp("generated") / "data"
    world = build_world(load_config())
    docs_config = load_documents_config()
    generate_with_errors(
        world, build_documents(world, docs_config), docs_config, load_errors_config(), out_dir
    )
    return out_dir


@pytest.fixture(scope="module")
def mock_client(generated: Path) -> Iterator[httpx.Client]:
    app = create_app(
        MockPlatformsSettings(
            _env_file=None,
            token=TOKEN,
            delivery_dir=generated / "delivery",
            faults_enabled=False,
        )
    )
    with TestClient(app) as client:
        yield client


def context(db: PipelineDb, client: httpx.Client, data_dir: Path) -> RunContext:
    settings = IngestSettings(
        _env_file=None,
        base_url="http://testserver",
        MOCK_PLATFORMS_TOKEN=TOKEN,
        period_start=date(2026, 5, 1),
        period_end=date(2026, 11, 30),
        page_size=500,
    )
    return RunContext(
        run_id=uuid.uuid4(),
        data_dir=data_dir,
        etl=db.etl,
        app=db.app,
        client=client,
        ingest_settings=settings,
        agency_timezone="Europe/Vilnius",
        invoice_settings=InvoiceSettings(_env_file=None, source=GROUND_TRUTH),
    )


def table_counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {
            t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in COUNTED_TABLES
        }


def run_rows(engine: Engine) -> list[Any]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT id, status, started_at, finished_at, details "
                    "FROM app.pipeline_runs ORDER BY started_at"
                )
            )
        )


@pytest.fixture(scope="module")
def two_runs(
    module_pipeline_db: PipelineDb, mock_client: httpx.Client, generated: Path
) -> tuple[list[RunOutcome], list[dict[str, int]], list[int]]:
    db = module_pipeline_db
    outcomes, counts, runs = [], [], []
    for _ in range(2):
        outcomes.append(execute(context(db, mock_client, generated)))
        counts.append(table_counts(db.owner))
        runs.append(len(run_rows(db.owner)))
    return outcomes, counts, runs


def test_both_runs_succeed(two_runs: tuple[list[RunOutcome], Any, Any]) -> None:
    outcomes, _, _ = two_runs
    assert [o.status for o in outcomes] == [SUCCEEDED, SUCCEEDED]
    assert [[s["name"] for s in o.steps] for o in outcomes] == [CONFIRMED_ORDER] * 2


def test_running_twice_gives_identical_row_counts(
    two_runs: tuple[Any, list[dict[str, int]], Any],
) -> None:
    _, counts, _ = two_runs
    first, second = counts
    assert all(first[table] > 0 for table in COUNTED_TABLES), first
    assert second == first


def test_each_run_adds_one_record(two_runs: tuple[Any, Any, list[int]]) -> None:
    _, _, runs = two_runs
    assert runs == [1, 2]


def test_the_record_keeps_status_duration_and_step_counts(
    module_pipeline_db: PipelineDb, two_runs: tuple[list[RunOutcome], Any, Any]
) -> None:
    outcomes, _, _ = two_runs
    rows = {row.id: row for row in run_rows(module_pipeline_db.owner)}
    row = rows[outcomes[1].run_id]
    assert row.status == SUCCEEDED
    assert row.finished_at >= row.started_at
    steps = {s["name"]: s for s in row.details["steps"]}
    assert list(steps) == CONFIRMED_ORDER
    assert steps["ingest"]["counts"]["failures"] == 0
    assert "retries" in steps["ingest"]["counts"]
    assert steps["normalize"]["counts"]["rejected_rows"] == 0
    assert "gap_notes_unattached" in steps["reconcile"]["counts"]
    assert "quality_issues_taken_over" in steps["pacing"]["counts"]
    assert steps["pacing"]["counts"]["as_of"] == "2026-10-31"


def test_every_step_writes_the_run_id(
    module_pipeline_db: PipelineDb, two_runs: tuple[list[RunOutcome], Any, Any]
) -> None:
    outcomes, _, _ = two_runs
    run_id = outcomes[1].run_id
    with module_pipeline_db.owner.connect() as conn:
        for table in ("core.daily_delivery", "core.insertion_orders", "core.invoices"):
            other = conn.execute(
                text(f"SELECT count(*) FROM {table} WHERE run_id <> :run_id"), {"run_id": run_id}
            ).scalar_one()
            assert other == 0, table


def test_a_failing_step_stops_the_run_and_is_recorded(
    module_pipeline_db: PipelineDb,
    mock_client: httpx.Client,
    generated: Path,
    two_runs: Any,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    shutil.copytree(generated, data_dir, ignore=shutil.ignore_patterns("invoices"))
    outcome = execute(context(module_pipeline_db, mock_client, data_dir))

    assert outcome.status == FAILED
    assert outcome.exit_code == 1
    stop = CONFIRMED_ORDER.index("load_invoices")
    assert [s["name"] for s in outcome.steps] == CONFIRMED_ORDER[: stop + 1]
    assert outcome.steps[-1]["status"] == FAILED
    row = next(r for r in run_rows(module_pipeline_db.owner) if r.id == outcome.run_id)
    assert row.status == FAILED
    assert row.finished_at is not None
    assert row.details["error"]["step"] == "load_invoices"
    assert "invoices" in row.details["error"]["message"]
    assert "reconcile" not in [s["name"] for s in row.details["steps"]]


def test_etl_cannot_write_the_run_record(module_pipeline_db: PipelineDb) -> None:
    with pytest.raises(DBAPIError, match="permission denied"):
        with module_pipeline_db.etl.begin() as conn:
            conn.execute(text("INSERT INTO app.pipeline_runs (status) VALUES ('running')"))
