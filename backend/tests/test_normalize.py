"""Field mapping, snapshot selection and upserts of the normalize step."""

import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from app.db.models.raw import GoogleResponse, MetaResponse, TiktokResponse
from sqlalchemy import Engine, insert, text
from tests.conftest import PipelineDb

from pipelines import normalize
from pipelines.normalize import (
    DUPLICATE_ROW,
    FOREIGN_CAMPAIGN,
    METRIC_MAPS,
    DeliveryRecord,
    RejectedRow,
    Snapshot,
    UnknownAccount,
    account_records,
    campaign_names,
    keep_one_account_per_campaign,
    parse_row,
    run_normalize,
)
from pipelines.quality import MISSING_DAYS, run_quality_checks
from pipelines.reference import load_reference

MODELS = {"meta": MetaResponse, "google": GoogleResponse, "tiktok": TiktokResponse}
DAY = date(2026, 10, 15)
DATES = {"meta": "2026-10-15", "google": "20261015", "tiktok": "2026-10-15 00:00:00"}
TZ = "Europe/Vilnius"
FETCHED = datetime(2026, 11, 1, 3, tzinfo=UTC)
LATER = datetime(2026, 11, 2, 3, tzinfo=UTC)
OCTOBER = (date(2026, 10, 1), date(2026, 10, 31))
TAGGED = "Client | Meta | 2026-10 [AAA-META-2610]"
RENAMED = "Client Meta – Promo October"


def api_row(platform: str = "meta", **overrides: Any) -> dict[str, Any]:
    spend: dict[str, Any] = {
        "meta": {"spend": "123.45"},
        "google": {"cost_micros": 123_450_000},
        "tiktok": {"cost": "123.45"},
    }[platform]
    return {
        "campaign_id": "c1",
        "campaign_name": TAGGED,
        "date": DATES[platform],
        "impressions": 1000,
        "clicks": 20,
        "conversions": 3,
        **spend,
        **overrides,
    }


def parse(platform: str, raw: Any) -> DeliveryRecord | RejectedRow:
    return parse_row(platform, "1001", TZ, raw)


def parsed(platform: str, **overrides: Any) -> DeliveryRecord:
    record = parse(platform, api_row(platform, **overrides))
    assert isinstance(record, DeliveryRecord), record
    return record


def rejected(platform: str, **overrides: Any) -> RejectedRow:
    record = parse(platform, api_row(platform, **overrides))
    assert isinstance(record, RejectedRow), record
    return record


@pytest.mark.parametrize("platform", ["meta", "google", "tiktok"])
def test_every_field_maps_to_the_shared_schema(platform: str) -> None:
    assert parsed(platform) == DeliveryRecord(
        platform=platform,
        account_id="1001",
        timezone=TZ,
        campaign_id="c1",
        campaign_name=TAGGED,
        date=DAY,
        spend=Decimal("123.45"),
        impressions=1000,
        clicks=20,
        conversions=3,
    )


def test_each_platform_names_its_own_spend_field() -> None:
    assert {name: mapping.spend for name, mapping in METRIC_MAPS.items()} == {
        "meta": "spend",
        "google": "cost_micros",
        "tiktok": "cost",
    }


@pytest.mark.parametrize(
    ("micros", "euros"),
    [
        (1_000_000, "1.00"),
        (1, "0.00"),
        (123_450_000, "123.45"),
        (1_005_000, "1.01"),
        (2_675_000, "2.68"),
        (4_999_999, "5.00"),
        (9_999_999_999_990_000, "9999999999.99"),
        (-1_005_000, "-1.01"),
        (0, "0.00"),
    ],
)
def test_micros_convert_exactly(micros: int, euros: str) -> None:
    spend = parsed("google", cost_micros=micros).spend
    assert isinstance(spend, Decimal)
    assert str(spend) == euros


def test_decimal_strings_round_half_up_to_cents() -> None:
    assert parsed("meta", spend="2.675").spend == Decimal("2.68")


@pytest.mark.parametrize(
    "value", ["NaN", "Infinity", "1e3", "1_000", "", "abc", " 1.00", 12.5, 12, True]
)
def test_bad_decimal_spend_is_rejected(value: Any) -> None:
    assert "spend" in rejected("meta", spend=value).reason


@pytest.mark.parametrize("value", ["123450000", 123.45, True])
def test_non_integer_micros_are_rejected(value: Any) -> None:
    assert "cost_micros" in rejected("google", cost_micros=value).reason


@pytest.mark.parametrize(
    ("platform", "field", "value"),
    [
        ("meta", "spend", "10000000000.00"),
        ("google", "cost_micros", 10_000_000_000_000_000),
        ("tiktok", "cost", "9" * 40),
    ],
)
def test_spend_too_large_for_the_column_is_rejected(platform: str, field: str, value: Any) -> None:
    assert "too large" in rejected(platform, **{field: value}).reason


@pytest.mark.parametrize(
    ("platform", "value"),
    [
        ("meta", "20261015"),
        ("meta", "2026-5-1"),
        ("google", "2026-10-15"),
        ("google", "2026101"),
        ("tiktok", "2026-10-15"),
        ("tiktok", "2026-10-15 13:00:00"),
        ("meta", None),
        ("meta", 20261015),
    ],
)
def test_bad_dates_are_rejected_without_a_day(platform: str, value: Any) -> None:
    assert rejected(platform, date=value).date is None


def test_a_bad_field_keeps_the_rows_date() -> None:
    assert rejected("meta", clicks="20").date == DAY


@pytest.mark.parametrize("field", ["campaign_id", "campaign_name"])
@pytest.mark.parametrize("value", [None, "", 111])
def test_bad_campaign_identity_is_rejected(field: str, value: Any) -> None:
    assert field in rejected("meta", **{field: value}).reason


@pytest.mark.parametrize("field", ["impressions", "clicks", "conversions"])
def test_counts_must_be_integers(field: str) -> None:
    assert field in rejected("meta", **{field: "20"}).reason
    assert field in rejected("meta", **{field: 2**63}).reason


def test_row_that_is_not_an_object_is_rejected() -> None:
    record = parse("meta", ["not", "a", "row"])
    assert isinstance(record, RejectedRow)


@pytest.mark.parametrize("field", ["spend", "impressions", "clicks", "conversions"])
def test_null_metric_stays_none_and_is_incomplete(field: str) -> None:
    record = parsed("meta", **{field: None})
    assert getattr(record, field) is None
    assert not record.is_complete


def test_absent_metric_is_treated_as_null() -> None:
    raw = api_row("meta")
    del raw["clicks"]
    record = parse("meta", raw)
    assert isinstance(record, DeliveryRecord)
    assert record.clicks is None


def test_negative_values_are_kept_for_the_quality_checks() -> None:
    record = parsed("meta", spend="-5.00", clicks=-1)
    assert (record.spend, record.clicks) == (Decimal("-5.00"), -1)
    assert record.is_complete


def snapshot(
    rows: list[dict[str, Any]],
    fetched_at: datetime = FETCHED,
    period: tuple[date, date] = OCTOBER,
) -> Snapshot:
    return Snapshot("meta", "1001", period[0], period[1], fetched_at, tuple(rows))


def oct_row(day: int, spend: str = "10.00", **overrides: Any) -> dict[str, Any]:
    return api_row("meta", date=date(2026, 10, day).isoformat(), spend=spend, **overrides)


def test_overlapping_snapshots_use_the_newest_values() -> None:
    older = snapshot([oct_row(1, "1.00"), oct_row(2, "2.00")])
    newer = snapshot([oct_row(2, "9.00")], LATER, (date(2026, 10, 2), date(2026, 10, 2)))
    records, _ = account_records([older, newer], TZ)
    assert {r.date.day: r.spend for r in records} == {1: Decimal("1.00"), 2: Decimal("9.00")}


def test_a_day_the_newer_snapshot_omits_stays_missing() -> None:
    older = snapshot([oct_row(1), oct_row(2), oct_row(3)], FETCHED)
    newer = snapshot([oct_row(1), oct_row(3)], LATER)
    records, _ = account_records([older, newer], TZ)
    assert sorted(r.date.day for r in records) == [1, 3]


def test_non_overlapping_snapshots_are_both_kept() -> None:
    first = snapshot([oct_row(1)], FETCHED, (date(2026, 10, 1), date(2026, 10, 1)))
    second = snapshot([oct_row(2)], LATER, (date(2026, 10, 2), date(2026, 10, 2)))
    records, _ = account_records([first, second], TZ)
    assert sorted(r.date.day for r in records) == [1, 2]


def test_rejected_row_in_a_superseded_period_is_ignored() -> None:
    older = snapshot([oct_row(1, "bad")], FETCHED)
    newer = snapshot([oct_row(1)], LATER)
    records, bad = account_records([older, newer], TZ)
    assert len(records) == 1
    assert bad == []


def test_duplicate_row_keeps_the_last_and_is_reported() -> None:
    records, bad = account_records([snapshot([oct_row(1, "1.00"), oct_row(1, "2.00")])], TZ)
    assert [r.spend for r in records] == [Decimal("2.00")]
    assert [row.reason for row in bad] == [DUPLICATE_ROW]


def test_second_account_cannot_claim_a_campaign() -> None:
    first = parse_row("meta", "1001", TZ, oct_row(1))
    second = parse_row("meta", "1002", TZ, oct_row(2))
    assert isinstance(first, DeliveryRecord) and isinstance(second, DeliveryRecord)
    kept, bad = keep_one_account_per_campaign([first, second])
    assert kept == [first]
    assert [(row.account_id, row.reason) for row in bad] == [("1002", FOREIGN_CAMPAIGN)]


def test_campaign_names_start_with_the_earliest_reported() -> None:
    late = parse_row("meta", "1001", TZ, oct_row(20, campaign_name=RENAMED))
    early = parse_row("meta", "1001", TZ, oct_row(1, campaign_name=TAGGED))
    assert isinstance(late, DeliveryRecord) and isinstance(early, DeliveryRecord)
    assert campaign_names([late, early]) == {("meta", "c1"): [TAGGED, RENAMED]}


REFERENCE = {
    "clients": [
        {
            "code": "AAA",
            "name": "UAB Aaa",
            "industry": "food",
            "billing_model": "markup",
            "markup_pct": "10.00",
            "mgmt_fee": None,
        }
    ],
    "platforms": [
        {"name": "meta", "timezone": TZ},
        {"name": "google", "timezone": TZ},
        {"name": "tiktok", "timezone": "UTC"},
    ],
    "ad_accounts": [
        {
            "client_code": "AAA",
            "platform": "meta",
            "external_account_id": "1001",
            "timezone": TZ,
        },
        {
            "client_code": "AAA",
            "platform": "google",
            "external_account_id": "2001",
            "timezone": TZ,
        },
        {
            "client_code": "AAA",
            "platform": "tiktok",
            "external_account_id": "3001",
            "timezone": "UTC",
        },
    ],
}
BODIES = {
    "meta": lambda rows: {"data": rows, "paging": {"cursors": {}}},
    "google": lambda rows: {"results": rows},
    "tiktok": lambda rows: {"code": 0, "data": {"list": rows, "page_info": {}}},
}
COUNTED = ("core.campaigns", "core.campaign_aliases", "core.daily_delivery")


@pytest.fixture
def seeded(pipeline_db: PipelineDb, tmp_path: Path) -> PipelineDb:
    path = tmp_path / "reference.json"
    path.write_text(json.dumps(REFERENCE), encoding="utf-8")
    load_reference(uuid.uuid4(), path, pipeline_db.etl)
    return pipeline_db


def store_raw(
    db: PipelineDb,
    platform: str,
    account_id: str,
    pages: list[list[dict[str, Any]]],
    period: tuple[date, date] = OCTOBER,
    fetched_at: datetime = FETCHED,
) -> None:
    with db.etl.begin() as conn:
        for number, rows in enumerate(pages, start=1):
            conn.execute(
                insert(MODELS[platform]).values(
                    run_id=uuid.uuid4(),
                    account_id=account_id,
                    period_start=period[0],
                    period_end=period[1],
                    page=number,
                    fetched_at=fetched_at,
                    payload=BODIES[platform](rows),
                    body=b"{}",
                )
            )


def counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in COUNTED}


def snapshot_of_table(engine: Engine, table: str, columns: str) -> list[tuple[Any, ...]]:
    with engine.connect() as conn:
        rows = conn.execute(text(f"SELECT {columns} FROM {table} ORDER BY {columns}"))
        return [tuple(row) for row in rows]


def run_ids(engine: Engine, table: str) -> set[uuid.UUID]:
    with engine.connect() as conn:
        return set(conn.execute(text(f"SELECT DISTINCT run_id FROM {table}")).scalars())


def test_loads_all_three_platforms_as_etl(seeded: PipelineDb) -> None:
    store_raw(seeded, "meta", "1001", [[api_row("meta", campaign_id="m")]])
    store_raw(seeded, "google", "2001", [[api_row("google", campaign_id="g")]])
    store_raw(seeded, "tiktok", "3001", [[api_row("tiktok", campaign_id="t")]])
    result = run_normalize(uuid.uuid4(), seeded.etl)
    assert (result.campaigns_written, result.delivery_written) == (3, 3)
    assert (
        snapshot_of_table(seeded.owner, "core.daily_delivery", "date, spend, clicks")
        == [(DAY, Decimal("123.45"), 20)] * 3
    )


def test_every_written_row_carries_the_run_id(seeded: PipelineDb) -> None:
    store_raw(seeded, "meta", "1001", [[oct_row(1), oct_row(2, campaign_name=RENAMED)]])
    run_id = uuid.uuid4()
    run_normalize(run_id, seeded.etl)
    for table in COUNTED:
        assert run_ids(seeded.owner, table) == {run_id}


def test_pages_of_a_snapshot_are_combined(seeded: PipelineDb) -> None:
    store_raw(seeded, "meta", "1001", [[oct_row(1)], [oct_row(2)]])
    assert run_normalize(uuid.uuid4(), seeded.etl).delivery_written == 2


def test_running_twice_gives_identical_rows(seeded: PipelineDb) -> None:
    store_raw(seeded, "meta", "1001", [[oct_row(1), oct_row(2, campaign_name=RENAMED)]])
    run_normalize(uuid.uuid4(), seeded.etl)
    first_counts = counts(seeded.owner)
    columns = "campaign_id, date, spend, impressions, clicks, conversions, id"
    first_rows = snapshot_of_table(seeded.owner, "core.daily_delivery", columns)
    second_run = uuid.uuid4()
    run_normalize(second_run, seeded.etl)
    assert counts(seeded.owner) == first_counts == dict(zip(COUNTED, (1, 1, 2), strict=True))
    assert snapshot_of_table(seeded.owner, "core.daily_delivery", columns) == first_rows
    assert run_ids(seeded.owner, "core.daily_delivery") == {second_run}


def test_changed_values_update_in_place(seeded: PipelineDb) -> None:
    store_raw(seeded, "meta", "1001", [[oct_row(1, "1.00")]])
    run_normalize(uuid.uuid4(), seeded.etl)
    with seeded.owner.begin() as conn:
        conn.execute(text("DELETE FROM raw.meta_responses"))
    store_raw(seeded, "meta", "1001", [[oct_row(1, "2.00")]])
    run_normalize(uuid.uuid4(), seeded.etl)
    assert snapshot_of_table(seeded.owner, "core.daily_delivery", "date, spend") == [
        (date(2026, 10, 1), Decimal("2.00"))
    ]


def test_renamed_campaign_gives_one_campaign_and_an_alias(seeded: PipelineDb) -> None:
    rows = [oct_row(1), oct_row(2), oct_row(3, campaign_name=RENAMED)]
    store_raw(seeded, "meta", "1001", [rows])
    result = run_normalize(uuid.uuid4(), seeded.etl)
    assert result.aliases_written == 1
    assert snapshot_of_table(seeded.owner, "core.campaigns", "platform_campaign_id, name") == [
        ("c1", TAGGED)
    ]
    assert snapshot_of_table(
        seeded.owner, "core.campaign_aliases", "alias, source, confidence"
    ) == [(RENAMED, "rule", Decimal("1.000"))]


def test_normalize_leaves_io_id_empty(seeded: PipelineDb) -> None:
    store_raw(seeded, "meta", "1001", [[oct_row(1)]])
    run_normalize(uuid.uuid4(), seeded.etl)
    assert snapshot_of_table(seeded.owner, "core.campaigns", "io_id, name") == [(None, TAGGED)]


def test_io_link_survives_a_rerun(seeded: PipelineDb) -> None:
    store_raw(seeded, "meta", "1001", [[oct_row(1)]])
    run_normalize(uuid.uuid4(), seeded.etl)
    with seeded.owner.begin() as conn:
        io_id = conn.execute(
            text(
                "INSERT INTO core.insertion_orders (client_id, platform_id, campaign_ref, "
                "start_date, end_date, budget, run_id) "
                "SELECT a.client_id, a.platform_id, 'REF', '2026-10-01', '2026-10-31', 100, "
                "gen_random_uuid() FROM core.ad_accounts a LIMIT 1 RETURNING id"
            )
        ).scalar_one()
        conn.execute(text("UPDATE core.campaigns SET io_id = :i"), {"i": io_id})
    run_normalize(uuid.uuid4(), seeded.etl)
    assert snapshot_of_table(seeded.owner, "core.campaigns", "io_id, name") == [(io_id, TAGGED)]


def test_human_alias_keeps_its_source(seeded: PipelineDb) -> None:
    store_raw(seeded, "meta", "1001", [[oct_row(1), oct_row(2, campaign_name=RENAMED)]])
    run_normalize(uuid.uuid4(), seeded.etl)
    with seeded.owner.begin() as conn:
        conn.execute(text("UPDATE core.campaign_aliases SET source = 'human'"))
    run_normalize(uuid.uuid4(), seeded.etl)
    assert snapshot_of_table(seeded.owner, "core.campaign_aliases", "source, alias") == [
        ("human", RENAMED)
    ]


def test_unknown_account_is_reported_and_not_loaded(seeded: PipelineDb) -> None:
    store_raw(seeded, "meta", "9999", [[oct_row(1)]])
    result = run_normalize(uuid.uuid4(), seeded.etl)
    assert result.unknown_accounts == [UnknownAccount("meta", "9999", *OCTOBER)]
    assert counts(seeded.owner) == dict.fromkeys(COUNTED, 0)


def test_null_row_is_not_stored_but_reaches_quality(seeded: PipelineDb) -> None:
    store_raw(seeded, "meta", "1001", [[oct_row(1), oct_row(2, clicks=None), oct_row(3)]])
    result = run_normalize(uuid.uuid4(), seeded.etl)
    assert result.delivery_written == 2
    checks = {f.check for f in run_quality_checks(result)}
    assert "null_values" in checks
    assert MISSING_DAYS not in checks


def test_negative_value_is_stored(seeded: PipelineDb) -> None:
    store_raw(seeded, "meta", "1001", [[oct_row(1, "-5.00")]])
    assert run_normalize(uuid.uuid4(), seeded.etl).delivery_written == 1
    assert snapshot_of_table(seeded.owner, "core.daily_delivery", "date, spend") == [
        (date(2026, 10, 1), Decimal("-5.00"))
    ]


def test_bad_row_does_not_stop_the_rest(seeded: PipelineDb) -> None:
    store_raw(seeded, "meta", "1001", [[oct_row(1, "abc"), oct_row(2)]])
    result = run_normalize(uuid.uuid4(), seeded.etl)
    assert result.delivery_written == 1
    assert len(result.rejected) == 1


def test_no_raw_data_writes_nothing(seeded: PipelineDb) -> None:
    result = run_normalize(uuid.uuid4(), seeded.etl)
    assert (result.campaigns_written, result.delivery_written) == (0, 0)


def test_source_never_mentions_the_answer_key() -> None:
    source = Path(normalize.__file__).read_text(encoding="utf-8")
    assert "answer_key" not in source
