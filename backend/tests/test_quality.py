"""Quality checks on small inputs, and the route of findings to `app.issues`."""

import uuid
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from tests.conftest import PipelineDb

from pipelines import quality
from pipelines.normalize import DeliveryRecord, NormalizeResult, RejectedRow, UnknownAccount
from pipelines.quality import (
    IMPRESSIONS_BELOW_CLICKS,
    MISSING_DAYS,
    MONTH_BOUNDARY_SHIFT,
    NEGATIVE_VALUES,
    NULL_VALUES,
    REJECTED_ROWS,
    UNKNOWN_ACCOUNT,
    ZERO_SPEND,
    Finding,
    check_impressions_below_clicks,
    check_missing_days,
    check_negative_values,
    check_null_values,
    check_rejected_rows,
    check_unknown_accounts,
    check_zero_spend,
    dedup_key,
    run_quality_checks,
    write_findings,
)


def row(day: date, **overrides: Any) -> DeliveryRecord:
    values: dict[str, Any] = {
        "platform": "meta",
        "account_id": "1001",
        "timezone": "Europe/Vilnius",
        "campaign_id": "c1",
        "campaign_name": "Campaign [REF]",
        "date": day,
        "spend": Decimal("10.00"),
        "impressions": 1000,
        "clicks": 10,
        "conversions": 1,
    }
    return DeliveryRecord(**{**values, **overrides})


def october(*days: int, **overrides: Any) -> list[DeliveryRecord]:
    return [row(date(2026, 10, day), **overrides) for day in days]


def spans(findings: list[Finding]) -> list[tuple[str, date | None, date | None]]:
    return [(f.check, f.date_from, f.date_to) for f in findings]


def test_contiguous_days_have_no_gaps() -> None:
    assert check_missing_days(october(1, 2, 3)) == []


def test_gap_inside_the_span_is_one_finding() -> None:
    findings = check_missing_days(october(1, 2, 6, 7))
    assert spans(findings) == [(MISSING_DAYS, date(2026, 10, 3), date(2026, 10, 5))]


def test_two_gaps_are_two_findings() -> None:
    assert len(check_missing_days(october(1, 3, 5))) == 2


def test_days_outside_the_active_span_are_not_missing() -> None:
    assert check_missing_days(october(10, 11)) == []


def test_finding_carries_identity_and_message() -> None:
    finding = check_missing_days(october(1, 3))[0]
    assert (finding.platform, finding.account_id, finding.campaign_id) == ("meta", "1001", "c1")
    assert finding.campaign_name == "Campaign [REF]"
    assert "2026-10-02" in finding.message
    assert "Europe/Vilnius" in finding.message


def test_campaigns_are_checked_separately() -> None:
    records = [*october(1, 2, campaign_id="a"), *october(4, 5, campaign_id="b")]
    assert check_missing_days(records) == []


def test_month_end_gap_with_final_first_of_month_row_is_a_shift() -> None:
    records = [row(date(2026, 6, 29)), row(date(2026, 7, 1))]
    assert spans(check_missing_days(records)) == [
        (MONTH_BOUNDARY_SHIFT, date(2026, 6, 30), date(2026, 6, 30))
    ]
    assert check_missing_days(records)[0].severity == "low"


def test_month_end_gap_followed_by_more_rows_is_missing() -> None:
    records = [row(date(2026, 6, 29)), row(date(2026, 7, 1)), row(date(2026, 7, 2))]
    assert check_missing_days(records)[0].check == MISSING_DAYS


def test_two_day_gap_before_the_first_is_missing() -> None:
    records = [row(date(2026, 6, 28)), row(date(2026, 7, 1))]
    assert check_missing_days(records)[0].check == MISSING_DAYS


def test_single_mid_month_gap_at_the_end_is_missing() -> None:
    assert check_missing_days(october(29, 31))[0].check == MISSING_DAYS


def test_null_values_name_the_fields_and_group_days() -> None:
    records = [*october(1), *october(2, 3, clicks=None), *october(4)]
    findings = check_null_values(records)
    assert spans(findings) == [(NULL_VALUES, date(2026, 10, 2), date(2026, 10, 3))]
    assert "clicks" in findings[0].message


def test_complete_rows_have_no_nulls() -> None:
    assert check_null_values(october(1, 2)) == []


def test_negative_values_are_found() -> None:
    findings = check_negative_values(october(2, spend=Decimal("-5.00")))
    assert spans(findings) == [(NEGATIVE_VALUES, date(2026, 10, 2), date(2026, 10, 2))]
    assert "spend" in findings[0].message


def test_zero_is_not_negative() -> None:
    assert check_negative_values(october(2, spend=Decimal("0.00"))) == []


def test_zero_run_after_spend_is_one_finding() -> None:
    records = [*october(1, 2), *october(3, 4, 5, spend=Decimal("0.00"))]
    assert spans(check_zero_spend(records)) == [(ZERO_SPEND, date(2026, 10, 3), date(2026, 10, 5))]


def test_zero_at_campaign_start_is_not_sudden() -> None:
    records = [*october(1, 2, spend=Decimal("0.00")), *october(3)]
    assert check_zero_spend(records) == []


def test_zero_after_a_null_spend_day_is_not_sudden() -> None:
    records = [*october(1, spend=None), *october(2, spend=Decimal("0.00"))]
    assert check_zero_spend(records) == []


def test_impressions_below_clicks_is_found() -> None:
    records = october(2, impressions=5, clicks=10)
    assert spans(check_impressions_below_clicks(records)) == [
        (IMPRESSIONS_BELOW_CLICKS, date(2026, 10, 2), date(2026, 10, 2))
    ]


def test_equal_impressions_and_clicks_is_fine() -> None:
    assert check_impressions_below_clicks(october(2, impressions=10, clicks=10)) == []


def test_impressions_check_skips_nulls() -> None:
    assert check_impressions_below_clicks(october(2, impressions=None)) == []


def result_with(**fields: Any) -> NormalizeResult:
    return NormalizeResult(run_id=uuid.uuid4(), **fields)


def test_rejected_rows_group_by_account_and_reason() -> None:
    day = date(2026, 10, 2)
    rejected = [
        RejectedRow("meta", "1001", "spend is not a decimal string", day),
        RejectedRow("meta", "1001", "spend is not a decimal string", date(2026, 10, 4)),
        RejectedRow("meta", "1001", "date does not match", None),
    ]
    findings = check_rejected_rows(result_with(rejected=rejected))
    assert sorted(spans(findings), key=str) == sorted(
        [
            (REJECTED_ROWS, None, None),
            (REJECTED_ROWS, date(2026, 10, 2), date(2026, 10, 4)),
        ],
        key=str,
    )
    assert len({dedup_key(f) for f in findings}) == 2


def test_unknown_account_is_a_finding_with_its_period() -> None:
    account = UnknownAccount("google", "2001", date(2026, 5, 1), date(2026, 10, 31))
    finding = check_unknown_accounts(result_with(unknown_accounts=[account]))[0]
    assert (finding.check, finding.platform, finding.account_id) == (
        UNKNOWN_ACCOUNT,
        "google",
        "2001",
    )
    assert (finding.date_from, finding.date_to) == (date(2026, 5, 1), date(2026, 10, 31))


def test_run_combines_checks_in_a_stable_order() -> None:
    records = [*october(1, 3), *october(4, clicks=20, impressions=5)]
    first = run_quality_checks(result_with(records=records))
    second = run_quality_checks(result_with(records=list(reversed(records))))
    assert first == second
    assert {f.check for f in first} == {MISSING_DAYS, IMPRESSIONS_BELOW_CLICKS}


def test_clean_data_has_no_findings() -> None:
    assert run_quality_checks(result_with(records=october(1, 2, 3))) == []


def finding(**overrides: Any) -> Finding:
    base = check_missing_days(october(1, 3))[0]
    return replace(base, **overrides)


def seed_account(db: PipelineDb) -> tuple[int, int]:
    with db.owner.begin() as conn:
        client_id = conn.execute(
            text(
                "INSERT INTO core.clients (name, industry, billing_model, mgmt_fee) "
                "VALUES ('UAB Aaa', 'food', 'mgmt_fee', 100) RETURNING id"
            )
        ).scalar_one()
        platform_id = conn.execute(
            text(
                "INSERT INTO core.platforms (name, timezone, metric_map) "
                "VALUES ('meta', 'UTC', '{}') RETURNING id"
            )
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO core.ad_accounts "
                "(client_id, platform_id, external_account_id, timezone, run_id) "
                "VALUES (:c, :p, '1001', 'UTC', gen_random_uuid())"
            ),
            {"c": client_id, "p": platform_id},
        )
    return client_id, platform_id


def test_write_findings_fills_the_issue(pipeline_db: PipelineDb) -> None:
    client_id, platform_id = seed_account(pipeline_db)
    run_id = uuid.uuid4()
    assert write_findings(run_id, [finding()], pipeline_db.app) == 1
    with pipeline_db.owner.connect() as conn:
        issue = conn.execute(text("SELECT * FROM app.issues")).mappings().one()
    assert issue["type"] == "quality_missing_days"
    assert (issue["client_id"], issue["platform_id"]) == (client_id, platform_id)
    assert issue["period"] == date(2026, 10, 1)
    assert (issue["entity_type"], issue["entity_id"]) == ("campaign", "c1")
    assert issue["status"] == "open"
    assert issue["run_id"] == run_id
    assert issue["proposed_action"]


def test_unknown_account_issue_has_no_client(pipeline_db: PipelineDb) -> None:
    seed_account(pipeline_db)
    account = UnknownAccount("meta", "9999", date(2026, 5, 1), date(2026, 5, 31))
    findings = check_unknown_accounts(result_with(unknown_accounts=[account]))
    write_findings(uuid.uuid4(), findings, pipeline_db.app)
    with pipeline_db.owner.connect() as conn:
        issue = conn.execute(text("SELECT * FROM app.issues")).mappings().one()
    assert issue["client_id"] is None
    assert issue["platform_id"] is not None
    assert (issue["entity_type"], issue["entity_id"]) == ("ad_account", "9999")


def test_write_findings_twice_gives_one_issue(pipeline_db: PipelineDb) -> None:
    seed_account(pipeline_db)
    write_findings(uuid.uuid4(), [finding()], pipeline_db.app)
    write_findings(uuid.uuid4(), [finding()], pipeline_db.app)
    with pipeline_db.owner.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM app.issues")).scalar_one() == 1


def test_rewrite_keeps_a_human_decision(pipeline_db: PipelineDb) -> None:
    seed_account(pipeline_db)
    write_findings(uuid.uuid4(), [finding()], pipeline_db.app)
    with pipeline_db.owner.begin() as conn:
        conn.execute(text("UPDATE app.issues SET status = 'rejected', decision_note = 'known'"))
    second_run = uuid.uuid4()
    write_findings(second_run, [finding(message="Updated text.")], pipeline_db.app)
    with pipeline_db.owner.connect() as conn:
        issue = conn.execute(text("SELECT * FROM app.issues")).mappings().one()
    assert (issue["status"], issue["decision_note"]) == ("rejected", "known")
    assert (issue["explanation"], issue["run_id"]) == ("Updated text.", second_run)


def test_etl_cannot_write_findings(pipeline_db: PipelineDb) -> None:
    seed_account(pipeline_db)
    with pytest.raises(DBAPIError, match="permission denied"):
        write_findings(uuid.uuid4(), [finding()], pipeline_db.etl)


def test_source_never_mentions_the_answer_key() -> None:
    source = Path(quality.__file__).read_text(encoding="utf-8")
    assert "answer_key" not in source
