"""The pacing step: reading `core` and writing `app.issues` as `app`."""

import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from app.reconciliation import templates
from app.reconciliation.config import PACING_WEEKDAY_WEIGHTS
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError
from tests.conftest import PipelineDb

from pipelines import pacing as pacing_module
from pipelines.pacing import PaceResult, pace
from pipelines.quality import check_zero_spend, write_findings
from pipelines.reconcile import load_delivery

JUNE = date(2026, 6, 1)
JUNE_END = date(2026, 6, 30)
REF = "AAA-META-2606"
PACING_KEY = f"pacing:AAA:meta:{REF}:2026-06"


def insert_returning(conn: Connection, statement: str, **values: Any) -> int:
    return conn.execute(text(statement), values).scalar_one()


def seed_order(db: PipelineDb, spends: list[str], budget: str = "3000.00") -> None:
    """One client, account, June order and linked campaign, with a row per day from June 1."""
    run_id = uuid.uuid4()
    with db.owner.begin() as conn:
        client = insert_returning(
            conn,
            "INSERT INTO core.clients (code, name, industry, billing_model, markup_pct) "
            "VALUES ('AAA', 'UAB Aaa', 'food', 'markup', 15) RETURNING id",
        )
        platform = insert_returning(
            conn,
            "INSERT INTO core.platforms (name, timezone, metric_map) "
            "VALUES ('meta', 'UTC', '{}') RETURNING id",
        )
        account = insert_returning(
            conn,
            "INSERT INTO core.ad_accounts "
            "(client_id, platform_id, external_account_id, timezone, run_id) "
            "VALUES (:c, :p, '1001', 'UTC', :r) RETURNING id",
            c=client,
            p=platform,
            r=run_id,
        )
        order = insert_returning(
            conn,
            "INSERT INTO core.insertion_orders "
            "(client_id, platform_id, campaign_ref, start_date, end_date, budget, run_id) "
            "VALUES (:c, :p, :ref, '2026-06-01', '2026-06-30', :b, :r) RETURNING id",
            c=client,
            p=platform,
            ref=REF,
            b=Decimal(budget),
            r=run_id,
        )
        campaign = insert_returning(
            conn,
            "INSERT INTO core.campaigns "
            "(platform_id, ad_account_id, platform_campaign_id, name, io_id, run_id) "
            "VALUES (:p, :a, 'c1', :name, :o, :r) RETURNING id",
            p=platform,
            a=account,
            name=f"Campaign [{REF}]",
            o=order,
            r=run_id,
        )
        for offset, spend in enumerate(spends):
            conn.execute(
                text(
                    "INSERT INTO core.daily_delivery "
                    "(campaign_id, date, spend, impressions, clicks, conversions, run_id) "
                    "VALUES (:c, :d, :s, 1000, 10, 1, :r)"
                ),
                {
                    "c": campaign,
                    "d": JUNE + timedelta(days=offset),
                    "s": Decimal(spend),
                    "r": run_id,
                },
            )


def drift_spends(tail: str = "0.00") -> list[str]:
    """EUR 3,000 spent over the first 18 of 30 days, then `tail` a day."""
    return ["166.67"] * 17 + ["166.61"] + [tail] * 12


def run(db: PipelineDb, as_of: date | None = None, run_id: uuid.UUID | None = None) -> PaceResult:
    return pace(run_id or uuid.uuid4(), db.app, as_of)


def run_quality(db: PipelineDb) -> None:
    with db.etl.connect() as conn:
        records, _ = load_delivery(conn)
    write_findings(uuid.uuid4(), check_zero_spend(records), db.app)


def issues(db: PipelineDb) -> list[Any]:
    with db.owner.connect() as conn:
        return list(conn.execute(text("SELECT * FROM app.issues ORDER BY id")).mappings())


def decide(db: PipelineDb) -> None:
    with db.owner.begin() as conn:
        conn.execute(text("UPDATE app.issues SET status = 'approved', decision_note = 'known'"))


def test_a_drift_without_zero_spend_is_written_on_the_pacing_key(
    pipeline_db: PipelineDb,
) -> None:
    seed_order(pipeline_db, drift_spends(tail="0.01"))
    run_id = uuid.uuid4()
    result = run(pipeline_db, run_id=run_id)
    assert (result.as_of, result.orders_paced, result.issues_written) == (JUNE_END, 1, 1)
    assert result.statuses == {"green": 1}
    [issue] = issues(pipeline_db)
    assert issue["dedup_key"] == PACING_KEY
    assert (issue["type"], issue["severity"]) == (templates.PACING_OVERPACING, "high")
    assert (issue["entity_type"], issue["entity_id"]) == ("insertion_order", REF)
    assert issue["period"] == JUNE
    assert issue["amount"] > 0
    assert (issue["status"], issue["run_id"]) == ("open", run_id)


def test_a_green_order_writes_nothing(pipeline_db: PipelineDb) -> None:
    seed_order(pipeline_db, ["100.00"] * 30)
    result = run(pipeline_db)
    assert (result.issues_written, issues(pipeline_db)) == (0, [])


def test_as_of_defaults_to_the_last_delivered_date(pipeline_db: PipelineDb) -> None:
    seed_order(pipeline_db, ["100.00"] * 12)
    assert run(pipeline_db).as_of == JUNE + timedelta(days=11)


def test_an_explicit_as_of_is_honoured(pipeline_db: PipelineDb) -> None:
    seed_order(pipeline_db, ["130.00"] * 10 + ["70.00"] * 20)
    early = run(pipeline_db, as_of=date(2026, 6, 10))
    assert (early.as_of, early.statuses) == (date(2026, 6, 10), {"red": 1})
    assert issues(pipeline_db)[0]["type"] == templates.PACING_OVERPACING


def test_no_delivery_and_no_date_paces_nothing(pipeline_db: PipelineDb) -> None:
    seed_order(pipeline_db, [])
    result = run(pipeline_db)
    assert (result.as_of, result.orders_paced, issues(pipeline_db)) == (None, 0, [])


def test_running_twice_keeps_one_issue_and_refreshes_its_run(pipeline_db: PipelineDb) -> None:
    seed_order(pipeline_db, drift_spends())
    run(pipeline_db)
    first = issues(pipeline_db)
    second_run = uuid.uuid4()
    run(pipeline_db, run_id=second_run)
    again = issues(pipeline_db)
    assert [row["id"] for row in again] == [row["id"] for row in first]
    assert again[0]["run_id"] == second_run


def test_a_re_run_keeps_a_human_decision_and_updates_the_rest(pipeline_db: PipelineDb) -> None:
    seed_order(pipeline_db, ["120.00"] * 30)
    run(pipeline_db, as_of=date(2026, 6, 10))
    assert issues(pipeline_db)[0]["severity"] == "medium"
    decide(pipeline_db)
    with pipeline_db.owner.begin() as conn:
        conn.execute(text("UPDATE core.daily_delivery SET spend = 140"))
    run(pipeline_db, as_of=date(2026, 6, 10))
    [issue] = issues(pipeline_db)
    assert (issue["status"], issue["decision_note"]) == ("approved", "known")
    assert issue["severity"] == "high"
    assert issue["amount"] == Decimal("1400.00") - issue_plan_to_june_10()


def issue_plan_to_june_10() -> Decimal:
    weights = PACING_WEEKDAY_WEIGHTS
    days = [JUNE + timedelta(days=offset) for offset in range(30)]
    total = sum(weights[day.weekday()] for day in days)
    to_date = sum(weights[day.weekday()] for day in days[:10])
    return templates.cents(Decimal("3000.00") * to_date / total)


def test_a_drift_takes_over_the_quality_zero_spend_issue(pipeline_db: PipelineDb) -> None:
    seed_order(pipeline_db, drift_spends())
    run_quality(pipeline_db)
    [quality] = issues(pipeline_db)
    assert quality["type"] == "quality_zero_spend"
    decide(pipeline_db)
    result = run(pipeline_db)
    assert (result.issues_written, result.quality_issues_taken_over) == (1, 1)
    [issue] = issues(pipeline_db)
    assert (issue["id"], issue["dedup_key"]) == (quality["id"], quality["dedup_key"])
    assert (issue["type"], issue["severity"]) == (templates.PACING_OVERPACING, "high")
    assert (issue["status"], issue["decision_note"]) == ("approved", "known")
    assert issue["amount"] > 0


def test_a_drift_before_quality_runs_lands_on_the_quality_key(pipeline_db: PipelineDb) -> None:
    seed_order(pipeline_db, drift_spends())
    run(pipeline_db)
    run_quality(pipeline_db)
    [issue] = issues(pipeline_db)
    assert issue["dedup_key"].startswith("quality:zero_spend:")
    assert issue["type"] == templates.PACING_OVERPACING


def test_quality_and_pacing_re_runs_keep_one_pacing_issue(pipeline_db: PipelineDb) -> None:
    seed_order(pipeline_db, drift_spends())
    run_quality(pipeline_db)
    run(pipeline_db)
    run_quality(pipeline_db)
    after_quality = issues(pipeline_db)
    assert [row["type"] for row in after_quality] == [templates.PACING_OVERPACING]
    run(pipeline_db)
    [issue] = issues(pipeline_db)
    assert issue["type"] == templates.PACING_OVERPACING
    assert issue["explanation"].startswith(f"AAA meta {REF}")


def test_an_existing_pacing_issue_is_kept_and_quality_is_left_alone(
    pipeline_db: PipelineDb,
) -> None:
    seed_order(pipeline_db, drift_spends())
    run(pipeline_db, as_of=date(2026, 6, 12))
    run_quality(pipeline_db)
    result = run(pipeline_db)
    assert result.quality_issues_taken_over == 0
    by_key = {row["dedup_key"]: row for row in issues(pipeline_db)}
    assert len(by_key) == 2
    assert by_key[PACING_KEY]["type"] == templates.PACING_OVERPACING
    [quality_key] = by_key.keys() - {PACING_KEY}
    assert by_key[quality_key]["type"] == "quality_zero_spend"


def test_an_unlinked_order_is_skipped(pipeline_db: PipelineDb) -> None:
    seed_order(pipeline_db, ["10.00"] * 30)
    with pipeline_db.owner.begin() as conn:
        conn.execute(text("UPDATE core.campaigns SET io_id = NULL"))
    result = run(pipeline_db)
    assert (result.orders_paced, result.orders_skipped, issues(pipeline_db)) == (0, 1, [])


def test_etl_cannot_write_issues(pipeline_db: PipelineDb) -> None:
    seed_order(pipeline_db, drift_spends())
    with pytest.raises(DBAPIError, match="permission denied"):
        pace(uuid.uuid4(), pipeline_db.etl)


def test_source_never_mentions_the_answer_key_or_eval() -> None:
    source = Path(pacing_module.__file__).read_text(encoding="utf-8")
    assert "answer_key" not in source
    assert "eval." not in source
