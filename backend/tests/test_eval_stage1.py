"""Stage 1 eval: matching rules, the answer-key loader and the eval run as `eval_runner`.

Every answer key here is built by the test (same JSON shape as the generator's) and loaded into a
throwaway database; the real data/generated/answer_key.json is never read (decision 033).
"""

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from app.config import Settings, get_settings
from app.db.roles import MissingRolePasswordError, ensure_roles, role_passwords
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError
from tests.test_migrations import alembic_config, insert, temporary_database

from evals import load_answer_key as loader
from evals.answer_key import (
    AnswerKeyError,
    PlantedError,
    parse_answer_key,
    read_answer_key,
)
from evals.stage1 import (
    EXIT_FAIL,
    EXIT_PASS,
    EXIT_SETUP,
    EvalIssue,
    EvalResult,
    eval_runner_database_url,
    evaluate,
    match,
    passed,
    summarize,
)

JUNE = date(2026, 6, 1)
JULY = date(2026, 7, 1)


def planted(
    error_no: int,
    entity_ref: str,
    *,
    error_type: str = "",
    client: str = "ACME",
    platform: str | None = "meta",
    month: date = JUNE,
    stage: int = 1,
    magnitude: str = "100.00",
    campaign: str | None = None,
) -> PlantedError:
    details = {"platform_campaign_id": campaign} if campaign else {}
    return PlantedError(
        error_no=error_no,
        error_type=error_type or f"error_{error_no}",
        entity_type="campaign",
        entity_ref=entity_ref,
        client_code=client,
        platform=platform,
        period_start=month,
        period_end=month.replace(day=30),
        magnitude=Decimal(magnitude),
        expected_detector="reconciliation",
        detectable_in_stage=stage,
        details=details,
    )


def issue(
    issue_id: int,
    issue_type: str,
    *,
    client: str | None = "ACME",
    platform: str | None = "meta",
    month: date | None = JUNE,
    entity_id: str | None = None,
    order_ref: str | None = None,
) -> EvalIssue:
    return EvalIssue(
        id=issue_id,
        type=issue_type,
        client_code=client,
        platform=platform,
        period=month,
        entity_id=entity_id,
        order_ref=order_ref,
    )


def overall(result: EvalResult) -> Any:
    return summarize(result)[-1]


# --- matching -----------------------------------------------------------------------------


def test_found_error() -> None:
    result = match([planted(1, "INV-1")], [issue(1, "reconciliation_overcharge")])
    assert len(result.found) == 1
    assert not result.missed and not result.false_alarms
    assert passed(result, 0)


def test_missed_error_fails() -> None:
    other_month = issue(1, "reconciliation_overcharge", month=JULY)
    result = match([planted(1, "INV-1")], [other_month])
    assert [p.entity_ref for p in result.missed] == ["INV-1"]
    assert len(result.false_alarms) == 1
    assert not passed(result, 5)


def test_false_alarm_is_attributed_to_its_error_type_or_other() -> None:
    result = match(
        [],
        [
            issue(1, "reconciliation_overcharge"),
            issue(2, "reconciliation_budget_underspend"),
        ],
    )
    assert [(f.issue.id, f.error_no, f.reason) for f in result.false_alarms] == [
        (1, 1, "no planted error"),
        (2, None, "no planted error"),
    ]
    rows = {row.label: row for row in summarize(result)}
    assert rows["#1 invoice_above_delivery"].false_alarms == 1
    assert rows["other issue types"].false_alarms == 1
    assert rows["**overall**"].false_alarms == 2
    assert not passed(result, 1)
    assert passed(result, 2)


def test_one_issue_counts_for_at_most_one_error() -> None:
    first = planted(9, "ACME-META-2606", campaign="c1")
    second = planted(9, "ACME-META-2606b", campaign="c1")
    result = match([first, second], [issue(1, "quality_missing_days", entity_id="c1")])
    assert [p.entity_ref for p, _ in result.found] == ["ACME-META-2606"]
    assert [p.entity_ref for p in result.missed] == ["ACME-META-2606b"]
    assert not result.false_alarms


def test_second_issue_for_one_error_is_a_duplicate_false_alarm() -> None:
    result = match(
        [planted(1, "INV-1")],
        [issue(1, "reconciliation_overcharge"), issue(2, "reconciliation_overcharge")],
    )
    assert [i.id for _, i in result.found] == [1]
    assert [(f.issue.id, f.reason) for f in result.false_alarms] == [(2, "duplicate detection")]


def test_expected_missed_errors_do_not_fail() -> None:
    renamed = planted(3, "ACME-META-2606", stage=2)
    spike = planted(8, "ACME-META-2607", month=JULY, stage=4)
    result = match([renamed, spike], [])
    assert len(result.expected_missed) == 2
    assert not result.missed
    assert passed(result, 0)
    assert overall(result).expected_missed == 2


def test_expected_missed_error_found_anyway_counts_as_found() -> None:
    accepted = {3: frozenset({"reconciliation_needs_matching"})}
    result = match(
        [planted(3, "ACME-META-2606", stage=2)],
        [issue(1, "reconciliation_needs_matching")],
        accepted,
    )
    assert len(result.found) == 1
    assert not result.expected_missed


@pytest.mark.parametrize(
    "issue_type", ["reconciliation_timezone_shift", "reconciliation_month_boundary_shift"]
)
def test_both_shift_types_count_for_error_4(issue_type: str) -> None:
    shift = planted(4, "ACME-META-2606", campaign="c7")
    result = match([shift], [issue(1, issue_type, entity_id="c7")])
    assert len(result.found) == 1


def test_campaign_errors_match_on_the_campaign() -> None:
    shift = planted(4, "ACME-META-2606", campaign="c7")
    result = match([shift], [issue(1, "reconciliation_timezone_shift", entity_id="c8")])
    assert len(result.missed) == 1


def test_campaign_rename_matches_on_the_campaign() -> None:
    renamed = planted(3, "ACME-META-2606", stage=2, campaign="c7")
    found = match([renamed], [issue(1, "reconciliation_campaign_renamed", entity_id="c7")])
    other = match([renamed], [issue(1, "reconciliation_campaign_renamed", entity_id="c8")])
    assert len(found.found) == 1 and not found.false_alarms
    assert len(other.expected_missed) == 1 and len(other.false_alarms) == 1


def test_wrong_markup_needs_a_client_level_issue() -> None:
    markup = planted(5, "AG-ACME-2606", platform=None)
    per_platform = match([markup], [issue(1, "reconciliation_wrong_markup")])
    client_level = match([markup], [issue(1, "reconciliation_wrong_markup", platform=None)])
    assert len(per_platform.missed) == 1
    assert len(client_level.found) == 1


def test_duplicate_matches_the_copy_number_only() -> None:
    duplicate = planted(2, "INV-COPY")
    copy = match([duplicate], [issue(1, "reconciliation_duplicate_invoice", entity_id="INV-COPY")])
    original = match([duplicate], [issue(1, "reconciliation_duplicate_invoice", entity_id="INV-1")])
    assert len(copy.found) == 1
    assert len(original.missed) == 1
    assert len(original.false_alarms) == 1


def test_pacing_overpacing_matches_the_campaign_ref() -> None:
    drift = planted(7, "ACME-META-2606")
    own = {"entity_id": "ACME-META-2606", "order_ref": "ACME-META-2606"}
    found = match([drift], [issue(1, "pacing_overpacing", **own)])
    underpacing = match([drift], [issue(1, "pacing_underpacing", **own)])
    assert len(found.found) == 1
    assert len(underpacing.missed) == 1
    assert underpacing.false_alarms[0].error_no is None


def test_taken_over_pacing_issue_matches_the_order_code() -> None:
    """The issue keeps the zero-spend issue's platform campaign id; the key has the order code."""
    drift = planted(7, "GIN-META-2609", client="GIN", campaign="120001")
    taken_over = issue(
        1, "pacing_overpacing", client="GIN", entity_id="120001", order_ref="GIN-META-2609"
    )
    result = match([drift], [taken_over])
    assert [i.id for _, i in result.found] == [1]
    assert not result.missed and not result.false_alarms
    assert passed(result, 0)


@pytest.mark.parametrize("order_ref", ["ACME-META-2606b", None])
def test_pacing_issue_of_another_or_no_order_does_not_match(order_ref: str | None) -> None:
    drift = planted(7, "ACME-META-2606", campaign="120001")
    result = match(
        [drift], [issue(1, "pacing_overpacing", entity_id="120001", order_ref=order_ref)]
    )
    assert len(result.missed) == 1
    assert len(result.false_alarms) == 1


@pytest.mark.parametrize(
    ("error_no", "issue_type", "entity_ref", "order_ref"),
    [
        (4, "quality_month_boundary_shift", "c7", None),
        (7, "quality_zero_spend", "ACME-META-2606", "ACME-META-2606"),
    ],
)
def test_precursor_issue_is_missed_but_not_a_false_alarm(
    error_no: int, issue_type: str, entity_ref: str, order_ref: str | None
) -> None:
    error = planted(error_no, entity_ref, campaign="c7")
    result = match([error], [issue(1, issue_type, entity_id="c7", order_ref=order_ref)])
    assert len(result.missed) == 1
    assert [m.issue.id for m in result.misclassified] == [1]
    assert not result.false_alarms
    assert not passed(result, 0)


def test_precursor_issue_without_a_planted_error_is_a_false_alarm() -> None:
    result = match([], [issue(1, "quality_zero_spend", entity_id="c7")])
    assert len(result.false_alarms) == 1
    assert not result.misclassified


def test_summary_counts_and_euros() -> None:
    result = match(
        [
            planted(1, "INV-1", magnitude="10.50"),
            planted(5, "AG-1", platform=None, magnitude="-2.25"),
            planted(8, "X", stage=4, magnitude="1.00"),
        ],
        [issue(1, "reconciliation_overcharge")],
    )
    total = overall(result)
    assert (total.planted, total.found, total.missed, total.expected_missed) == (3, 1, 1, 1)
    assert total.eur_planted == Decimal("13.75")
    assert total.eur_found == Decimal("10.50")
    assert isinstance(total.eur_planted, Decimal)


# --- answer key parsing -------------------------------------------------------------------


def entry(error_no: int, entity_ref: str, **overrides: Any) -> dict[str, Any]:
    """One answer-key entry in the generator's JSON shape."""
    row: dict[str, Any] = {
        "error_no": error_no,
        "error_type": f"error_{error_no}",
        "entity_type": "platform_invoice",
        "entity_ref": entity_ref,
        "client_code": "ACME",
        "platform": "meta",
        "period_start": "2026-06-01",
        "period_end": "2026-06-30",
        "magnitude": "100.00",
        "expected_detector": "reconciliation",
        "detectable_in_stage": 1,
        "details": {"pct": "0.05"},
    }
    return {**row, **overrides}


def key_payload(entries: list[dict[str, Any]], seed: int = 7) -> dict[str, Any]:
    return {
        "seed": seed,
        "error_rate": "0.05",
        "min_per_type": 3,
        "allocations": [],
        "errors": entries,
    }


def write_key(path: Path, entries: list[dict[str, Any]], seed: int = 7) -> Path:
    path.write_text(json.dumps(key_payload(entries, seed)), encoding="utf-8")
    return path


def test_parse_answer_key_reads_money_as_decimal(tmp_path: Path) -> None:
    key = read_answer_key(write_key(tmp_path / "key.json", [entry(1, "INV-1", magnitude="12.34")]))
    assert key.seed == 7
    assert key.errors[0].magnitude == Decimal("12.34")
    assert key.errors[0].period_start == JUNE


def test_parse_answer_key_rejects_floats(tmp_path: Path) -> None:
    path = tmp_path / "key.json"
    path.write_text(
        json.dumps(key_payload([entry(1, "INV-1")])).replace('"100.00"', "100.0"),
        encoding="utf-8",
    )
    with pytest.raises(AnswerKeyError, match="floats"):
        read_answer_key(path)


@pytest.mark.parametrize(
    "bad",
    [
        {"magnitude": 100},
        {"magnitude": "abc"},
        {"magnitude": "1.234"},
        {"period_start": "June"},
        {"platform": 3},
        {"detectable_in_stage": "1"},
    ],
)
def test_parse_answer_key_rejects_bad_fields(bad: dict[str, Any]) -> None:
    with pytest.raises(AnswerKeyError):
        parse_answer_key(key_payload([entry(1, "INV-1", **bad)]))


def test_parse_answer_key_requires_every_field() -> None:
    row = entry(1, "INV-1")
    del row["client_code"]
    with pytest.raises(AnswerKeyError, match="client_code"):
        parse_answer_key(key_payload([row]))


def test_loader_main_reports_a_missing_file_without_touching_the_db(tmp_path: Path) -> None:
    assert loader.main(["--path", str(tmp_path / "absent.json")]) == 2


def test_eval_runner_url_requires_its_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVAL_RUNNER_DB_PASSWORD", raising=False)
    settings = Settings(_env_file=None, database_url="postgresql+psycopg://u:p@localhost/db")
    with pytest.raises(MissingRolePasswordError, match="EVAL_RUNNER_DB_PASSWORD"):
        eval_runner_database_url(settings)


# --- database: loader and eval as eval_runner ---------------------------------------------


@dataclass(frozen=True)
class EvalDb:
    owner: Engine
    eval_runner: Engine


@pytest.fixture(scope="module")
def module_eval_db(database_url: str) -> Iterator[EvalDb]:
    try:
        passwords = role_passwords(get_settings())
    except MissingRolePasswordError as exc:
        pytest.fail(str(exc))
    with temporary_database(database_url) as url:
        owner = create_engine(url)
        with owner.begin() as conn:
            ensure_roles(conn, passwords)
        command.upgrade(alembic_config(url), "head")
        settings = Settings(
            _env_file=None,
            database_url=url.render_as_string(hide_password=False),
            eval_runner_db_password=passwords["eval_runner"],
        )
        eval_runner = create_engine(eval_runner_database_url(settings))
        try:
            yield EvalDb(owner=owner, eval_runner=eval_runner)
        finally:
            eval_runner.dispose()
            owner.dispose()


@pytest.fixture
def eval_db(module_eval_db: EvalDb) -> EvalDb:
    with module_eval_db.owner.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE eval.answer_key, app.issues, app.pipeline_runs, core.clients, "
                "core.platforms RESTART IDENTITY CASCADE"
            )
        )
    return module_eval_db


@dataclass(frozen=True)
class World:
    client_id: int
    platform_id: int


def seed_world(engine: Engine) -> World:
    with engine.begin() as conn:
        client_id = insert(
            conn,
            "INSERT INTO core.clients (code, name, industry, billing_model) "
            "VALUES ('ACME', 'UAB Acme', 'retail', 'client_owned') RETURNING id",
        )
        platform_id = insert(
            conn,
            "INSERT INTO core.platforms (name, timezone, metric_map) "
            "VALUES ('meta', 'Europe/Vilnius', '{}') RETURNING id",
        )
    return World(client_id=client_id, platform_id=platform_id)


def seed_run(engine: Engine, status: str = "succeeded", hour: int = 6) -> uuid.UUID:
    run_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO app.pipeline_runs (id, status, started_at, finished_at) "
                "VALUES (:id, :s, :t, :t)"
            ),
            {"id": run_id, "s": status, "t": datetime(2026, 11, 1, hour, tzinfo=UTC)},
        )
    return run_id


def seed_issue(
    engine: Engine,
    world: World,
    run_id: uuid.UUID,
    issue_type: str,
    *,
    entity_type: str | None = None,
    entity_id: str | None = None,
    status: str = "open",
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO app.issues (dedup_key, type, severity, client_id, platform_id, "
                "period, entity_type, entity_id, explanation, status, run_id) "
                "VALUES (:k, :t, 'high', :c, :p, :m, :et, :e, 'x', :s, :r)"
            ),
            {
                "k": uuid.uuid4().hex,
                "t": issue_type,
                "c": world.client_id,
                "p": world.platform_id,
                "m": JUNE,
                "et": entity_type,
                "e": entity_id,
                "s": status,
                "r": run_id,
            },
        )


def seed_linked_campaign(
    engine: Engine, world: World, campaign_ref: str, platform_campaign_id: str
) -> None:
    """One June insertion order and the platform campaign linked to it."""
    params = {
        "c": world.client_id,
        "p": world.platform_id,
        "ref": campaign_ref,
        "pc": platform_campaign_id,
        "r": uuid.uuid4(),
    }
    with engine.begin() as conn:
        io_id = conn.execute(
            text(
                "INSERT INTO core.insertion_orders (client_id, platform_id, campaign_ref, "
                "start_date, end_date, budget, run_id) "
                "VALUES (:c, :p, :ref, '2026-06-01', '2026-06-30', 1000.00, :r) RETURNING id"
            ),
            params,
        ).scalar_one()
        account_id = conn.execute(
            text(
                "INSERT INTO core.ad_accounts (client_id, platform_id, external_account_id, "
                "timezone, run_id) VALUES (:c, :p, 'act-1', 'Europe/Vilnius', :r) RETURNING id"
            ),
            params,
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO core.campaigns (platform_id, ad_account_id, platform_campaign_id, "
                "name, io_id, run_id) VALUES (:p, :a, :pc, 'x', :io, :r)"
            ),
            {**params, "a": account_id, "io": io_id},
        )


def load_key(engine: Engine, tmp_path: Path, entries: list[dict[str, Any]]) -> int:
    return loader.load_answer_key(engine, read_answer_key(write_key(tmp_path / "k.json", entries)))


def answer_key_rows(engine: Engine) -> list[Any]:
    with engine.connect() as conn:
        return conn.execute(text("SELECT * FROM eval.answer_key ORDER BY id")).all()


def test_loader_writes_rows_with_key_fields_in_details(eval_db: EvalDb, tmp_path: Path) -> None:
    assert (
        load_key(eval_db.owner, tmp_path, [entry(1, "INV-1"), entry(8, "C1", platform=None)]) == 2
    )
    rows = answer_key_rows(eval_db.owner)
    assert [r.entity_ref for r in rows] == ["INV-1", "C1"]
    assert rows[0].magnitude == Decimal("100.00")
    assert rows[0].details == {
        "error_no": 1,
        "entity_type": "platform_invoice",
        "client_code": "ACME",
        "platform": "meta",
        "detectable_in_stage": 1,
        "seed": 7,
        "planted": {"pct": "0.05"},
    }
    assert rows[1].details["platform"] is None


def test_loader_replaces_the_previous_key(eval_db: EvalDb, tmp_path: Path) -> None:
    load_key(eval_db.owner, tmp_path, [entry(1, "INV-1"), entry(2, "INV-2")])
    load_key(eval_db.owner, tmp_path, [entry(1, "INV-1"), entry(2, "INV-2")])
    assert len(answer_key_rows(eval_db.owner)) == 2
    load_key(eval_db.owner, tmp_path, [entry(9, "C9")])
    assert [r.entity_ref for r in answer_key_rows(eval_db.owner)] == ["C9"]


def test_eval_runner_cannot_run_the_loader(eval_db: EvalDb, tmp_path: Path) -> None:
    load_key(eval_db.owner, tmp_path, [entry(1, "INV-1")])
    with pytest.raises(DBAPIError, match="permission denied"):
        load_key(eval_db.eval_runner, tmp_path, [entry(2, "INV-2")])
    assert [r.entity_ref for r in answer_key_rows(eval_db.owner)] == ["INV-1"]


def run_eval(eval_db: EvalDb, tmp_path: Path, max_false_alarms: int = 0) -> int:
    return evaluate(eval_db.eval_runner, tmp_path / "eval-results.md", max_false_alarms)


def test_eval_passes_when_every_error_is_found(eval_db: EvalDb, tmp_path: Path) -> None:
    world = seed_world(eval_db.owner)
    run_id = seed_run(eval_db.owner)
    load_key(
        eval_db.owner,
        tmp_path,
        [
            entry(1, "INV-SECRET-1"),
            entry(8, "CAMPAIGN-SECRET-8", magnitude="1.00", detectable_in_stage=4),
        ],
    )
    seed_issue(eval_db.owner, world, run_id, "reconciliation_overcharge")
    assert run_eval(eval_db, tmp_path) == EXIT_PASS

    results = (tmp_path / "eval-results.md").read_text(encoding="utf-8")
    assert "Result: **PASS**" in results
    assert str(run_id) in results
    assert "| **overall** | 2 | 1 | 0 | 1 | 0 | 101.00 | 100.00 |" in results
    assert "SECRET" not in results
    assert "ACME" not in results


def test_eval_fails_on_a_missed_error(
    eval_db: EvalDb, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_world(eval_db.owner)
    seed_run(eval_db.owner)
    load_key(eval_db.owner, tmp_path, [entry(1, "INV-1")])
    assert run_eval(eval_db, tmp_path) == EXIT_FAIL
    assert "missed: #1 error_1 INV-1" in capsys.readouterr().out
    assert "Result: **FAIL**" in (tmp_path / "eval-results.md").read_text(encoding="utf-8")


def test_eval_fails_above_the_false_alarm_limit(eval_db: EvalDb, tmp_path: Path) -> None:
    world = seed_world(eval_db.owner)
    run_id = seed_run(eval_db.owner)
    load_key(eval_db.owner, tmp_path, [entry(8, "C8", detectable_in_stage=4)])
    seed_issue(eval_db.owner, world, run_id, "reconciliation_budget_overspend")
    assert run_eval(eval_db, tmp_path) == EXIT_FAIL
    assert run_eval(eval_db, tmp_path, max_false_alarms=1) == EXIT_PASS


def test_eval_finds_pacing_drift_on_a_taken_over_issue(
    eval_db: EvalDb, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pacing takes over the zero-spend issue, which keeps the platform campaign id."""
    world = seed_world(eval_db.owner)
    run_id = seed_run(eval_db.owner)
    seed_linked_campaign(eval_db.owner, world, "ACME-META-2606", "120001")
    load_key(
        eval_db.owner,
        tmp_path,
        [
            entry(
                7,
                "ACME-META-2606",
                error_type="pacing_drift",
                entity_type="campaign",
                details={"platform_campaign_id": "120001"},
            )
        ],
    )
    seed_issue(
        eval_db.owner,
        world,
        run_id,
        "pacing_overpacing",
        entity_type="campaign",
        entity_id="120001",
    )
    assert run_eval(eval_db, tmp_path) == EXIT_PASS
    out = capsys.readouterr().out
    assert "missed:" not in out
    assert "false alarm" not in out
    results = (tmp_path / "eval-results.md").read_text(encoding="utf-8")
    assert "| #7 pacing_drift | 1 | 1 | 0 | 0 | 0 |" in results


def test_eval_reads_only_open_issues_of_the_latest_finished_run(
    eval_db: EvalDb, tmp_path: Path
) -> None:
    world = seed_world(eval_db.owner)
    old_run = seed_run(eval_db.owner, hour=1)
    latest = seed_run(eval_db.owner, hour=2)
    failed = seed_run(eval_db.owner, status="failed", hour=3)
    load_key(eval_db.owner, tmp_path, [entry(1, "INV-1")])
    seed_issue(eval_db.owner, world, latest, "reconciliation_overcharge")
    seed_issue(eval_db.owner, world, latest, "reconciliation_undercharge", status="rejected")
    seed_issue(eval_db.owner, world, old_run, "reconciliation_budget_overspend")
    seed_issue(eval_db.owner, world, failed, "reconciliation_budget_overspend")
    assert run_eval(eval_db, tmp_path) == EXIT_PASS


@pytest.mark.parametrize(
    ("in_latest_run", "row"), [(True, "| 1 | 1 | 0 | 0 | 0 |"), (False, "| 1 | 0 | 0 | 1 | 0 |")]
)
def test_eval_finds_a_rename_only_in_the_latest_run(
    eval_db: EvalDb, tmp_path: Path, in_latest_run: bool, row: str
) -> None:
    """match_names moves a re-detected rename's run_id to the latest run (decision 047)."""
    world = seed_world(eval_db.owner)
    old_run = seed_run(eval_db.owner, hour=1)
    latest = seed_run(eval_db.owner, hour=2)
    load_key(
        eval_db.owner,
        tmp_path,
        [
            entry(
                3,
                "ACME-META-2606",
                error_type="campaign_renamed",
                entity_type="campaign",
                detectable_in_stage=2,
                details={"platform_campaign_id": "120003"},
            )
        ],
    )
    seed_issue(
        eval_db.owner,
        world,
        latest if in_latest_run else old_run,
        "reconciliation_campaign_renamed",
        entity_type="campaign",
        entity_id="120003",
    )
    assert run_eval(eval_db, tmp_path) == EXIT_PASS
    results = (tmp_path / "eval-results.md").read_text(encoding="utf-8")
    assert f"| #3 campaign_renamed {row}" in results


def test_eval_cannot_run_without_an_answer_key(eval_db: EvalDb, tmp_path: Path) -> None:
    seed_run(eval_db.owner)
    assert run_eval(eval_db, tmp_path) == EXIT_SETUP
    assert not (tmp_path / "eval-results.md").exists()


def test_eval_cannot_run_without_a_finished_pipeline_run(eval_db: EvalDb, tmp_path: Path) -> None:
    load_key(eval_db.owner, tmp_path, [entry(1, "INV-1")])
    seed_run(eval_db.owner, status="failed")
    assert run_eval(eval_db, tmp_path) == EXIT_SETUP
