"""Generated data -> mock APIs -> raw -> core -> findings, in a throwaway database.

Expected values come from the generated delivery files only, never from the planted-error record.
Needs the local Postgres (`make up`) and the role passwords.
"""

import json
import uuid
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from tests.conftest import PipelineDb

from generator.documents.billing import build_documents
from generator.documents.config import load_documents_config
from generator.errors import REFERENCE_PATH, generate_with_errors, load_errors_config
from generator.world import build_world, load_config
from mock_platforms.app import create_app
from mock_platforms.config import MockPlatformsSettings
from pipelines.ingest import IngestSettings, run_ingest
from pipelines.normalize import NormalizeResult, run_normalize
from pipelines.quality import (
    MISSING_DAYS,
    MONTH_BOUNDARY_SHIFT,
    run_quality_checks,
    write_findings,
)
from pipelines.reference import load_reference

TOKEN = "test-token"
TABLES = (
    "core.clients",
    "core.platforms",
    "core.ad_accounts",
    "core.campaigns",
    "core.campaign_aliases",
    "core.daily_delivery",
    "app.issues",
)

CampaignRows = dict[tuple[str, str], list[dict[str, Any]]]


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out_dir = tmp_path_factory.mktemp("generated") / "data"
    world = build_world(load_config())
    docs_config = load_documents_config()
    generate_with_errors(
        world, build_documents(world, docs_config), docs_config, load_errors_config(), out_dir
    )
    return out_dir


@pytest.fixture(scope="module")
def delivery_files(generated: Path) -> list[dict[str, Any]]:
    paths = sorted((generated / "delivery").glob("*/*.json"))
    return [json.loads(path.read_text(encoding="utf-8")) for path in paths]


@pytest.fixture(scope="module")
def campaigns(delivery_files: list[dict[str, Any]]) -> CampaignRows:
    grouped: CampaignRows = defaultdict(list)
    for account in delivery_files:
        for row in account["rows"]:
            grouped[(account["platform"], row["campaign_id"])].append(row)
    return {key: sorted(rows, key=lambda r: r["date"]) for key, rows in grouped.items()}


@pytest.fixture(scope="module")
def ingested(module_pipeline_db: PipelineDb, generated: Path) -> PipelineDb:
    db = module_pipeline_db
    load_reference(uuid.uuid4(), generated / REFERENCE_PATH, db.etl)
    settings = IngestSettings(
        _env_file=None,
        base_url="http://testserver",
        MOCK_PLATFORMS_TOKEN=TOKEN,
        period_start=date(2026, 5, 1),
        period_end=date(2026, 11, 30),
        page_size=500,
    )
    app = create_app(
        MockPlatformsSettings(
            _env_file=None,
            token=TOKEN,
            delivery_dir=generated / "delivery",
            faults_enabled=False,
        )
    )
    with TestClient(app) as client:
        result = run_ingest(uuid.uuid4(), settings, client, db.etl)
    assert result.failures == []
    return db


def table_counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in TABLES}


@pytest.fixture(scope="module")
def first_run(ingested: PipelineDb) -> NormalizeResult:
    result = run_normalize(uuid.uuid4(), ingested.etl)
    write_findings(uuid.uuid4(), run_quality_checks(result), ingested.app)
    return result


def test_nothing_is_rejected_or_unknown(first_run: NormalizeResult) -> None:
    assert first_run.rejected == []
    assert first_run.unknown_accounts == []


def test_row_and_campaign_counts_match_the_files(
    ingested: PipelineDb,
    first_run: NormalizeResult,
    campaigns: CampaignRows,
    delivery_files: list[dict[str, Any]],
) -> None:
    counts = table_counts(ingested.owner)
    assert counts["core.campaigns"] == len(campaigns)
    assert counts["core.daily_delivery"] == sum(len(a["rows"]) for a in delivery_files)
    assert counts["core.ad_accounts"] == len(delivery_files)


def test_campaign_spend_matches_the_files_to_the_cent(
    ingested: PipelineDb, first_run: NormalizeResult, campaigns: CampaignRows
) -> None:
    query = text(
        "SELECT p.name, c.platform_campaign_id, SUM(d.spend) FROM core.daily_delivery d "
        "JOIN core.campaigns c ON c.id = d.campaign_id "
        "JOIN core.platforms p ON p.id = c.platform_id GROUP BY 1, 2"
    )
    with ingested.owner.connect() as conn:
        totals = {(row[0], row[1]): row[2] for row in conn.execute(query)}
    assert totals == {
        key: sum((Decimal(r["spend"]) for r in rows), Decimal("0.00"))
        for key, rows in campaigns.items()
    }


def test_every_multi_name_campaign_has_its_aliases(
    ingested: PipelineDb, first_run: NormalizeResult, campaigns: CampaignRows
) -> None:
    expected = set()
    for (platform, campaign_id), rows in campaigns.items():
        names = list(dict.fromkeys(r["campaign_name"] for r in rows))
        expected |= {(platform, campaign_id, name) for name in names[1:]}
    assert expected
    query = text(
        "SELECT p.name, c.platform_campaign_id, a.alias FROM core.campaign_aliases a "
        "JOIN core.campaigns c ON c.id = a.campaign_id "
        "JOIN core.platforms p ON p.id = c.platform_id"
    )
    with ingested.owner.connect() as conn:
        assert {tuple(row) for row in conn.execute(query)} == expected


def test_every_campaign_keeps_its_earliest_name_and_no_io(
    ingested: PipelineDb, first_run: NormalizeResult, campaigns: CampaignRows
) -> None:
    with ingested.owner.connect() as conn:
        stored = {
            row[0]: (row[1], row[2])
            for row in conn.execute(
                text("SELECT platform_campaign_id, name, io_id FROM core.campaigns")
            )
        }
    for (_, campaign_id), rows in campaigns.items():
        assert stored[campaign_id] == (rows[0]["campaign_name"], None)


def test_gap_findings_match_the_files(first_run: NormalizeResult, campaigns: CampaignRows) -> None:
    expected = set()
    for (platform, campaign_id), rows in campaigns.items():
        days = [date.fromisoformat(r["date"]) for r in rows]
        for before, after in zip(days, days[1:], strict=False):
            if after - before > timedelta(days=1):
                expected.add(
                    (platform, campaign_id, before + timedelta(days=1), after - timedelta(days=1))
                )
    findings = run_quality_checks(first_run)
    gaps = [f for f in findings if f.check in (MISSING_DAYS, MONTH_BOUNDARY_SHIFT)]
    assert {(f.platform, f.campaign_id, f.date_from, f.date_to) for f in gaps} == expected
    assert expected
    assert {f.check for f in gaps} == {MISSING_DAYS, MONTH_BOUNDARY_SHIFT}


def test_running_twice_gives_identical_row_counts(
    ingested: PipelineDb, first_run: NormalizeResult
) -> None:
    before = table_counts(ingested.owner)
    result = run_normalize(uuid.uuid4(), ingested.etl)
    write_findings(uuid.uuid4(), run_quality_checks(result), ingested.app)
    assert table_counts(ingested.owner) == before
    assert before["app.issues"] > 0
