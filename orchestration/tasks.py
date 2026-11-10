"""What the Airflow tasks call, run in the project venv: start, run steps, record (decision 034).

Each Airflow task is a separate process, so the in-memory `RunContext` of `pipelines.run.execute`
cannot be shared. Every task builds its own context for the same `run_id`, and every step leaves
its record as a JSON file in the run-state directory, which `finish` reads to write the run row.
The step functions, their order and the status rules are those of `pipelines/run.py`.
"""

import json
import os
import time
import traceback
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
from app.config import get_settings
from app.db.session import create_app_engine, create_etl_engine
from sqlalchemy import Engine

from pipelines import run
from pipelines.ingest import REQUEST_TIMEOUT_SECONDS, IngestSettings
from pipelines.load_plans import DEFAULT_DATA_DIR

DATA_DIR_ENV = "MATCHBOOK_DATA_DIR"
RUN_STATE_DIR_ENV = "MATCHBOOK_RUN_STATE_DIR"
DEFAULT_RUN_STATE_DIR = Path("/opt/airflow/run-state")
ORCHESTRATOR = "airflow"
NOT_REPORTED = "step did not report (killed, timed out or not run)"

StepRecord = dict[str, Any]


def data_dir() -> Path:
    return Path(os.environ.get(DATA_DIR_ENV, str(DEFAULT_DATA_DIR)))


def run_state_dir() -> Path:
    return Path(os.environ.get(RUN_STATE_DIR_ENV, str(DEFAULT_RUN_STATE_DIR)))


def step_functions() -> dict[str, run.StepFn]:
    return dict(run.STEPS)


def record_path(state_dir: Path, run_id: str, name: str) -> Path:
    return state_dir / run_id / f"{name}.json"


def write_record(state_dir: Path, run_id: str, record: StepRecord) -> None:
    path = record_path(state_dir, run_id, record["name"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, default=str))


def read_record(state_dir: Path, run_id: str, name: str) -> StepRecord | None:
    path = record_path(state_dir, run_id, name)
    return json.loads(path.read_text()) if path.exists() else None


def run_named_steps(ctx: run.RunContext, names: Sequence[str], state_dir: Path) -> list[StepRecord]:
    """Run the steps on one context, in order; a failing step is recorded, then re-raised."""
    functions = step_functions()
    run_id = str(ctx.run_id)
    records: list[StepRecord] = []
    for name in names:
        started = time.perf_counter()
        try:
            counts = functions[name](ctx)
        except Exception as error:
            traceback.print_exc()
            write_record(
                state_dir,
                run_id,
                {
                    "name": name,
                    "status": run.FAILED,
                    "duration_s": run.elapsed(started),
                    "counts": {},
                    "error": {"step": name, "message": run.error_text(error)},
                },
            )
            raise
        record = {
            "name": name,
            "status": run.SUCCEEDED,
            "duration_s": run.elapsed(started),
            "counts": counts,
        }
        write_record(state_dir, run_id, record)
        records.append(record)
    return records


def collect_steps(state_dir: Path, run_id: str) -> tuple[list[StepRecord], dict[str, str] | None]:
    """Step records in `STEPS` order up to the first failed or missing step, and its error."""
    steps: list[StepRecord] = []
    for name, _ in run.STEPS:
        record = read_record(state_dir, run_id, name)
        if record is None:
            steps.append({"name": name, "status": run.FAILED, "duration_s": 0.0, "counts": {}})
            return steps, {"step": name, "message": NOT_REPORTED}
        error = record.pop("error", None)
        steps.append(record)
        if record["status"] == run.FAILED:
            return steps, error
    return steps, None


def finish_run_record(
    app: Engine, state_dir: Path, run_id: str, dag_run_id: str, data_dir_used: Path
) -> str:
    steps, error = collect_steps(state_dir, run_id)
    status = run.run_status(steps)
    details: dict[str, Any] = {
        "data_dir": str(data_dir_used),
        "steps": steps,
        "orchestrator": ORCHESTRATOR,
        "airflow_run_id": dag_run_id,
    }
    if error is not None:
        details["error"] = error
    run.finish_run(app, uuid.UUID(run_id), status, details)
    return status


@contextmanager
def app_engine() -> Iterator[Engine]:
    engine = create_app_engine()
    try:
        yield engine
    finally:
        engine.dispose()


@contextmanager
def run_context(run_id: str) -> Iterator[run.RunContext]:
    """The same context `pipelines.run.main` builds, for one task of the run `run_id`."""
    ingest_settings = IngestSettings()
    etl, app = create_etl_engine(), create_app_engine()
    try:
        with httpx.Client(
            base_url=ingest_settings.base_url, timeout=REQUEST_TIMEOUT_SECONDS
        ) as client:
            yield run.RunContext(
                run_id=uuid.UUID(run_id),
                data_dir=data_dir(),
                etl=etl,
                app=app,
                client=client,
                ingest_settings=ingest_settings,
                agency_timezone=get_settings().agency_timezone,
            )
    finally:
        app.dispose()
        etl.dispose()


def start(dag_run_id: str) -> str:
    """Create the run's id and its `running` row; every later task gets the id from Airflow."""
    run_id = uuid.uuid4()
    with app_engine() as app:
        run.start_run(app, run_id)
    print(f"run {run_id} started for Airflow run {dag_run_id}")
    return str(run_id)


def run_steps(run_id: str, names: Sequence[str]) -> list[StepRecord]:
    with run_context(run_id) as ctx:
        return run_named_steps(ctx, names, run_state_dir())


def finish(run_id: str, dag_run_id: str) -> str:
    with app_engine() as app:
        status = finish_run_record(app, run_state_dir(), run_id, dag_run_id, data_dir())
    print(f"run {run_id}: {status}")
    return status
