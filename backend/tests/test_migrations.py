"""Run the Alembic migrations on a throwaway empty database and inspect the result.

Needs the local Postgres (`make up`); skipped when it is unreachable.
"""

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, inspect, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from tests.schema_expectations import (
    EXPECTED_SCHEMAS,
    EXPECTED_TABLES,
    UPDATED_AT_TABLES,
)

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


@contextmanager
def temporary_database(database_url: str) -> Iterator[URL]:
    url = make_url(database_url)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    name = f"matchbook_test_{uuid.uuid4().hex[:12]}"
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except OperationalError as exc:
        admin.dispose()
        pytest.skip(f"Postgres is not reachable: {exc.orig}")
    try:
        yield url.set(database=name)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def alembic_config(url: URL) -> Config:
    config = Config(str(ALEMBIC_INI))
    rendered = url.render_as_string(hide_password=False)
    config.set_main_option("sqlalchemy.url", rendered.replace("%", "%%"))
    return config


def existing_tables(engine: Engine) -> set[str]:
    inspector = inspect(engine)
    return {
        f"{schema}.{table}"
        for schema in EXPECTED_SCHEMAS
        if schema in inspector.get_schema_names()
        for table in inspector.get_table_names(schema=schema)
    }


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    with temporary_database(database_url) as url:
        command.upgrade(alembic_config(url), "head")
        engine = create_engine(url)
        yield engine
        engine.dispose()


@pytest.fixture
def conn(engine: Engine) -> Iterator[Connection]:
    """A connection whose writes are rolled back after the test."""
    with engine.connect() as connection:
        transaction = connection.begin()
        yield connection
        if transaction.is_active:
            transaction.rollback()


def insert(conn: Connection, sql: str, **params: object) -> int:
    return conn.execute(text(sql), params).scalar_one()


def seed_campaign(conn: Connection) -> dict[str, object]:
    """Minimal client → platform → ad account → campaign chain."""
    run_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:8]
    client_id = insert(
        conn,
        "INSERT INTO core.clients (code, name, industry, billing_model, markup_pct) "
        "VALUES (:code, :name, 'retail', 'markup', 15) RETURNING id",
        code=suffix,
        name=f"client-{suffix}",
    )
    platform_id = insert(
        conn,
        "INSERT INTO core.platforms (name, timezone, metric_map) "
        "VALUES (:name, 'Europe/Vilnius', '{}') RETURNING id",
        name=f"platform-{suffix}",
    )
    account_id = insert(
        conn,
        "INSERT INTO core.ad_accounts "
        "(client_id, platform_id, external_account_id, timezone, run_id) "
        "VALUES (:c, :p, 'acc-1', 'Europe/Vilnius', :r) RETURNING id",
        c=client_id,
        p=platform_id,
        r=run_id,
    )
    campaign_id = insert(
        conn,
        "INSERT INTO core.campaigns "
        "(platform_id, ad_account_id, platform_campaign_id, name, run_id) "
        "VALUES (:p, :a, 'cmp-1', 'Spring sale', :r) RETURNING id",
        p=platform_id,
        a=account_id,
        r=run_id,
    )
    return {
        "run_id": run_id,
        "client_id": client_id,
        "platform_id": platform_id,
        "campaign_id": campaign_id,
    }


def insert_platform_invoice(conn: Connection, seed: dict[str, object], number: str) -> int:
    return insert(
        conn,
        "INSERT INTO core.invoices "
        "(kind, vendor, number, platform_id, period_start, period_end, total, run_id) "
        "VALUES ('platform', 'Meta', :n, :p, '2026-10-01', '2026-10-31', 100, :r) "
        "RETURNING id",
        n=number,
        p=seed["platform_id"],
        r=seed["run_id"],
    )


def test_all_schemas_exist(engine: Engine) -> None:
    assert EXPECTED_SCHEMAS <= set(inspect(engine).get_schema_names())


def test_all_tables_exist(engine: Engine) -> None:
    assert existing_tables(engine) == EXPECTED_TABLES


def test_expected_indexes_exist(engine: Engine) -> None:
    inspector = inspect(engine)

    def indexed(schema: str, table: str) -> set[tuple[str, ...]]:
        indexes = inspector.get_indexes(table, schema=schema)
        uniques = inspector.get_unique_constraints(table, schema=schema)
        return {tuple(i["column_names"]) for i in indexes} | {
            tuple(u["column_names"]) for u in uniques
        }

    assert ("date",) in indexed("core", "daily_delivery")
    assert ("campaign_id", "date") in indexed("core", "daily_delivery")
    assert ("period_start", "period_end") in indexed("core", "invoices")
    assert ("status",) in indexed("app", "issues")


def test_updated_at_trigger_on_every_upserted_table(conn: Connection) -> None:
    rows = conn.execute(
        text(
            "SELECT DISTINCT event_object_schema || '.' || event_object_table "
            "FROM information_schema.triggers WHERE trigger_name = 'set_updated_at'"
        )
    ).scalars()
    assert set(rows) == UPDATED_AT_TABLES


def test_daily_delivery_rejects_duplicate_campaign_and_date(conn: Connection) -> None:
    seed = seed_campaign(conn)
    sql = (
        "INSERT INTO core.daily_delivery "
        "(campaign_id, date, spend, impressions, clicks, conversions, run_id) "
        "VALUES (:c, '2026-10-15', 12.34, 1000, 10, 1, :r)"
    )
    conn.execute(text(sql), {"c": seed["campaign_id"], "r": seed["run_id"]})
    with pytest.raises(IntegrityError, match="uq_daily_delivery_campaign_id_date"):
        conn.execute(text(sql), {"c": seed["campaign_id"], "r": seed["run_id"]})


def test_money_round_trips_as_exact_decimal(conn: Connection) -> None:
    seed = seed_campaign(conn)
    invoice_id = insert_platform_invoice(conn, seed, "INV-DEC")
    conn.execute(
        text("UPDATE core.invoices SET total = 0.1 + 0.2 WHERE id = :id"),
        {"id": invoice_id},
    )
    total = conn.execute(
        text("SELECT total FROM core.invoices WHERE id = :id"), {"id": invoice_id}
    ).scalar_one()
    assert str(total) == "0.30"


@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "TRUNCATE"])
def test_audit_log_is_insert_only(conn: Connection, operation: str) -> None:
    conn.execute(
        text(
            "INSERT INTO app.audit_log (actor, action, entity) "
            "VALUES ('pipeline', 'create', 'invoice')"
        )
    )
    statement = {
        "UPDATE": "UPDATE app.audit_log SET action = 'tampered'",
        "DELETE": "DELETE FROM app.audit_log",
        "TRUNCATE": "TRUNCATE app.audit_log",
    }[operation]
    with pytest.raises(DBAPIError, match="insert-only"):
        conn.execute(text(statement))


def test_platform_invoice_requires_platform_id(conn: Connection) -> None:
    with pytest.raises(IntegrityError, match="ck_invoices_kind_requires_party"):
        conn.execute(
            text(
                "INSERT INTO core.invoices "
                "(kind, vendor, number, period_start, period_end, total, run_id) "
                "VALUES ('platform', 'Meta', 'X-1', '2026-10-01', '2026-10-31', "
                "1, :r)"
            ),
            {"r": uuid.uuid4()},
        )


def test_client_invoice_requires_client_id(conn: Connection) -> None:
    seed = seed_campaign(conn)
    with pytest.raises(IntegrityError, match="ck_invoices_kind_requires_party"):
        conn.execute(
            text(
                "INSERT INTO core.invoices (kind, vendor, number, platform_id, "
                "period_start, period_end, total, run_id) "
                "VALUES ('client', 'Agency', 'C-1', :p, '2026-10-01', "
                "'2026-10-31', 1, :r)"
            ),
            {"p": seed["platform_id"], "r": seed["run_id"]},
        )


def test_invoice_number_is_unique_per_vendor(conn: Connection) -> None:
    seed = seed_campaign(conn)
    insert_platform_invoice(conn, seed, "INV-1")
    with pytest.raises(IntegrityError, match="uq_invoices_vendor_number"):
        insert_platform_invoice(conn, seed, "INV-1")


def test_invoice_with_lines_cannot_be_deleted(conn: Connection) -> None:
    seed = seed_campaign(conn)
    invoice_id = insert_platform_invoice(conn, seed, "INV-2")
    conn.execute(
        text(
            "INSERT INTO core.invoice_lines (invoice_id, line_no, amount, run_id) "
            "VALUES (:i, 1, 100, :r)"
        ),
        {"i": invoice_id, "r": seed["run_id"]},
    )
    with pytest.raises(IntegrityError, match="fk_invoice_lines_invoice_id_invoices"):
        conn.execute(text("DELETE FROM core.invoices WHERE id = :i"), {"i": invoice_id})


def test_updated_at_advances_on_update(engine: Engine) -> None:
    name = f"client-{uuid.uuid4().hex[:8]}"
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO core.clients (code, name, industry, billing_model) "
                "VALUES (:n, :n, 'retail', 'client_owned')"
            ),
            {"n": name},
        )
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE core.clients SET industry = 'travel' WHERE name = :n"),
            {"n": name},
        )
        created_at, updated_at = conn.execute(
            text("SELECT created_at, updated_at FROM core.clients WHERE name = :n"),
            {"n": name},
        ).one()
    assert updated_at > created_at


def test_downgrade_then_upgrade_round_trips(database_url: str) -> None:
    with temporary_database(database_url) as url:
        config = alembic_config(url)
        engine = create_engine(url)
        try:
            command.upgrade(config, "head")
            command.downgrade(config, "base")
            assert not EXPECTED_SCHEMAS & set(inspect(engine).get_schema_names())
            command.upgrade(config, "head")
            assert existing_tables(engine) == EXPECTED_TABLES
        finally:
            engine.dispose()


DEMO_VIEWS = {"v_issues", "v_pipeline_runs"}


def demo_state(engine: Engine) -> tuple[set[str], list[str]]:
    """Views in schema `demo` and the `demo_ro` settings in this database."""
    inspector = inspect(engine)
    views = (
        set(inspector.get_view_names(schema="demo"))
        if "demo" in inspector.get_schema_names()
        else set()
    )
    with engine.connect() as conn:
        settings = conn.execute(
            text(
                "SELECT unnest(s.setconfig) FROM pg_db_role_setting s "
                "JOIN pg_roles r ON r.oid = s.setrole "
                "JOIN pg_database d ON d.oid = s.setdatabase "
                "WHERE r.rolname = 'demo_ro' AND d.datname = current_database()"
            )
        ).scalars()
        return views, sorted(settings)


def test_demo_migration_downgrades_and_upgrades_again(database_url: str) -> None:
    expected_settings = ["default_transaction_read_only=on", "statement_timeout=3s"]
    with temporary_database(database_url) as url:
        config = alembic_config(url)
        engine = create_engine(url)
        try:
            command.upgrade(config, "head")
            assert demo_state(engine) == (DEMO_VIEWS, expected_settings)
            command.downgrade(config, "0004")
            assert "demo" not in inspect(engine).get_schema_names()
            assert demo_state(engine) == (set(), [])
            assert existing_tables(engine) == EXPECTED_TABLES
            command.upgrade(config, "head")
            assert demo_state(engine) == (DEMO_VIEWS, expected_settings)
        finally:
            engine.dispose()


def insert_client_without_code(engine: Engine, name: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO core.clients (name, industry, billing_model) "
                "VALUES (:n, 'retail', 'client_owned')"
            ),
            {"n": name},
        )


def write_reference(path: Path, *clients: tuple[str, str]) -> Path:
    payload = {"clients": [{"code": code, "name": name} for code, name in clients]}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def has_code_column(engine: Engine) -> bool:
    return "code" in {c["name"] for c in inspect(engine).get_columns("clients", schema="core")}


def test_client_code_is_backfilled_by_name(database_url: str, tmp_path: Path) -> None:
    with temporary_database(database_url) as url:
        config = alembic_config(url)
        engine = create_engine(url)
        try:
            command.upgrade(config, "0003")
            insert_client_without_code(engine, "UAB Aaa")
            insert_client_without_code(engine, "UAB Bbb")
            config.attributes["reference_path"] = write_reference(
                tmp_path / "reference.json", ("AAA", "UAB Aaa"), ("BBB", "UAB Bbb"), ("CCC", "New")
            )
            command.upgrade(config, "head")
            with engine.connect() as conn:
                rows = conn.execute(text("SELECT code, name FROM core.clients ORDER BY code")).all()
            assert [tuple(r) for r in rows] == [("AAA", "UAB Aaa"), ("BBB", "UAB Bbb")]
        finally:
            engine.dispose()


def test_client_code_backfill_aborts_on_an_unmatched_client(
    database_url: str, tmp_path: Path
) -> None:
    with temporary_database(database_url) as url:
        config = alembic_config(url)
        engine = create_engine(url)
        try:
            command.upgrade(config, "0003")
            insert_client_without_code(engine, "UAB Aaa")
            config.attributes["reference_path"] = write_reference(
                tmp_path / "reference.json", ("BBB", "UAB Bbb")
            )
            with pytest.raises(RuntimeError, match="UAB Aaa"):
                command.upgrade(config, "head")
            assert not has_code_column(engine)
        finally:
            engine.dispose()


def test_client_code_backfill_aborts_without_the_reference_file(
    database_url: str, tmp_path: Path
) -> None:
    with temporary_database(database_url) as url:
        config = alembic_config(url)
        engine = create_engine(url)
        try:
            command.upgrade(config, "0003")
            insert_client_without_code(engine, "UAB Aaa")
            config.attributes["reference_path"] = tmp_path / "missing.json"
            with pytest.raises(RuntimeError, match="make generate"):
                command.upgrade(config, "head")
        finally:
            engine.dispose()


def test_empty_clients_table_never_reads_the_reference_file(
    database_url: str, tmp_path: Path
) -> None:
    with temporary_database(database_url) as url:
        config = alembic_config(url)
        engine = create_engine(url)
        try:
            config.attributes["reference_path"] = tmp_path / "missing.json"
            command.upgrade(config, "head")
            assert has_code_column(engine)
        finally:
            engine.dispose()


def test_client_code_is_required(conn: Connection) -> None:
    with pytest.raises(IntegrityError, match="code"):
        conn.execute(
            text(
                "INSERT INTO core.clients (name, industry, billing_model) "
                "VALUES ('x', 'retail', 'client_owned')"
            )
        )


def test_client_code_is_unique(conn: Connection) -> None:
    sql = (
        "INSERT INTO core.clients (code, name, industry, billing_model) "
        "VALUES ('DUP', :n, 'retail', 'client_owned')"
    )
    conn.execute(text(sql), {"n": "first"})
    with pytest.raises(IntegrityError, match="uq_clients_code"):
        conn.execute(text(sql), {"n": "second"})
