"""The Airflow task glue: step records on disk, the run record, and what a missing step means.

Fake steps need no database; the last tests need the local Postgres (`make up`) and role passwords.
"""

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text
from tests.conftest import PipelineDb

from orchestration import tasks
from pipelines.run import FAILED, PARTIAL, STEPS, SUCCEEDED, start_run

STEP_NAMES = [name for name, _ in STEPS]


def fake_ctx() -> Any:
    return SimpleNamespace(run_id=uuid.uuid4(), normalized=None)


def write_records(state_dir: Path, run_id: str, names: list[str], **counts: Any) -> None:
    for name in names:
        tasks.write_record(
            state_dir,
            run_id,
            {"name": name, "status": SUCCEEDED, "duration_s": 0.1, "counts": dict(counts)},
        )


@pytest.fixture
def fake_steps(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def normalize(ctx: Any) -> dict[str, Any]:
        calls.append("normalize")
        ctx.normalized = "normalized"
        return {"records": 3}

    def quality(ctx: Any) -> dict[str, Any]:
        calls.append("quality")
        if ctx.normalized != "normalized":
            raise RuntimeError("Quality needs the normalize result of the same run")
        return {"findings_written": 1}

    def broken(ctx: Any) -> dict[str, Any]:
        calls.append("broken")
        raise ValueError("bad invoice")

    monkeypatch.setattr(
        tasks,
        "step_functions",
        lambda: {"normalize": normalize, "quality": quality, "broken": broken},
    )
    return calls


def test_steps_of_one_task_share_one_context(fake_steps: list[str], tmp_path: Path) -> None:
    ctx = fake_ctx()
    records = tasks.run_named_steps(ctx, ["normalize", "quality"], tmp_path)

    assert fake_steps == ["normalize", "quality"]
    assert [r["status"] for r in records] == [SUCCEEDED, SUCCEEDED]
    assert records[1]["counts"] == {"findings_written": 1}


def test_each_step_leaves_a_record_file(fake_steps: list[str], tmp_path: Path) -> None:
    ctx = fake_ctx()
    tasks.run_named_steps(ctx, ["normalize", "quality"], tmp_path)

    for name in ("normalize", "quality"):
        record = json.loads(tasks.record_path(tmp_path, str(ctx.run_id), name).read_text())
        assert record["name"] == name
        assert record["status"] == SUCCEEDED
        assert record["duration_s"] >= 0


def test_a_failing_step_is_recorded_and_re_raised(fake_steps: list[str], tmp_path: Path) -> None:
    ctx = fake_ctx()
    with pytest.raises(ValueError, match="bad invoice"):
        tasks.run_named_steps(ctx, ["broken", "normalize"], tmp_path)

    assert fake_steps == ["broken"]
    record = tasks.read_record(tmp_path, str(ctx.run_id), "broken")
    assert record is not None
    assert record["status"] == FAILED
    assert record["counts"] == {}
    assert record["error"] == {"step": "broken", "message": "ValueError: bad invoice"}


def test_a_retry_overwrites_the_failed_record(fake_steps: list[str], tmp_path: Path) -> None:
    run_id = str(uuid.uuid4())
    tasks.write_record(
        tmp_path, run_id, {"name": "normalize", "status": FAILED, "duration_s": 0, "counts": {}}
    )
    tasks.run_named_steps(SimpleNamespace(run_id=uuid.UUID(run_id)), ["normalize"], tmp_path)

    record = tasks.read_record(tmp_path, run_id, "normalize")
    assert record is not None
    assert record["status"] == SUCCEEDED


def test_collect_steps_returns_every_step_of_a_clean_run(tmp_path: Path) -> None:
    run_id = str(uuid.uuid4())
    write_records(tmp_path, run_id, STEP_NAMES)

    steps, error = tasks.collect_steps(tmp_path, run_id)

    assert [s["name"] for s in steps] == STEP_NAMES
    assert error is None


def test_collect_steps_stops_at_the_failed_step_and_keeps_its_error(tmp_path: Path) -> None:
    run_id = str(uuid.uuid4())
    write_records(tmp_path, run_id, STEP_NAMES[:5])
    error = {"step": "load_invoices", "message": "FileNotFoundError: invoices"}
    tasks.write_record(
        tmp_path,
        run_id,
        {"name": "load_invoices", "status": FAILED, "duration_s": 0, "counts": {}, "error": error},
    )

    steps, found = tasks.collect_steps(tmp_path, run_id)

    assert [s["name"] for s in steps] == STEP_NAMES[:6]
    assert "error" not in steps[-1]
    assert found == error


def test_a_step_without_a_record_counts_as_failed(tmp_path: Path) -> None:
    run_id = str(uuid.uuid4())
    write_records(tmp_path, run_id, STEP_NAMES[:2])

    steps, error = tasks.collect_steps(tmp_path, run_id)

    assert [s["name"] for s in steps] == STEP_NAMES[:3]
    assert steps[-1]["status"] == FAILED
    assert error == {"step": STEP_NAMES[2], "message": tasks.NOT_REPORTED}


def test_data_and_state_dirs_come_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(tasks.DATA_DIR_ENV, str(tmp_path / "data"))
    monkeypatch.setenv(tasks.RUN_STATE_DIR_ENV, str(tmp_path / "state"))

    assert tasks.data_dir() == tmp_path / "data"
    assert tasks.run_state_dir() == tmp_path / "state"


@pytest.fixture
def airflow_env(
    monkeypatch: pytest.MonkeyPatch, pipeline_db: PipelineDb, tmp_path: Path
) -> PipelineDb:
    monkeypatch.setattr(tasks, "create_app_engine", lambda: pipeline_db.app)
    monkeypatch.setenv(tasks.RUN_STATE_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(tasks.DATA_DIR_ENV, "/opt/matchbook/data/generated")
    return pipeline_db


def run_row(db: PipelineDb, run_id: str) -> Any:
    with db.owner.connect() as conn:
        return conn.execute(
            text("SELECT status, finished_at, details FROM app.pipeline_runs WHERE id = :id"),
            {"id": run_id},
        ).one()


def test_start_writes_a_running_record(airflow_env: PipelineDb) -> None:
    run_id = tasks.start("scheduled__2026-11-10T01:00:00+00:00")

    row = run_row(airflow_env, run_id)
    assert row.status == "running"
    assert row.finished_at is None


def test_finish_writes_status_steps_and_airflow_run_id(
    airflow_env: PipelineDb, tmp_path: Path
) -> None:
    run_id = tasks.start("manual__1")
    write_records(tmp_path, run_id, STEP_NAMES)

    assert tasks.finish(run_id, "manual__1") == SUCCEEDED

    row = run_row(airflow_env, run_id)
    assert row.status == SUCCEEDED
    assert row.finished_at is not None
    assert [s["name"] for s in row.details["steps"]] == STEP_NAMES
    assert row.details["orchestrator"] == "airflow"
    assert row.details["airflow_run_id"] == "manual__1"
    assert row.details["data_dir"] == "/opt/matchbook/data/generated"
    assert "error" not in row.details


def test_finish_records_a_partial_run(airflow_env: PipelineDb, tmp_path: Path) -> None:
    run_id = tasks.start("manual__2")
    write_records(tmp_path, run_id, STEP_NAMES, failures=1)

    assert tasks.finish(run_id, "manual__2") == PARTIAL
    assert run_row(airflow_env, run_id).status == PARTIAL


def test_finish_records_a_run_whose_step_never_reported(
    airflow_env: PipelineDb, tmp_path: Path
) -> None:
    run_id = tasks.start("manual__3")
    write_records(tmp_path, run_id, STEP_NAMES[:1])

    assert tasks.finish(run_id, "manual__3") == FAILED

    row = run_row(airflow_env, run_id)
    assert row.status == FAILED
    assert row.details["error"] == {"step": "ingest", "message": tasks.NOT_REPORTED}


def test_finish_can_run_twice_for_the_same_run(airflow_env: PipelineDb, tmp_path: Path) -> None:
    run_id = str(uuid.uuid4())
    start_run(airflow_env.app, uuid.UUID(run_id))
    write_records(tmp_path, run_id, STEP_NAMES)

    assert tasks.finish(run_id, "manual__4") == SUCCEEDED
    assert tasks.finish(run_id, "manual__4") == SUCCEEDED
    assert run_row(airflow_env, run_id).status == SUCCEEDED
