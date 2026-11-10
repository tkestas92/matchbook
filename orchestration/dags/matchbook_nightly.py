"""Nightly Matchbook pipeline: one Airflow task per step of `pipelines/run.py` (decision 034).

Thin by design: every task calls a function in `orchestration.tasks` inside the project venv,
because Airflow's own environment does not have (and must not get) the project's dependencies.
normalize and quality share one task: quality needs normalize's in-memory result.
"""

import os
from datetime import timedelta

import pendulum
from airflow.sdk import DAG, chain, task
from airflow.sdk.exceptions import AirflowFailException, AirflowSkipException
from airflow.timetables.trigger import CronTriggerTimetable

VENV_PYTHON = "/opt/matchbook-venv/bin/python"
AGENCY_TIMEZONE = os.environ.get("AGENCY_TIMEZONE", "Europe/Vilnius")
NIGHTLY_CRON = "0 3 * * *"
FAILED = "failed"
PARTIAL = "partial"

# (task id, pipeline steps it runs), in the order of `pipelines.run.STEPS`.
STEP_TASKS = (
    ("reference", ("reference",)),
    ("ingest", ("ingest",)),
    ("normalize_quality", ("normalize", "quality")),
    ("load_plans", ("load_plans",)),
    ("load_invoices", ("load_invoices",)),
    ("reconcile", ("reconcile",)),
    ("pacing", ("pacing",)),
)


@task.external_python(python=VENV_PYTHON)
def start(dag_run_id: str) -> str:
    from orchestration import tasks

    return tasks.start(dag_run_id)


@task.external_python(python=VENV_PYTHON)
def run_steps(pipeline_run_id: str, names: list[str]) -> list[dict]:
    from orchestration import tasks

    return tasks.run_steps(pipeline_run_id, names)


@task.external_python(python=VENV_PYTHON, trigger_rule="all_done")
def record(pipeline_run_id: str, dag_run_id: str) -> str:
    from orchestration import tasks

    return tasks.finish(pipeline_run_id, dag_run_id)


@task(retries=0)
def outcome(status: str) -> None:
    if status == FAILED:
        raise AirflowFailException("Pipeline run failed; see app.pipeline_runs.details")
    if status == PARTIAL:
        raise AirflowSkipException("Pipeline run is partial; see app.pipeline_runs.details")


with DAG(
    dag_id="matchbook_nightly",
    schedule=CronTriggerTimetable(NIGHTLY_CRON, timezone=AGENCY_TIMEZONE),
    start_date=pendulum.datetime(2026, 11, 1, tz=AGENCY_TIMEZONE),
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(hours=3),
    default_args={
        "retries": 2,
        "retry_delay": timedelta(minutes=5),
        "execution_timeout": timedelta(hours=1),
    },
    tags=["matchbook", "stage-1"],
):
    pipeline_run_id = start("{{ run_id }}")
    steps = [
        run_steps.override(task_id=task_id)(pipeline_run_id, list(names))
        for task_id, names in STEP_TASKS
    ]
    chain(pipeline_run_id, *steps)
    status = record(pipeline_run_id, "{{ run_id }}")
    steps >> status
    outcome(status)
