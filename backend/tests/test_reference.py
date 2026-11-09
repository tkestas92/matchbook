"""Reference data: the generator export and its load into `core.*`."""

import json
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from app.config import Settings
from app.db.session import app_database_url
from sqlalchemy import Engine, text
from tests.conftest import PipelineDb

from generator.errors import reference_payload
from generator.world import World, build_world, load_config
from pipelines import reference as reference_module
from pipelines.normalize import METRIC_MAPS
from pipelines.reference import ReferenceDataError, ReferenceResult, load_reference, metric_map_for

TABLES = ("core.clients", "core.platforms", "core.ad_accounts")


@pytest.fixture(scope="module")
def world() -> World:
    return build_world(load_config())


def small_reference() -> dict[str, Any]:
    return {
        "clients": [
            {
                "code": "AAA",
                "name": "UAB Aaa",
                "industry": "food",
                "billing_model": "markup",
                "markup_pct": "12.50",
                "mgmt_fee": None,
            },
            {
                "code": "BBB",
                "name": "UAB Bbb",
                "industry": "energy",
                "billing_model": "mgmt_fee",
                "markup_pct": None,
                "mgmt_fee": "900.00",
            },
        ],
        "platforms": [{"name": "meta", "timezone": "Europe/Vilnius"}],
        "ad_accounts": [
            {
                "client_code": "BBB",
                "platform": "meta",
                "external_account_id": "1001",
                "timezone": "Europe/Vilnius",
            }
        ],
    }


def write_reference(tmp_path: Path, payload: dict[str, Any]) -> Path:
    path = tmp_path / "reference.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in TABLES}


def test_payload_lists_every_world_entity(world: World) -> None:
    payload = reference_payload(world)
    assert len(payload["clients"]) == len(world.clients)
    assert [p["name"] for p in payload["platforms"]] == [p.key for p in world.platforms]
    assert {a["external_account_id"] for a in payload["ad_accounts"]} == {
        a.external_account_id for a in world.ad_accounts
    }


def test_payload_keeps_money_as_strings(world: World) -> None:
    client = next(c for c in reference_payload(world)["clients"] if c["markup_pct"] is not None)
    assert isinstance(client["markup_pct"], str)


def test_payload_has_only_registry_fields(world: World) -> None:
    payload = reference_payload(world)
    assert set(payload) == {"clients", "platforms", "ad_accounts"}
    assert set(payload["ad_accounts"][0]) == {
        "client_code",
        "platform",
        "external_account_id",
        "timezone",
    }


def test_load_writes_rows_as_etl(pipeline_db: PipelineDb, tmp_path: Path) -> None:
    run_id = uuid.uuid4()
    result = load_reference(run_id, write_reference(tmp_path, small_reference()), pipeline_db.etl)
    assert result == ReferenceResult(clients=2, platforms=1, ad_accounts=1)
    with pipeline_db.owner.connect() as conn:
        account = conn.execute(
            text(
                "SELECT c.name, a.timezone, a.run_id FROM core.ad_accounts a "
                "JOIN core.clients c ON c.id = a.client_id"
            )
        ).one()
        metric_map = conn.execute(text("SELECT metric_map FROM core.platforms")).scalar_one()
    assert tuple(account) == ("UAB Bbb", "Europe/Vilnius", run_id)
    assert metric_map == asdict(METRIC_MAPS["meta"])


def test_load_twice_gives_identical_counts(pipeline_db: PipelineDb, tmp_path: Path) -> None:
    path = write_reference(tmp_path, small_reference())
    load_reference(uuid.uuid4(), path, pipeline_db.etl)
    first = counts(pipeline_db.owner)
    load_reference(uuid.uuid4(), path, pipeline_db.etl)
    assert counts(pipeline_db.owner) == first == dict(zip(TABLES, (2, 1, 1), strict=True))


def test_load_updates_a_changed_account_owner(pipeline_db: PipelineDb, tmp_path: Path) -> None:
    payload = small_reference()
    load_reference(uuid.uuid4(), write_reference(tmp_path, payload), pipeline_db.etl)
    payload["ad_accounts"][0]["client_code"] = "AAA"
    load_reference(uuid.uuid4(), write_reference(tmp_path, payload), pipeline_db.etl)
    with pipeline_db.owner.connect() as conn:
        owner = conn.execute(
            text("SELECT c.name FROM core.ad_accounts a JOIN core.clients c ON c.id = a.client_id")
        ).scalar_one()
    assert owner == "UAB Aaa"


def test_platform_without_metric_map_fails() -> None:
    with pytest.raises(ReferenceDataError, match="snapchat"):
        metric_map_for("snapchat")


def test_account_with_unknown_client_fails_and_writes_nothing(
    pipeline_db: PipelineDb, tmp_path: Path
) -> None:
    payload = small_reference()
    payload["ad_accounts"][0]["client_code"] = "ZZZ"
    with pytest.raises(ReferenceDataError, match="ZZZ"):
        load_reference(uuid.uuid4(), write_reference(tmp_path, payload), pipeline_db.etl)
    assert counts(pipeline_db.owner) == dict.fromkeys(TABLES, 0)


def test_app_url_swaps_the_owner_credentials() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://owner:secret@localhost:5432/matchbook",
        app_db_password="app-password",
    )
    url = app_database_url(settings)
    assert (url.username, url.password) == ("app", "app-password")


def test_source_never_mentions_the_answer_key() -> None:
    source = Path(reference_module.__file__).read_text(encoding="utf-8")
    assert "answer_key" not in source
