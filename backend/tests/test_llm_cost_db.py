"""`monthly_spend` against a throwaway migrated database (needs DATABASE_URL; owner-run)."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from app.llm.cost import monthly_spend
from sqlalchemy import text
from tests.conftest import PipelineDb

NOW = datetime(2026, 11, 15, 12, tzinfo=UTC)


def insert_run(db: PipelineDb, started_at: datetime, cost: str) -> uuid.UUID:
    with db.owner.begin() as conn:
        return conn.execute(
            text(
                "INSERT INTO app.pipeline_runs (started_at, status, cost_eur) "
                "VALUES (:started_at, 'succeeded', :cost) RETURNING id"
            ),
            {"started_at": started_at, "cost": Decimal(cost)},
        ).scalar_one()


def test_monthly_spend_sums_this_agency_month_without_the_current_run(
    pipeline_db: PipelineDb,
) -> None:
    with pipeline_db.owner.begin() as conn:
        conn.execute(text("TRUNCATE app.pipeline_runs"))
    insert_run(pipeline_db, datetime(2026, 10, 31, 21, 59, tzinfo=UTC), "5")  # 31 Oct in Vilnius
    insert_run(pipeline_db, datetime(2026, 10, 31, 22, 0, tzinfo=UTC), "0.125")  # 1 Nov, 00:00
    insert_run(pipeline_db, datetime(2026, 11, 10, tzinfo=UTC), "0.000001")
    current = insert_run(pipeline_db, datetime(2026, 11, 15, tzinfo=UTC), "0.3")

    spent = monthly_spend(pipeline_db.app, NOW, "Europe/Vilnius", exclude_run_id=current)
    assert spent == Decimal("0.125001")
    assert monthly_spend(pipeline_db.app, NOW, "Europe/Vilnius") == Decimal("0.425001")


def test_monthly_spend_is_zero_without_runs(pipeline_db: PipelineDb) -> None:
    with pipeline_db.owner.begin() as conn:
        conn.execute(text("TRUNCATE app.pipeline_runs"))
    assert monthly_spend(pipeline_db.app, NOW, "Europe/Vilnius") == Decimal(0)
