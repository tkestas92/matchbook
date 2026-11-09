"""Excel media plans into `core.insertion_orders`, using plans written by the generator."""

import json
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import load_workbook
from sqlalchemy import Engine, text
from tests.conftest import PipelineDb

from generator.documents.excel import write_media_plan
from generator.documents.writer import ensure_parent, plan_path
from generator.world import Client, InsertionOrder, Platform, World
from pipelines import load_plans as load_plans_module
from pipelines.load_plans import (
    campaign_ref_of,
    load_plans,
    load_plans_from_env,
    parse_budget,
    parse_cell_date,
    require_directory,
)
from pipelines.normalize import RowError
from pipelines.reference import load_reference

META = Platform("meta", "Meta", "Europe/Vilnius")
GOOGLE = Platform("google", "Google", "Europe/Vilnius")
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
        {"name": "meta", "timezone": "Europe/Vilnius"},
        {"name": "google", "timezone": "Europe/Vilnius"},
    ],
    "ad_accounts": [
        {
            "client_code": "AAA",
            "platform": "meta",
            "external_account_id": "1001",
            "timezone": "Europe/Vilnius",
        }
    ],
}


def order(
    ref: str = "AAA-META-2605",
    platform: str = "meta",
    budget: str = "1234.56",
    client: str = "AAA",
) -> InsertionOrder:
    return InsertionOrder(
        client, platform, ref, date(2026, 5, 1), date(2026, 5, 31), Decimal(budget)
    )


def write_plan(
    data_dir: Path,
    orders: tuple[InsertionOrder, ...],
    code: str = "AAA",
    platforms: tuple[Platform, ...] = (META, GOOGLE),
) -> Path:
    client = Client(code, f"UAB {code}", "food", "markup", Decimal("10.00"), None)
    world = World(1, (client,), platforms, (), orders, (), ())
    path = ensure_parent(data_dir / plan_path(code))
    write_media_plan(world, client, path)
    return path


@pytest.fixture
def seeded(pipeline_db: PipelineDb, tmp_path: Path) -> PipelineDb:
    path = tmp_path / "reference.json"
    path.write_text(json.dumps(REFERENCE), encoding="utf-8")
    load_reference(uuid.uuid4(), path, pipeline_db.etl)
    return pipeline_db


def order_count(engine: Engine) -> int:
    with engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM core.insertion_orders")).scalar_one()


def seed_campaign(db: PipelineDb, name: str, external_id: str = "c-1") -> int:
    with db.owner.begin() as conn:
        return conn.execute(
            text(
                "INSERT INTO core.campaigns "
                "(platform_id, ad_account_id, platform_campaign_id, name, run_id) "
                "SELECT platform_id, id, :e, :n, :r FROM core.ad_accounts RETURNING id"
            ),
            {"e": external_id, "n": name, "r": uuid.uuid4()},
        ).scalar_one()


def campaign_io(db: PipelineDb, campaign_id: int) -> int | None:
    with db.owner.connect() as conn:
        return conn.execute(
            text("SELECT io_id FROM core.campaigns WHERE id = :i"), {"i": campaign_id}
        ).scalar_one()


def test_load_writes_orders_as_etl(seeded: PipelineDb, tmp_path: Path) -> None:
    run_id = uuid.uuid4()
    write_plan(tmp_path, (order(), order("AAA-GOOGLE-2605", "google", "900.00")))
    result = load_plans(run_id, tmp_path, seeded.etl)
    assert (result.orders_written, result.rejected) == (2, [])
    with seeded.owner.connect() as conn:
        row = conn.execute(
            text(
                "SELECT p.name, io.campaign_ref, io.start_date, io.end_date, io.budget, "
                "io.rate_type, io.run_id FROM core.insertion_orders io "
                "JOIN core.platforms p ON p.id = io.platform_id WHERE p.name = 'meta'"
            )
        ).one()
    assert tuple(row) == (
        "meta",
        "AAA-META-2605",
        date(2026, 5, 1),
        date(2026, 5, 31),
        Decimal("1234.56"),
        None,
        run_id,
    )


def test_load_twice_with_new_run_id_keeps_counts_and_updates_run_id(
    seeded: PipelineDb, tmp_path: Path
) -> None:
    write_plan(tmp_path, (order(),))
    load_plans(uuid.uuid4(), tmp_path, seeded.etl)
    second = uuid.uuid4()
    load_plans(second, tmp_path, seeded.etl)
    assert order_count(seeded.owner) == 1
    with seeded.owner.connect() as conn:
        stored = conn.execute(text("SELECT run_id FROM core.insertion_orders")).scalar_one()
    assert stored == second


def test_changed_budget_updates_the_same_order(seeded: PipelineDb, tmp_path: Path) -> None:
    write_plan(tmp_path, (order(budget="100.00"),))
    load_plans(uuid.uuid4(), tmp_path, seeded.etl)
    write_plan(tmp_path, (order(budget="250.50"),))
    load_plans(uuid.uuid4(), tmp_path, seeded.etl)
    with seeded.owner.connect() as conn:
        budget = conn.execute(text("SELECT budget FROM core.insertion_orders")).scalar_one()
    assert budget == Decimal("250.50")


def test_wrong_header_rejects_the_file(seeded: PipelineDb, tmp_path: Path) -> None:
    path = write_plan(tmp_path, (order(),))
    workbook = load_workbook(path)
    workbook.active["E6"] = "Budget"
    workbook.save(path)
    result = load_plans(uuid.uuid4(), tmp_path, seeded.etl)
    assert [r.path for r in result.rejected] == ["plans/AAA_media_plan.xlsx"]
    assert order_count(seeded.owner) == 0


def test_unknown_client_rejects_only_that_file(seeded: PipelineDb, tmp_path: Path) -> None:
    write_plan(tmp_path, (order(),))
    write_plan(tmp_path, (order("ZZZ-META-2605", client="ZZZ"),), code="ZZZ")
    result = load_plans(uuid.uuid4(), tmp_path, seeded.etl)
    assert result.orders_written == 1
    assert [r.reason for r in result.rejected] == ["Unknown client code 'ZZZ'"]


def test_unknown_platform_rejects_the_whole_file(seeded: PipelineDb, tmp_path: Path) -> None:
    snap = Platform("snapchat", "Snapchat", "UTC")
    orders = (order(), order("AAA-SNAP-2605", "snapchat"))
    write_plan(tmp_path, orders, platforms=(META, snap))
    result = load_plans(uuid.uuid4(), tmp_path, seeded.etl)
    assert [r.reason for r in result.rejected] == ["Unknown platform 'Snapchat'"]
    assert order_count(seeded.owner) == 0


def test_repeated_order_rejects_the_file(seeded: PipelineDb, tmp_path: Path) -> None:
    write_plan(tmp_path, (order(), order()))
    result = load_plans(uuid.uuid4(), tmp_path, seeded.etl)
    assert len(result.rejected) == 1
    assert order_count(seeded.owner) == 0


def test_corrupt_workbook_is_rejected(seeded: PipelineDb, tmp_path: Path) -> None:
    ensure_parent(tmp_path / plan_path("AAA")).write_bytes(b"not a workbook")
    result = load_plans(uuid.uuid4(), tmp_path, seeded.etl)
    assert [r.reason for r in result.rejected] == ["File is not a readable workbook"]


def test_missing_plans_directory_fails(seeded: PipelineDb, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_plans(uuid.uuid4(), tmp_path, seeded.etl)


def test_links_a_tagged_campaign_to_its_order(seeded: PipelineDb, tmp_path: Path) -> None:
    campaign_id = seed_campaign(seeded, "Aaa | Meta | 2026-05 [AAA-META-2605]")
    write_plan(tmp_path, (order(),))
    result = load_plans(uuid.uuid4(), tmp_path, seeded.etl)
    assert (result.campaigns_linked, result.campaigns_unlinked) == (1, 0)
    with seeded.owner.connect() as conn:
        io_id = conn.execute(text("SELECT id FROM core.insertion_orders")).scalar_one()
    assert campaign_io(seeded, campaign_id) == io_id


@pytest.mark.parametrize("name", ["Spring sale", "Other [AAA-META-9999]"])
def test_campaign_without_a_matching_order_stays_unlinked(
    seeded: PipelineDb, tmp_path: Path, name: str
) -> None:
    campaign_id = seed_campaign(seeded, name)
    write_plan(tmp_path, (order(),))
    result = load_plans(uuid.uuid4(), tmp_path, seeded.etl)
    assert (result.campaigns_linked, result.campaigns_unlinked) == (0, 1)
    assert campaign_io(seeded, campaign_id) is None


def test_ref_matching_two_orders_is_not_guessed(seeded: PipelineDb, tmp_path: Path) -> None:
    campaign_id = seed_campaign(seeded, "Aaa [AAA-META-2605]")
    later = InsertionOrder(
        "AAA", "meta", "AAA-META-2605", date(2026, 6, 1), date(2026, 6, 30), Decimal("5.00")
    )
    write_plan(tmp_path, (order(), later))
    result = load_plans(uuid.uuid4(), tmp_path, seeded.etl)
    assert (result.campaigns_linked, result.campaigns_unlinked) == (0, 1)
    assert campaign_io(seeded, campaign_id) is None


def test_load_from_env_uses_the_etl_engine(
    seeded: PipelineDb, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(load_plans_module, "create_etl_engine", lambda: seeded.etl)
    write_plan(tmp_path, (order(),))
    assert load_plans_from_env(uuid.uuid4(), tmp_path).orders_written == 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (7200, "7200.00"),
        (1234.56, "1234.56"),
        (0.1 + 0.2, "0.30"),
        (Decimal("10.005"), "10.01"),
    ],
)
def test_parse_budget_is_exact_decimal(value: object, expected: str) -> None:
    parsed = parse_budget(value)
    assert isinstance(parsed, Decimal)
    assert str(parsed) == expected


@pytest.mark.parametrize("value", [None, "7200", True, float("nan"), 1e12])
def test_parse_budget_rejects_bad_cells(value: object) -> None:
    with pytest.raises(RowError):
        parse_budget(value)


def test_parse_cell_date_accepts_date_cells() -> None:
    assert parse_cell_date(datetime(2026, 5, 1), "x") == date(2026, 5, 1)
    assert parse_cell_date(date(2026, 5, 1), "x") == date(2026, 5, 1)


@pytest.mark.parametrize("value", ["2026-05-01", None, datetime(2026, 5, 1, 12)])
def test_parse_cell_date_rejects_other_cells(value: object) -> None:
    with pytest.raises(RowError):
        parse_cell_date(value, "x")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Aaa | Meta | 2026-05 [AAA-META-2605]", "AAA-META-2605"),
        ("Aaa [AAA-META-2605] ", "AAA-META-2605"),
        ("Aaa [note] renamed", None),
        ("Aaa", None),
    ],
)
def test_campaign_ref_of(name: str, expected: str | None) -> None:
    assert campaign_ref_of(name) == expected


def test_require_directory(tmp_path: Path) -> None:
    require_directory(tmp_path)
    with pytest.raises(FileNotFoundError):
        require_directory(tmp_path / "missing")


def test_source_never_mentions_the_answer_key() -> None:
    source = Path(load_plans_module.__file__).read_text(encoding="utf-8")
    assert "answer_key" not in source
