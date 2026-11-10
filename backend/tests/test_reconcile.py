"""The reconcile step: reading delivery evidence, and writing as `etl` and `app`."""

import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from app.reconciliation import templates
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError
from tests.conftest import PipelineDb

from pipelines import reconcile as reconcile_module
from pipelines.normalize import DeliveryRecord
from pipelines.quality import check_missing_days, dedup_key, write_findings
from pipelines.reconcile import ReconcileResult, delivery_evidence, load_delivery, reconcile

VILNIUS = "Europe/Vilnius"
JUNE = date(2026, 6, 1)
REF = "AAA-META-2606"
CLIENTS = {("meta", "c1"): "AAA"}


def record(day: date, spend: str = "10.00", **overrides: Any) -> DeliveryRecord:
    values: dict[str, Any] = {
        "platform": "meta",
        "account_id": "1001",
        "timezone": "UTC",
        "campaign_id": "c1",
        "campaign_name": f"Campaign [{REF}]",
        "date": day,
        "spend": Decimal(spend),
        "impressions": 1000,
        "clicks": 10,
        "conversions": 1,
    }
    return DeliveryRecord(**{**values, **overrides})


def test_delivery_evidence_finds_a_shift_with_its_spend_and_quality_key() -> None:
    records = [record(date(2026, 6, 29)), record(date(2026, 7, 1), "42.50")]
    shifts, gaps = delivery_evidence(records, CLIENTS)
    assert gaps == []
    assert (shifts[0].missing_day, shifts[0].row_day) == (date(2026, 6, 30), date(2026, 7, 1))
    assert (shifts[0].spend, shifts[0].timezone, shifts[0].client) == (
        Decimal("42.50"),
        "UTC",
        "AAA",
    )
    assert shifts[0].issue_key == dedup_key(check_missing_days(records)[0])


def test_delivery_evidence_finds_a_gap_with_its_quality_key() -> None:
    records = [record(date(2026, 6, 1)), record(date(2026, 6, 4))]
    shifts, gaps = delivery_evidence(records, CLIENTS)
    assert shifts == []
    assert (gaps[0].first_day, gaps[0].last_day) == (date(2026, 6, 2), date(2026, 6, 3))
    assert gaps[0].issue_key == dedup_key(check_missing_days(records)[0])


def insert_returning(conn: Connection, statement: str, **values: Any) -> int:
    return conn.execute(text(statement), values).scalar_one()


def seed_world(db: PipelineDb, days: list[tuple[date, str]], invoice_lines: list[str]) -> None:
    """One client, account, order and campaign, its delivery and one platform invoice."""
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
            "VALUES (:c, :p, :ref, '2026-06-01', '2026-06-30', 1000, :r) RETURNING id",
            c=client,
            p=platform,
            ref=REF,
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
        for day, spend in days:
            conn.execute(
                text(
                    "INSERT INTO core.daily_delivery "
                    "(campaign_id, date, spend, impressions, clicks, conversions, run_id) "
                    "VALUES (:c, :d, :s, 1000, 10, 1, :r)"
                ),
                {"c": campaign, "d": day, "s": Decimal(spend), "r": run_id},
            )
        invoice = insert_returning(
            conn,
            "INSERT INTO core.invoices (kind, vendor, number, client_id, platform_id, "
            "issue_date, period_start, period_end, subtotal, vat, total, run_id) "
            "VALUES ('platform', 'Meta', 'INV-1', :c, :p, '2026-07-02', '2026-06-01', "
            "'2026-06-30', 0, 0, 0, :r) RETURNING id",
            c=client,
            p=platform,
            r=run_id,
        )
        for line_no, amount in enumerate(invoice_lines, start=1):
            conn.execute(
                text(
                    "INSERT INTO core.invoice_lines "
                    "(invoice_id, line_no, campaign_ref, amount, run_id) "
                    "VALUES (:i, :n, :ref, :a, :r)"
                ),
                {"i": invoice, "n": line_no, "ref": REF, "a": Decimal(amount), "r": run_id},
            )


def june_days(*spends: str) -> list[tuple[date, str]]:
    return [(JUNE + timedelta(days=offset), spend) for offset, spend in enumerate(spends)]


def run(db: PipelineDb, run_id: uuid.UUID | None = None) -> ReconcileResult:
    return reconcile(run_id or uuid.uuid4(), db.etl, db.app, VILNIUS)


def run_quality(db: PipelineDb) -> None:
    with db.etl.connect() as conn:
        records, _ = load_delivery(conn)
    write_findings(uuid.uuid4(), check_missing_days(records), db.app)


def issues(db: PipelineDb) -> list[Any]:
    with db.owner.connect() as conn:
        return list(conn.execute(text("SELECT * FROM app.issues ORDER BY id")).mappings())


def results(db: PipelineDb) -> list[Any]:
    query = text("SELECT * FROM core.reconciliation_results ORDER BY period")
    with db.owner.connect() as conn:
        return list(conn.execute(query).mappings())


def test_results_and_issues_are_written(pipeline_db: PipelineDb) -> None:
    seed_world(pipeline_db, june_days("100.00"), ["110.00"])
    run_id = uuid.uuid4()
    result = run(pipeline_db, run_id)
    assert (result.results_written, result.issues_written) == (1, 1)
    row = results(pipeline_db)[0]
    assert row["period"] == JUNE
    assert (row["delivered"], row["invoiced"], row["variance"]) == (
        Decimal("100.00"),
        Decimal("110.00"),
        Decimal("10.00"),
    )
    assert (row["status"], row["run_id"]) == ("issue", run_id)
    assert row["planned"] is None
    issue = issues(pipeline_db)[0]
    assert (issue["type"], issue["amount"]) == (templates.OVERCHARGE, Decimal("10.00"))
    assert (issue["status"], issue["period"]) == ("open", JUNE)
    assert issue["client_id"] is not None and issue["platform_id"] is not None


def test_invoice_lines_are_linked_and_otherwise_untouched(pipeline_db: PipelineDb) -> None:
    seed_world(pipeline_db, june_days("110.00"), ["110.00"])
    assert run(pipeline_db).lines_linked == 1
    with pipeline_db.owner.connect() as conn:
        line = conn.execute(text("SELECT * FROM core.invoice_lines")).mappings().one()
        campaign = conn.execute(text("SELECT id FROM core.campaigns")).scalar_one()
    assert line["campaign_id"] == campaign
    assert (line["amount"], line["campaign_ref"]) == (Decimal("110.00"), REF)


def test_a_link_is_not_cleared_when_the_code_stops_matching(pipeline_db: PipelineDb) -> None:
    seed_world(pipeline_db, june_days("110.00"), ["110.00"])
    run(pipeline_db)
    with pipeline_db.owner.begin() as conn:
        conn.execute(text("UPDATE core.invoice_lines SET campaign_ref = 'GONE'"))
    run(pipeline_db)
    with pipeline_db.owner.connect() as conn:
        linked = conn.execute(text("SELECT campaign_id FROM core.invoice_lines")).scalar_one()
    assert linked is not None


def test_a_credit_note_does_not_change_invoice_vs_delivered(pipeline_db: PipelineDb) -> None:
    seed_world(pipeline_db, june_days("100.00"), ["100.00"])
    with pipeline_db.owner.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO core.credit_notes "
                "(original_invoice_id, vendor, number, amount, run_id) "
                "SELECT id, 'Meta', 'CN-1', 20, gen_random_uuid() FROM core.invoices"
            )
        )
    run(pipeline_db)
    row = results(pipeline_db)[0]
    assert (row["invoiced"], row["status"]) == (Decimal("100.00"), "ok")
    assert issues(pipeline_db) == []


def test_running_twice_changes_no_row_counts(pipeline_db: PipelineDb) -> None:
    seed_world(pipeline_db, june_days("100.00"), ["110.00"])
    run(pipeline_db)
    first = (results(pipeline_db), issues(pipeline_db))
    second_run = uuid.uuid4()
    run(pipeline_db, second_run)
    assert (len(results(pipeline_db)), len(issues(pipeline_db))) == (len(first[0]), len(first[1]))
    assert results(pipeline_db)[0]["id"] == first[0][0]["id"]
    assert issues(pipeline_db)[0]["id"] == first[1][0]["id"]
    assert issues(pipeline_db)[0]["run_id"] == second_run


def test_a_re_run_keeps_a_human_decision_and_refreshes_the_amount(
    pipeline_db: PipelineDb,
) -> None:
    seed_world(pipeline_db, june_days("100.00"), ["110.00"])
    run(pipeline_db)
    with pipeline_db.owner.begin() as conn:
        conn.execute(text("UPDATE app.issues SET status = 'rejected', decision_note = 'known'"))
        conn.execute(text("UPDATE core.invoice_lines SET amount = 120"))
    run(pipeline_db)
    issue = issues(pipeline_db)[0]
    assert (issue["status"], issue["decision_note"]) == ("rejected", "known")
    assert issue["amount"] == Decimal("20.00")


def shift_days() -> list[tuple[date, str]]:
    """A June campaign whose June 30th row is dated July 1st."""
    return [(date(2026, 6, 29), "100.00"), (date(2026, 7, 1), "50.00")]


def test_a_month_end_shift_is_one_issue_with_quality(pipeline_db: PipelineDb) -> None:
    seed_world(pipeline_db, shift_days(), ["150.00"])
    run_quality(pipeline_db)
    assert [issue["type"] for issue in issues(pipeline_db)] == ["quality_month_boundary_shift"]
    run(pipeline_db)
    only = issues(pipeline_db)
    assert len(only) == 1
    assert only[0]["type"] == templates.TIMEZONE_SHIFT
    assert only[0]["amount"] == Decimal("50.00")
    assert "not an overcharge" in only[0]["explanation"]


def test_a_full_re_run_keeps_one_shift_issue_and_the_decision(pipeline_db: PipelineDb) -> None:
    seed_world(pipeline_db, shift_days(), ["150.00"])
    run_quality(pipeline_db)
    run(pipeline_db)
    with pipeline_db.owner.begin() as conn:
        conn.execute(text("UPDATE app.issues SET status = 'approved'"))
    before = issues(pipeline_db)[0]
    run_quality(pipeline_db)
    run(pipeline_db)
    only = issues(pipeline_db)
    assert len(only) == 1
    assert only[0]["status"] == "approved"
    assert (only[0]["type"], only[0]["explanation"], only[0]["severity"]) == (
        before["type"],
        before["explanation"],
        before["severity"],
    )


def gap_days() -> list[tuple[date, str]]:
    """June 3rd and 4th are missing; the highest daily spend is 100.00."""
    return [(date(2026, 6, 1), "100.00"), (date(2026, 6, 2), "80.00"), (date(2026, 6, 5), "20.00")]


def test_a_variance_within_the_limit_is_added_to_the_quality_issue(
    pipeline_db: PipelineDb,
) -> None:
    seed_world(pipeline_db, gap_days(), ["350.00"])
    run_quality(pipeline_db)
    result = run(pipeline_db)
    assert (result.gap_notes_attached, result.gap_notes_unattached) == (1, 0)
    only = issues(pipeline_db)
    assert len(only) == 1
    assert only[0]["type"] == "quality_missing_days"
    assert only[0]["amount"] == Decimal("150.00")
    assert "EUR 200.00" in only[0]["explanation"]


def test_a_variance_above_the_limit_is_a_separate_overcharge(pipeline_db: PipelineDb) -> None:
    seed_world(pipeline_db, gap_days(), ["400.01"])
    run_quality(pipeline_db)
    run(pipeline_db)
    assert sorted(issue["type"] for issue in issues(pipeline_db)) == sorted(
        ["quality_missing_days", templates.OVERCHARGE]
    )


def test_a_gap_without_a_quality_issue_is_counted_and_creates_nothing(
    pipeline_db: PipelineDb,
) -> None:
    seed_world(pipeline_db, gap_days(), ["350.00"])
    result = run(pipeline_db)
    assert (result.gap_notes_attached, result.gap_notes_unattached) == (0, 1)
    assert issues(pipeline_db) == []


def test_etl_cannot_write_issues(pipeline_db: PipelineDb) -> None:
    seed_world(pipeline_db, june_days("100.00"), ["110.00"])
    with pytest.raises(DBAPIError, match="permission denied"):
        reconcile(uuid.uuid4(), pipeline_db.etl, pipeline_db.etl, VILNIUS)


def test_app_cannot_write_results(pipeline_db: PipelineDb) -> None:
    seed_world(pipeline_db, june_days("100.00"), ["110.00"])
    with pytest.raises(DBAPIError, match="permission denied"):
        reconcile(uuid.uuid4(), pipeline_db.app, pipeline_db.app, VILNIUS)


def test_source_never_mentions_the_answer_key() -> None:
    source = Path(reconcile_module.__file__).read_text(encoding="utf-8")
    assert "answer_key" not in source
