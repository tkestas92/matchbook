"""The nightly Airflow DAG, read statically with `ast`: Airflow is not installed here (decision 034).

Checks the task layout against `pipelines.run.STEPS`, the schedule and retry settings, and that the
DAG file stays thin: no SQL, no project imports, every project task a one-line call into
`orchestration.tasks`.
"""

import ast
import re
from pathlib import Path
from typing import Any

import pytest

from orchestration import tasks
from pipelines.run import FAILED, PARTIAL, STEPS

DAG_FILE = Path(__file__).resolve().parents[2] / "orchestration" / "dags" / "matchbook_nightly.py"
SOURCE = DAG_FILE.read_text()
TREE = ast.parse(SOURCE)

ALLOWED_IMPORT_ROOTS = {"airflow", "pendulum", "datetime", "os"}
SQL = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|TRUNCATE|JOIN)\b", re.I)
FORBIDDEN_NAMES = re.compile(r"sqlalchemy|psycopg|httpx|^app\b|\bpipelines\b|answer_key|\beval\b")
PROJECT_TASKS = ("start", "run_steps", "record")
# Airflow 3 context keys plus Airflow 2 legacy ones: a task parameter with one of these names
# makes every task fail at start ("The key 'run_id' in args is a part of kwargs and therefore
# reserved").
AIRFLOW_CONTEXT_KEYS = frozenset(
    {
        "conf",
        "conn",
        "dag",
        "dag_run",
        "data_interval_end",
        "data_interval_start",
        "ds",
        "ds_nodash",
        "exception",
        "execution_date",
        "expanded_ti_count",
        "inlet_events",
        "inlets",
        "logical_date",
        "macros",
        "map_index_template",
        "next_ds",
        "next_ds_nodash",
        "next_execution_date",
        "outlet_events",
        "outlets",
        "params",
        "prev_data_interval_end_success",
        "prev_data_interval_start_success",
        "prev_ds",
        "prev_ds_nodash",
        "prev_end_date_success",
        "prev_execution_date",
        "prev_execution_date_success",
        "prev_start_date_success",
        "reason",
        "run_id",
        "start_date",
        "task",
        "task_instance",
        "task_instance_key_str",
        "task_reschedule_count",
        "templates_dict",
        "test_mode",
        "ti",
        "tomorrow_ds",
        "tomorrow_ds_nodash",
        "triggering_asset_events",
        "triggering_dataset_events",
        "try_number",
        "ts",
        "ts_nodash",
        "ts_nodash_with_tz",
        "var",
        "yesterday_ds",
        "yesterday_ds_nodash",
    }
)


def constant(name: str) -> Any:
    for node in TREE.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} is not a module-level literal")


def functions() -> dict[str, ast.FunctionDef]:
    return {node.name: node for node in TREE.body if isinstance(node, ast.FunctionDef)}


def decorator_call(function: ast.FunctionDef) -> ast.Call:
    (decorator,) = function.decorator_list
    assert isinstance(decorator, ast.Call)
    return decorator


def keywords(call: ast.Call) -> dict[str, ast.expr]:
    return {kw.arg: kw.value for kw in call.keywords if kw.arg is not None}


def dag_call() -> ast.Call:
    (with_node,) = [node for node in TREE.body if isinstance(node, ast.With)]
    call = with_node.items[0].context_expr
    assert isinstance(call, ast.Call) and ast.unparse(call.func) == "DAG"
    return call


def dag_body() -> list[ast.stmt]:
    (with_node,) = [node for node in TREE.body if isinstance(node, ast.With)]
    return with_node.body


def test_dag_tasks_cover_the_runner_steps_in_order() -> None:
    step_tasks = constant("STEP_TASKS")
    flattened = [step for _, steps in step_tasks for step in steps]

    assert flattened == [name for name, _ in STEPS]


def test_only_normalize_and_quality_share_a_task() -> None:
    step_tasks = dict(constant("STEP_TASKS"))

    assert step_tasks["normalize_quality"] == ("normalize", "quality")
    for task_id, steps in step_tasks.items():
        if task_id != "normalize_quality":
            assert steps == (task_id,)


def test_task_ids() -> None:
    step_task_ids = [task_id for task_id, _ in constant("STEP_TASKS")]

    assert set(functions()) == {"start", "run_steps", "record", "outcome"}
    assert len(step_task_ids) == len(set(step_task_ids)) == 7
    assert not {"start", "record", "outcome"} & set(step_task_ids)


def test_step_tasks_are_chained_after_start_then_record_then_outcome() -> None:
    body = "\n".join(ast.unparse(stmt) for stmt in dag_body())

    assert "pipeline_run_id = start('{{ run_id }}')" in body
    assert "run_steps.override(task_id=task_id)(pipeline_run_id, list(names))" in body
    assert "chain(pipeline_run_id, *steps)" in body
    assert "status = record(pipeline_run_id, '{{ run_id }}')" in body
    assert "steps >> status" in body
    assert "outcome(status)" in body


def parameter_names(function: ast.FunctionDef) -> set[str]:
    arguments = function.args
    names = {arg.arg for arg in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]}
    names |= {arg.arg for arg in (arguments.vararg, arguments.kwarg) if arg is not None}
    return names


def test_task_parameters_are_not_airflow_context_keys() -> None:
    collisions = {
        name: sorted(parameter_names(function) & AIRFLOW_CONTEXT_KEYS)
        for name, function in functions().items()
    }

    assert {name: keys for name, keys in collisions.items() if keys} == {}


def test_record_always_runs() -> None:
    trigger_rule = keywords(decorator_call(functions()["record"]))["trigger_rule"]

    assert ast.literal_eval(trigger_rule) == "all_done"


def test_project_tasks_run_in_the_project_venv() -> None:
    assert constant("VENV_PYTHON") == "/opt/matchbook-venv/bin/python"
    for name in PROJECT_TASKS:
        call = decorator_call(functions()[name])
        assert ast.unparse(call.func) == "task.external_python"
        assert ast.unparse(keywords(call)["python"]) == "VENV_PYTHON"


@pytest.mark.parametrize("name", PROJECT_TASKS)
def test_project_tasks_only_call_one_glue_function(name: str) -> None:
    body = functions()[name].body

    assert len(body) == 2
    imported, returned = body
    assert ast.unparse(imported) == "from orchestration import tasks"
    assert isinstance(returned, ast.Return) and isinstance(returned.value, ast.Call)
    glue = ast.unparse(returned.value.func)
    assert glue.startswith("tasks.")
    assert callable(getattr(tasks, glue.removeprefix("tasks.")))


def test_outcome_maps_failed_and_partial_and_never_retries() -> None:
    assert constant("FAILED") == FAILED
    assert constant("PARTIAL") == PARTIAL
    outcome = functions()["outcome"]
    assert ast.literal_eval(keywords(decorator_call(outcome))["retries"]) == 0
    raised = [
        ast.unparse(node.exc.func) for node in ast.walk(outcome) if isinstance(node, ast.Raise)
    ]
    assert raised == ["AirflowFailException", "AirflowSkipException"]


def test_schedule_is_nightly_in_the_agency_timezone() -> None:
    kwargs = keywords(dag_call())

    assert ast.unparse(kwargs["schedule"]) == (
        "CronTriggerTimetable(NIGHTLY_CRON, timezone=AGENCY_TIMEZONE)"
    )
    assert constant("NIGHTLY_CRON") == "0 3 * * *"
    assert "os.environ.get('AGENCY_TIMEZONE', 'Europe/Vilnius')" in SOURCE.replace('"', "'")
    assert ast.literal_eval(kwargs["catchup"]) is False
    assert ast.literal_eval(kwargs["max_active_runs"]) == 1


def test_retries_are_small() -> None:
    default_args = keywords(dag_call())["default_args"]
    assert isinstance(default_args, ast.Dict)
    settings = {
        ast.literal_eval(k): ast.unparse(v)
        for k, v in zip(default_args.keys, default_args.values, strict=True)
        if k
    }

    assert settings["retries"] == "2"
    assert settings["retry_delay"] == "timedelta(minutes=5)"
    assert settings["execution_timeout"] == "timedelta(hours=1)"


def test_top_level_imports_are_airflow_and_stdlib_only() -> None:
    roots = set()
    for node in TREE.body:
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            roots.add((node.module or "").split(".")[0])

    assert roots <= ALLOWED_IMPORT_ROOTS


def docstrings() -> set[int]:
    owners = [TREE, *(node for node in ast.walk(TREE) if isinstance(node, ast.FunctionDef))]
    return {
        id(owner.body[0].value)
        for owner in owners
        if isinstance(owner.body[0], ast.Expr) and isinstance(owner.body[0].value, ast.Constant)
    }


def string_literals() -> list[str]:
    skip = docstrings()
    return [
        node.value
        for node in ast.walk(TREE)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip
    ]


def referenced_names() -> list[str]:
    names: list[str] = []
    for node in ast.walk(TREE):
        if isinstance(node, ast.Name | ast.Attribute):
            names.append(ast.unparse(node))
        elif isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names += [f"{node.module}.{alias.name}" for alias in node.names]
    return names


def test_dag_file_has_no_sql() -> None:
    assert [text for text in string_literals() if SQL.search(text)] == []


def test_dag_file_names_no_data_access_or_project_code() -> None:
    assert [name for name in referenced_names() if FORBIDDEN_NAMES.search(name)] == []
