import pytest
from app.config import get_settings
from app.db.base import Base, one_of
from app.db.models import AuditLog
from app.db.session import get_engine
from sqlalchemy import Column, DateTime, Numeric, UniqueConstraint
from tests.schema_expectations import (
    EXPECTED_TABLES,
    FINANCIAL_TABLES,
    NON_MONEY_NUMERIC_COLUMNS,
    RUN_ID_TABLES,
    UPDATED_AT_TABLES,
)

TABLES = Base.metadata.tables


def all_columns() -> list[Column]:
    return [column for table in TABLES.values() for column in table.columns]


def test_metadata_has_exactly_the_expected_tables() -> None:
    assert set(TABLES) == EXPECTED_TABLES


def test_money_columns_are_numeric_12_2_decimal() -> None:
    for column in all_columns():
        if not isinstance(column.type, Numeric):
            continue
        name = f"{column.table.fullname}.{column.name}"
        assert column.type.asdecimal, name
        if name not in NON_MONEY_NUMERIC_COLUMNS:
            assert (column.type.precision, column.type.scale) == (12, 2), name


def test_cost_eur_keeps_sub_cent_precision() -> None:
    cost = TABLES["app.pipeline_runs"].c.cost_eur.type
    assert (cost.precision, cost.scale) == (12, 6)


def test_timestamps_are_timezone_aware() -> None:
    for column in all_columns():
        if isinstance(column.type, DateTime):
            assert column.type.timezone, f"{column.table.fullname}.{column.name}"


def test_every_table_has_created_at() -> None:
    for name, table in TABLES.items():
        assert "created_at" in table.c, name
        assert table.c.created_at.server_default is not None, name


def test_upserted_tables_have_updated_at() -> None:
    with_updated_at = {name for name, t in TABLES.items() if "updated_at" in t.c}
    assert with_updated_at == UPDATED_AT_TABLES


def test_pipeline_tables_have_non_null_run_id() -> None:
    for name in RUN_ID_TABLES:
        assert TABLES[name].c.run_id.nullable is False, name


def test_run_id_has_no_foreign_key() -> None:
    for name in RUN_ID_TABLES:
        assert not TABLES[name].c.run_id.foreign_keys, name


def test_daily_delivery_is_unique_per_campaign_and_date() -> None:
    uniques = {
        tuple(c.name for c in constraint.columns)
        for constraint in TABLES["core.daily_delivery"].constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("campaign_id", "date") in uniques


def test_eval_schema_has_no_foreign_keys() -> None:
    for name, table in TABLES.items():
        if table.schema == "eval":
            assert not table.foreign_keys, name


def test_financial_foreign_keys_restrict_deletes() -> None:
    for name in FINANCIAL_TABLES:
        for fk in TABLES[name].foreign_keys:
            assert fk.ondelete == "RESTRICT", f"{name}.{fk.parent.name}"


def test_audit_log_has_no_updated_at() -> None:
    assert "updated_at" not in AuditLog.__table__.c


def test_one_of_builds_named_in_check() -> None:
    check = one_of("kind", ("a", "b"))
    assert check.name == "kind"
    assert str(check.sqltext) == "kind IN ('a', 'b')"


def test_get_engine_uses_database_url_and_is_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    get_settings.cache_clear()
    get_engine.cache_clear()
    try:
        engine = get_engine()
        assert engine.url.database == "db"
        assert get_engine() is engine
    finally:
        get_settings.cache_clear()
        get_engine.cache_clear()
