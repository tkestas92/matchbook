"""Log in as each least-privilege role and prove what it can and cannot do.

Needs the local Postgres (`make up`) and the role passwords from `.env`.
"""

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import pytest
from alembic import command
from app.config import Settings, get_settings
from app.db.roles import (
    LOGIN_ROLES,
    MissingRolePasswordError,
    ensure_roles,
    role_passwords,
)
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from tests.schema_expectations import EXPECTED_TABLES
from tests.test_migrations import alembic_config, insert, temporary_database

AGENT_VIEWS = ("v_spend_daily", "v_pacing_base", "v_invoices", "v_issues")
DENIED = "permission denied"


@dataclass(frozen=True)
class Seed:
    client_a: int
    client_b: int
    user_a: int
    user_b: int
    unscoped_user: int


@dataclass(frozen=True)
class Db:
    url: URL
    passwords: dict[str, str]
    seed: Seed


def seed_client(conn: Connection, platform_id: int, run_id: uuid.UUID) -> int:
    """A client with one row in every table an agent view reads."""
    suffix = uuid.uuid4().hex[:8]
    client_id = insert(
        conn,
        "INSERT INTO core.clients (name, industry, billing_model) "
        "VALUES (:n, 'retail', 'client_owned') RETURNING id",
        n=f"client-{suffix}",
    )
    params = {"c": client_id, "p": platform_id, "r": run_id, "s": suffix}
    account_id = insert(
        conn,
        "INSERT INTO core.ad_accounts "
        "(client_id, platform_id, external_account_id, timezone, run_id) "
        "VALUES (:c, :p, :s, 'Europe/Vilnius', :r) RETURNING id",
        **params,
    )
    io_id = insert(
        conn,
        "INSERT INTO core.insertion_orders "
        "(client_id, platform_id, campaign_ref, start_date, end_date, budget, run_id) "
        "VALUES (:c, :p, :s, '2026-10-01', '2026-10-31', 1000, :r) RETURNING id",
        **params,
    )
    campaign_id = insert(
        conn,
        "INSERT INTO core.campaigns "
        "(platform_id, ad_account_id, platform_campaign_id, name, io_id, run_id) "
        "VALUES (:p, :a, :s, 'Campaign', :io, :r) RETURNING id",
        a=account_id,
        io=io_id,
        **params,
    )
    insert(
        conn,
        "INSERT INTO core.daily_delivery "
        "(campaign_id, date, spend, impressions, clicks, conversions, run_id) "
        "VALUES (:cmp, '2026-10-15', 12.34, 1000, 10, 1, :r) RETURNING id",
        cmp=campaign_id,
        r=run_id,
    )
    insert(
        conn,
        "INSERT INTO core.invoices "
        "(kind, vendor, number, client_id, period_start, period_end, total, run_id) "
        "VALUES ('client', 'Agency', :s, :c, '2026-10-01', '2026-10-31', 100, :r) "
        "RETURNING id",
        **params,
    )
    insert(
        conn,
        "INSERT INTO app.issues "
        "(dedup_key, type, severity, client_id, explanation, run_id) "
        "VALUES (:s, 'overcharge', 'high', :c, 'Too much', :r) RETURNING id",
        **params,
    )
    return client_id


def seed_user(conn: Connection, client_ids: tuple[int, ...]) -> int:
    user_id = insert(
        conn,
        "INSERT INTO app.users (email, role) VALUES (:e, 'planner') RETURNING id",
        e=f"{uuid.uuid4().hex[:8]}@example.com",
    )
    for client_id in client_ids:
        conn.execute(
            text(
                "INSERT INTO app.user_client_scope (user_id, client_id) VALUES (:u, :c)"
            ),
            {"u": user_id, "c": client_id},
        )
    return user_id


def seed(conn: Connection) -> Seed:
    run_id = uuid.uuid4()
    platform_id = insert(
        conn,
        "INSERT INTO core.platforms (name, timezone, metric_map) "
        "VALUES ('meta', 'Europe/Vilnius', '{}') RETURNING id",
    )
    client_a = seed_client(conn, platform_id, run_id)
    client_b = seed_client(conn, platform_id, run_id)
    conn.execute(
        text(
            "INSERT INTO eval.answer_key (error_type, entity_ref, period_start, "
            "period_end, expected_detector) "
            "VALUES ('overcharge', 'x', '2026-10-01', '2026-10-31', 'rules')"
        )
    )
    return Seed(
        client_a=client_a,
        client_b=client_b,
        user_a=seed_user(conn, (client_a,)),
        user_b=seed_user(conn, (client_b,)),
        unscoped_user=seed_user(conn, ()),
    )


@pytest.fixture(scope="module")
def db() -> Iterator[Db]:
    try:
        passwords = role_passwords(get_settings())
    except MissingRolePasswordError as exc:
        pytest.fail(str(exc))
    with temporary_database() as url:
        admin = create_engine(url)
        try:
            with admin.begin() as conn:
                ensure_roles(conn, passwords)
            command.upgrade(alembic_config(url), "head")
            with admin.begin() as conn:
                seeded = seed(conn)
            yield Db(url=url, passwords=passwords, seed=seeded)
        finally:
            admin.dispose()


@pytest.fixture(scope="module")
def login(db: Db) -> Iterator[Callable[[str], Engine]]:
    """Engine that logs in as the given role."""
    engines: dict[str, Engine] = {}

    def engine_for(role: str) -> Engine:
        if role not in engines:
            engines[role] = create_engine(
                db.url.set(username=role, password=db.passwords[role])
            )
        return engines[role]

    yield engine_for
    for engine in engines.values():
        engine.dispose()


@pytest.fixture
def as_role(
    login: Callable[[str], Engine],
) -> Iterator[Callable[[str], Connection]]:
    """A connection as `role` inside a transaction that is rolled back afterwards."""
    connections: list[Connection] = []

    def connect(role: str) -> Connection:
        connection = login(role).connect()
        connection.begin()
        connections.append(connection)
        return connection

    yield connect
    for connection in connections:
        connection.close()


def set_user(conn: Connection, user_id: int) -> None:
    """What the trusted caller does before running agent SQL."""
    conn.execute(text(f"SET LOCAL app.user_id = '{int(user_id)}'"))


def visible_clients(conn: Connection, view: str) -> set[int]:
    return set(conn.execute(text(f"SELECT client_id FROM core.{view}")).scalars())


def assert_denied(conn: Connection, sql: str, match: str = DENIED) -> None:
    with pytest.raises(DBAPIError, match=match):
        conn.execute(text(sql))


# --- roles script --------------------------------------------------------------


def test_ensure_roles_is_idempotent_and_roles_are_unprivileged(db: Db) -> None:
    engine = create_engine(db.url)
    try:
        with engine.begin() as conn:
            ensure_roles(conn, db.passwords)
            rows = conn.execute(
                text(
                    "SELECT rolname, rolcanlogin, rolsuper, rolbypassrls, "
                    "rolcreaterole, rolcreatedb FROM pg_roles "
                    "WHERE rolname = ANY(:roles)"
                ),
                {"roles": list(LOGIN_ROLES)},
            ).all()
    finally:
        engine.dispose()
    assert {row.rolname for row in rows} == set(LOGIN_ROLES)
    for row in rows:
        assert row.rolcanlogin
        assert not (row.rolsuper or row.rolbypassrls or row.rolcreaterole)
        assert not row.rolcreatedb


def test_role_passwords_require_every_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    for role in LOGIN_ROLES:
        monkeypatch.setenv(f"{role.upper()}_DB_PASSWORD", "x")
    monkeypatch.delenv("AGENT_RO_DB_PASSWORD")
    with pytest.raises(MissingRolePasswordError, match="AGENT_RO_DB_PASSWORD"):
        role_passwords(Settings(_env_file=None))


# --- agent_ro: no writes, no base tables ----------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO core.v_issues (type) VALUES ('x')",
        "UPDATE core.v_invoices SET total = 0",
        (
            "INSERT INTO core.clients (name, industry, billing_model) "
            "VALUES ('x', 'x', 'client_owned')"
        ),
        "UPDATE core.daily_delivery SET spend = 0",
        "DELETE FROM app.issues",
        "INSERT INTO eval.answer_key (error_type) VALUES ('x')",
        "DELETE FROM raw.meta_responses",
        "CREATE TABLE core.agent_table (id int)",
        "CREATE TEMP TABLE agent_temp (id int)",
    ],
)
def test_agent_ro_cannot_write(as_role: Callable[[str], Connection], sql: str) -> None:
    assert_denied(as_role("agent_ro"), sql)


def test_agent_ro_cannot_write_join_view(as_role: Callable[[str], Connection]) -> None:
    # A join view is not auto-updatable, so Postgres rejects it before checking grants.
    assert_denied(
        as_role("agent_ro"),
        "DELETE FROM core.v_spend_daily",
        match="cannot delete from view",
    )


@pytest.mark.parametrize("table", sorted(EXPECTED_TABLES))
def test_agent_ro_cannot_read_base_tables(
    as_role: Callable[[str], Connection], table: str
) -> None:
    assert_denied(as_role("agent_ro"), f"SELECT * FROM {table} LIMIT 1")


def test_agent_ro_has_statement_timeout(as_role: Callable[[str], Connection]) -> None:
    timeout = as_role("agent_ro").execute(text("SHOW statement_timeout")).scalar_one()
    assert timeout == "5s"


# --- agent_ro: RLS through the views ---------------------------------------------


@pytest.mark.parametrize("view", AGENT_VIEWS)
def test_views_apply_rls_even_for_the_superuser(db: Db, view: str) -> None:
    """Base tables hold both clients, yet an unscoped view read returns nothing:
    the view runs as `agent_views`, so RLS cannot be bypassed via the view owner."""
    engine = create_engine(db.url)
    try:
        with engine.connect() as conn:
            clients = set(conn.execute(text("SELECT id FROM core.clients")).scalars())
            assert {db.seed.client_a, db.seed.client_b} <= clients
            assert visible_clients(conn, view) == set()
    finally:
        engine.dispose()


@pytest.mark.parametrize("view", AGENT_VIEWS)
@pytest.mark.parametrize("user", ["a", "b"])
def test_agent_ro_sees_only_scoped_client(
    db: Db, as_role: Callable[[str], Connection], view: str, user: str
) -> None:
    conn = as_role("agent_ro")
    set_user(conn, getattr(db.seed, f"user_{user}"))
    assert visible_clients(conn, view) == {getattr(db.seed, f"client_{user}")}


@pytest.mark.parametrize("view", AGENT_VIEWS)
@pytest.mark.parametrize("scope", ["unset", "empty", "unscoped user", "unknown user"])
def test_agent_ro_without_scope_sees_nothing(
    db: Db, as_role: Callable[[str], Connection], view: str, scope: str
) -> None:
    conn = as_role("agent_ro")
    if scope == "empty":
        conn.execute(text("SET LOCAL app.user_id = ''"))
    elif scope == "unscoped user":
        set_user(conn, db.seed.unscoped_user)
    elif scope == "unknown user":
        set_user(conn, 999_999_999)
    assert visible_clients(conn, view) == set()


# --- agent_ro: cannot widen its own scope ----------------------------------------


def test_agent_ro_cannot_call_set_config(
    db: Db, as_role: Callable[[str], Connection]
) -> None:
    conn = as_role("agent_ro")
    set_user(conn, db.seed.user_a)
    with pytest.raises(DBAPIError, match="permission denied for function set_config"):
        conn.execute(
            text("SELECT set_config('app.user_id', :u, false)"),
            {"u": str(db.seed.user_b)},
        )


def test_agent_ro_scope_holds_after_set_config_attempt(
    db: Db, login: Callable[[str], Engine]
) -> None:
    with login("agent_ro").connect() as conn:
        with pytest.raises(DBAPIError, match=DENIED), conn.begin():
            conn.execute(
                text("SELECT set_config('app.user_id', :u, false)"),
                {"u": str(db.seed.user_b)},
            )
        with conn.begin():
            assert all(visible_clients(conn, view) == set() for view in AGENT_VIEWS)
        with conn.begin():
            set_user(conn, db.seed.user_a)
            for view in AGENT_VIEWS:
                assert visible_clients(conn, view) == {db.seed.client_a}


@pytest.mark.parametrize("role", ["etl", "app", "eval_runner"])
def test_trusted_roles_can_still_call_set_config(
    as_role: Callable[[str], Connection], role: str
) -> None:
    conn = as_role(role)
    value = conn.execute(text("SELECT set_config('app.user_id', '1', true)"))
    assert value.scalar_one() == "1"


# --- app, etl, eval_runner -------------------------------------------------------


@pytest.mark.parametrize("role", ["app", "etl"])
def test_role_cannot_read_eval(as_role: Callable[[str], Connection], role: str) -> None:
    assert_denied(as_role(role), "SELECT * FROM eval.answer_key")


def test_app_reads_core_and_writes_app(
    db: Db, as_role: Callable[[str], Connection]
) -> None:
    conn = as_role("app")
    assert conn.execute(text("SELECT count(*) FROM core.clients")).scalar_one() >= 2
    conn.execute(
        text(
            "INSERT INTO app.issues (dedup_key, type, severity, explanation, run_id) "
            "VALUES ('app-test', 'note', 'low', 'x', gen_random_uuid())"
        )
    )


def test_app_cannot_write_core(as_role: Callable[[str], Connection]) -> None:
    assert_denied(as_role("app"), "UPDATE core.clients SET industry = 'x'")


def test_etl_writes_raw_and_core(as_role: Callable[[str], Connection]) -> None:
    conn = as_role("etl")
    conn.execute(
        text(
            "INSERT INTO raw.meta_responses (page, fetched_at, payload, run_id) "
            "VALUES (1, now(), '{}', gen_random_uuid())"
        )
    )
    conn.execute(text("UPDATE core.daily_delivery SET clicks = clicks"))
    assert conn.execute(text("SELECT count(*) FROM core.ad_accounts")).scalar_one() >= 2


def test_etl_cannot_write_app(as_role: Callable[[str], Connection]) -> None:
    assert_denied(as_role("etl"), "DELETE FROM app.issues")


def test_eval_runner_reads_everything(as_role: Callable[[str], Connection]) -> None:
    conn = as_role("eval_runner")
    for table in sorted(EXPECTED_TABLES):
        conn.execute(text(f"SELECT * FROM {table} LIMIT 1"))
    assert conn.execute(text("SELECT count(*) FROM eval.answer_key")).scalar_one() == 1


def test_eval_runner_cannot_write(as_role: Callable[[str], Connection]) -> None:
    assert_denied(as_role("eval_runner"), "DELETE FROM eval.answer_key")
