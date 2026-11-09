"""Ingest against the real mock platform app, in-process, into a throwaway database.

No HTTP server is started. Needs the local Postgres (`make up`) and the role passwords.
"""

import json
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import repeat
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from app.config import Settings, get_settings
from app.db import session
from app.db.roles import MissingRolePasswordError, ensure_roles, role_passwords
from app.db.session import etl_database_url
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from tests.test_migrations import alembic_config, temporary_database

from mock_platforms.app import create_app
from mock_platforms.config import MockPlatformsSettings
from pipelines import ingest
from pipelines.ingest import (
    FetchError,
    IngestFailure,
    IngestResult,
    IngestSettings,
    RetryPolicy,
    ingest_from_env,
    parse_json,
    retry_after_seconds,
    retry_delay,
    retry_kind,
    run_ingest,
)

TOKEN = "test-token"
ACCOUNTS = {"meta": ["1001", "1002"], "google": ["2001", "2002"], "tiktok": ["3001", "3002"]}
ACCOUNT_COUNT = 6
TABLES = {name: f"raw.{name}_responses" for name in ACCOUNTS}
DAYS = [date(2026, 10, day) for day in range(13, 18)]
ROWS_PER_ACCOUNT = len(DAYS)
PAGE_SIZE = 2
PAGES_PER_ACCOUNT = 3
PERIOD_START = DAYS[0]
PERIOD_END = DAYS[-1]
TOTAL_PAGES = ACCOUNT_COUNT * PAGES_PER_ACCOUNT
TOTAL_ROWS = ACCOUNT_COUNT * ROWS_PER_ACCOUNT


class ScriptedFaults:
    """Per-platform status script; once it runs out, requests succeed."""

    def __init__(self, scripts: dict[str, Iterable[int | None]]) -> None:
        self._scripts = {platform: iter(script) for platform, script in scripts.items()}

    def next_status(self, platform: str) -> int | None:
        return next(self._scripts.get(platform, iter(())), None)


class RecordingClient(TestClient):
    """Remembers each successful report body, as the HTTP layer delivered it."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.bodies: list[bytes] = []

    def get(self, url: Any, **kwargs: Any) -> httpx.Response:  # type: ignore[override]
        response = super().get(url, **kwargs)
        if response.status_code == 200 and str(url).endswith(("/report", "/insights")):
            self.bodies.append(response.content)
        return response


class Sleeps(list[float]):
    def __call__(self, seconds: float) -> None:
        self.append(seconds)


def delivery_rows(days: list[date]) -> list[dict[str, Any]]:
    return [
        {
            "campaign_id": "111",
            "campaign_name": "Žalgirio kampanija [REF]",
            "date": day.isoformat(),
            "spend": f"{100 + index}.45",
            "impressions": 1000 + index,
            "clicks": 10 + index,
            "conversions": index,
        }
        for index, day in enumerate(days)
    ]


def write_delivery(delivery_dir: Path, days: list[date]) -> None:
    for platform, account_ids in ACCOUNTS.items():
        (delivery_dir / platform).mkdir(parents=True, exist_ok=True)
        for account_id in account_ids:
            payload = {
                "platform": platform,
                "account_id": account_id,
                "timezone": "UTC",
                "currency": "EUR",
                "rows": delivery_rows(days),
            }
            (delivery_dir / platform / f"{account_id}.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )


def build_settings(**overrides: Any) -> IngestSettings:
    values = {
        "base_url": "http://testserver",
        "MOCK_PLATFORMS_TOKEN": TOKEN,
        "period_start": PERIOD_START,
        "period_end": PERIOD_END,
        "page_size": PAGE_SIZE,
        **overrides,
    }
    return IngestSettings(_env_file=None, **values)


def build_client(
    delivery_dir: Path,
    scripts: dict[str, Iterable[int | None]] | None = None,
    retry_after: int = 1,
) -> RecordingClient:
    app = create_app(
        MockPlatformsSettings(
            _env_file=None,
            token=TOKEN,
            delivery_dir=delivery_dir,
            faults_enabled=False,
            retry_after_seconds=retry_after,
        )
    )
    app.state.faults = ScriptedFaults(scripts or {})
    return RecordingClient(app)


def stub_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://stub")


def stub_meta(report: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    """Only Meta has accounts (1001, 1002); `report` answers its insights calls."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/insights"):
            return report(request)
        if path == "/meta/adaccounts":
            return httpx.Response(200, json={"data": [{"account_id": a} for a in ACCOUNTS["meta"]]})
        if path == "/google/customers":
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, json={"code": 0, "data": {"list": []}})

    return stub_client(handler)


@dataclass(frozen=True)
class Db:
    owner: Engine
    etl: Engine
    url: URL
    passwords: dict[str, str]


@pytest.fixture(scope="module")
def module_db(database_url: str) -> Iterator[Db]:
    try:
        passwords = role_passwords(get_settings())
    except MissingRolePasswordError as exc:
        pytest.fail(str(exc))
    with temporary_database(database_url) as url:
        owner = create_engine(url)
        with owner.begin() as conn:
            ensure_roles(conn, passwords)
        command.upgrade(alembic_config(url), "head")
        settings = Settings(
            _env_file=None,
            database_url=url.render_as_string(hide_password=False),
            etl_db_password=passwords["etl"],
        )
        etl = create_engine(etl_database_url(settings))
        try:
            yield Db(owner=owner, etl=etl, url=url, passwords=passwords)
        finally:
            etl.dispose()
            owner.dispose()


@pytest.fixture
def db(module_db: Db) -> Db:
    with module_db.owner.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(TABLES.values())}"))
    return module_db


@pytest.fixture
def delivery_dir(tmp_path: Path) -> Path:
    path = tmp_path / "delivery"
    write_delivery(path, DAYS)
    return path


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


def ingest_with(db: Db, client: httpx.Client, sleeps: Sleeps, **kwargs: Any) -> IngestResult:
    settings = kwargs.pop("settings", build_settings())
    run_id = kwargs.pop("run_id", uuid.uuid4())
    return run_ingest(run_id, settings, client, db.etl, sleep=sleeps, **kwargs)


def row_count(db: Db, platform: str) -> int:
    with db.owner.connect() as conn:
        return conn.execute(text(f"SELECT count(*) FROM {TABLES[platform]}")).scalar_one()


def all_row_counts(db: Db) -> dict[str, int]:
    return {platform: row_count(db, platform) for platform in TABLES}


def pages_by_account(db: Db, platform: str) -> dict[str, list[int]]:
    with db.owner.connect() as conn:
        rows = conn.execute(
            text(f"SELECT account_id, page FROM {TABLES[platform]} ORDER BY account_id, page")
        ).all()
    pages: dict[str, list[int]] = {}
    for account_id, page in rows:
        pages.setdefault(account_id, []).append(page)
    return pages


def stored_rows(db: Db, platform: str) -> list[Any]:
    with db.owner.connect() as conn:
        return list(
            conn.execute(
                text(f"SELECT * FROM {TABLES[platform]} ORDER BY account_id, page")
            ).mappings()
        )


FULL_ACCOUNT_PAGES = [1, 2, 3]


# --- pagination -----------------------------------------------------------------------------


@pytest.mark.parametrize("platform", list(ACCOUNTS))
def test_every_page_of_every_account_is_stored(
    db: Db, delivery_dir: Path, sleeps: Sleeps, platform: str
) -> None:
    result = ingest_with(db, build_client(delivery_dir), sleeps)
    assert result.failures == []
    assert pages_by_account(db, platform) == {
        account: FULL_ACCOUNT_PAGES for account in ACCOUNTS[platform]
    }


def rows_in_payload(platform: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    if platform == "tiktok":
        return payload["data"]["list"]
    return payload["data"] if platform == "meta" else payload["results"]


@pytest.mark.parametrize("platform", list(ACCOUNTS))
def test_stored_pages_cover_every_delivery_row_once(
    db: Db, delivery_dir: Path, sleeps: Sleeps, platform: str
) -> None:
    ingest_with(db, build_client(delivery_dir), sleeps)
    for account in ACCOUNTS[platform]:
        rows = [
            row
            for stored in stored_rows(db, platform)
            if stored["account_id"] == account
            for row in rows_in_payload(platform, stored["payload"])
        ]
        assert [row["impressions"] for row in rows] == [1000 + i for i in range(ROWS_PER_ACCOUNT)]


def test_result_counts_pages_and_rows(db: Db, delivery_dir: Path, sleeps: Sleeps) -> None:
    run_id = uuid.uuid4()
    result = ingest_with(db, build_client(delivery_dir), sleeps, run_id=run_id)
    assert result == IngestResult(
        run_id=run_id, pages_fetched=TOTAL_PAGES, rows_seen=TOTAL_ROWS, retries=0, failures=[]
    )
    assert sleeps == []


def test_result_is_serialisable_for_the_run_record() -> None:
    run_id = uuid.uuid4()
    result = IngestResult(run_id=run_id, failures=[IngestFailure("meta", "1", "HTTP 500")])
    assert asdict(result)["failures"] == [
        {"platform": "meta", "account": "1", "reason": "HTTP 500"}
    ]


def test_empty_period_stores_one_empty_page_per_account(
    db: Db, delivery_dir: Path, sleeps: Sleeps
) -> None:
    settings = build_settings(period_start=date(2026, 1, 1), period_end=date(2026, 1, 31))
    result = ingest_with(db, build_client(delivery_dir), sleeps, settings=settings)
    assert result.failures == []
    assert result.pages_fetched == ACCOUNT_COUNT
    assert result.rows_seen == 0
    assert all(count == 2 for count in all_row_counts(db).values())


def test_non_advancing_cursor_is_a_failure(db: Db, sleeps: Sleeps) -> None:
    stuck_body = {"data": [], "paging": {"cursors": {"after": "same"}}}
    client = stub_meta(lambda request: httpx.Response(200, json=stuck_body))
    result = ingest_with(db, client, sleeps)
    assert [f.account for f in result.failures] == ACCOUNTS["meta"]
    assert all("did not advance" in f.reason for f in result.failures)
    assert row_count(db, "meta") == 0


def test_non_advancing_tiktok_page_is_a_failure(db: Db, sleeps: Sleeps) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tiktok/advertisers":
            return httpx.Response(200, json={"data": {"list": [{"account_id": "3001"}]}})
        if request.url.path.endswith("/report"):
            info = {"page": 1, "total_page": 3}
            return httpx.Response(200, json={"data": {"list": [], "page_info": info}})
        return httpx.Response(200, json={"data": [], "results": []})

    result = ingest_with(db, stub_client(handler), sleeps)
    assert [(f.platform, f.account) for f in result.failures] == [("tiktok", "3001")]


# --- 429 ------------------------------------------------------------------------------------


def test_429_then_success_waits_for_retry_after(db: Db, delivery_dir: Path, sleeps: Sleeps) -> None:
    client = build_client(delivery_dir, {"meta": [429]}, retry_after=7)
    result = ingest_with(db, client, sleeps)
    assert sleeps == [7]
    assert result.retries == 1
    assert result.failures == []
    assert result.pages_fetched == TOTAL_PAGES


def test_429_on_a_report_page_resumes_the_same_account(
    db: Db, delivery_dir: Path, sleeps: Sleeps
) -> None:
    # Listing ok, first page ok, second page limited once.
    client = build_client(delivery_dir, {"google": [None, None, 429]})
    result = ingest_with(db, client, sleeps)
    assert result.failures == []
    assert result.retries == 1
    assert pages_by_account(db, "google")["2001"] == FULL_ACCOUNT_PAGES


def test_429_exhausted_is_recorded_and_others_continue(
    db: Db, delivery_dir: Path, sleeps: Sleeps
) -> None:
    client = build_client(delivery_dir, {"tiktok": repeat(429)})
    result = ingest_with(db, client, sleeps)
    assert len(result.failures) == 1
    failure = result.failures[0]
    assert (failure.platform, failure.account) == ("tiktok", None)
    assert "HTTP 429" in failure.reason
    assert result.retries == RetryPolicy().max_attempts_429 - 1
    assert row_count(db, "meta") == 2 * PAGES_PER_ACCOUNT


# --- 500 ------------------------------------------------------------------------------------


def test_500_retried_then_success(db: Db, delivery_dir: Path, sleeps: Sleeps) -> None:
    client = build_client(delivery_dir, {"meta": [500, 500]})
    result = ingest_with(db, client, sleeps)
    assert sleeps == [1, 2]
    assert result.failures == []


def test_500_forever_is_recorded_while_other_platforms_load(
    db: Db, delivery_dir: Path, sleeps: Sleeps
) -> None:
    client = build_client(delivery_dir, {"meta": repeat(500)})
    result = ingest_with(db, client, sleeps)
    assert [(f.platform, f.account) for f in result.failures] == [("meta", None)]
    assert "HTTP 500" in result.failures[0].reason
    assert result.retries == RetryPolicy().max_attempts_5xx - 1
    assert all_row_counts(db) == {
        "meta": 0,
        "google": 2 * PAGES_PER_ACCOUNT,
        "tiktok": 2 * PAGES_PER_ACCOUNT,
    }


def test_500_on_one_account_is_recorded_and_the_next_account_loads(
    db: Db, delivery_dir: Path, sleeps: Sleeps
) -> None:
    client = build_client(delivery_dir, {"meta": [None, 500, 500, 500, 500]})
    result = ingest_with(db, client, sleeps)
    assert [(f.platform, f.account) for f in result.failures] == [("meta", "1001")]
    assert pages_by_account(db, "meta") == {"1002": FULL_ACCOUNT_PAGES}


def test_failure_partway_through_an_account_stores_none_of_it(
    db: Db, delivery_dir: Path, sleeps: Sleeps
) -> None:
    client = build_client(delivery_dir, {"meta": [None, None, 500, 500, 500, 500]})
    result = ingest_with(db, client, sleeps)
    assert [f.account for f in result.failures] == ["1001"]
    assert "1001" not in pages_by_account(db, "meta")
    # The page received before the failure still counts as fetched.
    assert result.pages_fetched == TOTAL_PAGES - PAGES_PER_ACCOUNT + 1


def test_network_error_is_retried(db: Db, sleeps: Sleeps) -> None:
    failures = iter([httpx.ConnectError("refused")])

    def handler(request: httpx.Request) -> httpx.Response:
        for error in failures:
            raise error
        return httpx.Response(200, json={"data": [], "results": [], "code": 0})

    result = ingest_with(db, stub_client(handler), sleeps, settings=build_settings())
    assert sleeps == [1]
    assert result.retries == 1


def test_network_error_forever_reports_only_the_error_type(db: Db, sleeps: Sleeps) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    result = ingest_with(db, stub_client(handler), sleeps)
    assert len(result.failures) == 3
    assert all("ReadTimeout" in f.reason for f in result.failures)


# --- backoff --------------------------------------------------------------------------------


def test_backoff_doubles_up_to_the_cap() -> None:
    policy = RetryPolicy(base_delay=1, cap=5)
    assert [policy.backoff(n) for n in range(1, 6)] == [1, 2, 4, 5, 5]


def test_500_delays_are_exponential(db: Db, delivery_dir: Path, sleeps: Sleeps) -> None:
    client = build_client(delivery_dir, {"meta": repeat(500)})
    ingest_with(db, client, sleeps)
    assert sleeps == [1, 2, 4]


def test_429_delays_grow_and_stop_at_the_cap(db: Db, delivery_dir: Path, sleeps: Sleeps) -> None:
    client = build_client(delivery_dir, {"meta": repeat(429)}, retry_after=0)
    ingest_with(db, client, sleeps, policy=RetryPolicy(base_delay=1, cap=5))
    assert sleeps == [1, 2, 4, 5, 5]


def test_429_backoff_overtakes_a_small_retry_after(
    db: Db, delivery_dir: Path, sleeps: Sleeps
) -> None:
    client = build_client(delivery_dir, {"meta": repeat(429)}, retry_after=3)
    ingest_with(db, client, sleeps, policy=RetryPolicy(base_delay=1, cap=30))
    assert sleeps == [3, 3, 4, 8, 16]


def test_retry_after_above_the_cap_is_honoured(db: Db, delivery_dir: Path, sleeps: Sleeps) -> None:
    client = build_client(delivery_dir, {"meta": [429]}, retry_after=60)
    ingest_with(db, client, sleeps, policy=RetryPolicy(base_delay=1, cap=5))
    assert sleeps == [60]


def test_429_and_500_use_separate_attempt_budgets(
    db: Db, delivery_dir: Path, sleeps: Sleeps
) -> None:
    client = build_client(delivery_dir, {"meta": [500, 429, 500]}, retry_after=0)
    result = ingest_with(db, client, sleeps)
    assert sleeps == [1, 1, 2]
    assert result.failures == []


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Retry-After": "12"}, 12.0),
        ({}, 0.0),
        ({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, 0.0),
    ],
    ids=["seconds", "missing", "http-date"],
)
def test_retry_after_seconds(headers: dict[str, str], expected: float) -> None:
    assert retry_after_seconds(httpx.Response(429, headers=headers)) == expected


def test_retry_delay_without_a_header_is_the_backoff() -> None:
    assert retry_delay(httpx.Response(429), 3, RetryPolicy()) == 4


@pytest.mark.parametrize(
    ("status", "expected"),
    [(429, "rate_limited"), (500, "server_error"), (503, "server_error"), (404, None)],
)
def test_retry_kind(status: int, expected: str | None) -> None:
    assert retry_kind(httpx.Response(status)) == expected


def test_retry_kind_treats_no_response_as_a_server_error() -> None:
    assert retry_kind(None) == "server_error"


# --- non-retryable failures -----------------------------------------------------------------


def test_bad_token_fails_every_platform_without_retrying(
    db: Db, delivery_dir: Path, sleeps: Sleeps
) -> None:
    settings = build_settings(MOCK_PLATFORMS_TOKEN="wrong")
    result = ingest_with(db, build_client(delivery_dir), sleeps, settings=settings)
    assert [(f.platform, f.account) for f in result.failures] == [
        ("meta", None),
        ("google", None),
        ("tiktok", None),
    ]
    assert all("HTTP 401" in f.reason for f in result.failures)
    assert sleeps == []


def test_token_is_never_in_a_failure_reason(db: Db, delivery_dir: Path, sleeps: Sleeps) -> None:
    client = build_client(delivery_dir, {"meta": repeat(500)})
    result = ingest_with(db, client, sleeps)
    assert TOKEN not in str(result)


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        pytest.param(b"not json", "Unparsable", id="not-json"),
        pytest.param(b"\xff\xfe{}", "Unparsable", id="not-utf8"),
        pytest.param(b"[1, 2]", "Unexpected", id="not-an-object"),
        pytest.param(b'{"data": 5, "paging": {"cursors": {}}}', "Unexpected", id="rows-not-a-list"),
        pytest.param(b'{"data": []}', "Unexpected", id="missing-paging"),
        pytest.param(
            b'{"data": ["\\u0000"], "paging": {"cursors": {}}}', "rejected", id="nul-in-json"
        ),
    ],
)
def test_malformed_page_fails_the_account_and_the_run_continues(
    db: Db, sleeps: Sleeps, content: bytes, reason: str
) -> None:
    client = stub_meta(lambda request: httpx.Response(200, content=content))
    result = ingest_with(db, client, sleeps)
    assert [f.account for f in result.failures] == ACCOUNTS["meta"]
    assert all(reason in f.reason for f in result.failures)
    assert sleeps == []
    assert row_count(db, "meta") == 0


def test_malformed_account_list_is_a_platform_failure(db: Db, sleeps: Sleeps) -> None:
    client = stub_client(lambda request: httpx.Response(200, json={"unexpected": True}))
    result = ingest_with(db, client, sleeps)
    assert [f.account for f in result.failures] == [None, None, None]
    assert all("Unexpected" in f.reason for f in result.failures)


def test_parse_json_returns_text_and_body() -> None:
    assert parse_json(httpx.Response(200, content=b'{"a": 1}'), "/x") == ('{"a": 1}', {"a": 1})


def test_parse_json_rejects_garbage() -> None:
    with pytest.raises(FetchError, match="/x"):
        parse_json(httpx.Response(200, content=b"{"), "/x")


# --- stored as received ---------------------------------------------------------------------


NON_CANONICAL = (
    '{ "zeta": 1.10,\n  "data": [ {"name": "Žalgiris", "big": 0.1234567890123456789} ],'
    '   "paging": {"cursors": {}} }'
).encode()


def test_body_keeps_the_exact_bytes_jsonb_would_normalise(db: Db, sleeps: Sleeps) -> None:
    client = stub_meta(lambda request: httpx.Response(200, content=NON_CANONICAL))
    ingest_with(db, client, sleeps)
    stored = stored_rows(db, "meta")
    assert [bytes(row["body"]) for row in stored] == [NON_CANONICAL, NON_CANONICAL]


def test_every_real_response_is_stored_byte_for_byte(
    db: Db, delivery_dir: Path, sleeps: Sleeps
) -> None:
    client = build_client(delivery_dir)
    ingest_with(db, client, sleeps)
    stored = sorted(
        bytes(row["body"]) for platform in ACCOUNTS for row in stored_rows(db, platform)
    )
    assert stored == sorted(client.bodies)
    assert len(stored) == TOTAL_PAGES


def test_payload_is_queryable_json_with_exact_numbers(db: Db, sleeps: Sleeps) -> None:
    client = stub_meta(lambda request: httpx.Response(200, content=NON_CANONICAL))
    ingest_with(db, client, sleeps)
    with db.owner.connect() as conn:
        name, big = conn.execute(
            text(
                "SELECT payload #>> '{data,0,name}', payload #>> '{data,0,big}' "
                "FROM raw.meta_responses LIMIT 1"
            )
        ).one()
    assert name == "Žalgiris"
    assert big == "0.1234567890123456789"


def test_rows_carry_run_period_account_and_fetch_time(
    db: Db, delivery_dir: Path, sleeps: Sleeps
) -> None:
    run_id = uuid.uuid4()
    before = datetime.now(UTC)
    ingest_with(db, build_client(delivery_dir), sleeps, run_id=run_id)
    for platform in ACCOUNTS:
        for row in stored_rows(db, platform):
            assert row["run_id"] == run_id
            assert row["account_id"] in ACCOUNTS[platform]
            assert (row["period_start"], row["period_end"]) == (PERIOD_START, PERIOD_END)
            assert row["fetched_at"].utcoffset() == timedelta(0)
            assert before <= row["fetched_at"] <= datetime.now(UTC)


# --- running twice --------------------------------------------------------------------------


def test_running_twice_keeps_row_counts_and_takes_the_new_run_id(
    db: Db, delivery_dir: Path, sleeps: Sleeps
) -> None:
    ingest_with(db, build_client(delivery_dir), sleeps)
    first = all_row_counts(db)
    second_run = uuid.uuid4()
    ingest_with(db, build_client(delivery_dir), sleeps, run_id=second_run)
    assert all_row_counts(db) == first
    assert {row["run_id"] for row in stored_rows(db, "meta")} == {second_run}


def test_rerun_with_fewer_pages_leaves_no_stale_pages(
    db: Db, delivery_dir: Path, tmp_path: Path, sleeps: Sleeps
) -> None:
    ingest_with(db, build_client(delivery_dir), sleeps)
    shorter = tmp_path / "shorter"
    write_delivery(shorter, DAYS[:2])
    ingest_with(db, build_client(shorter), sleeps)
    assert pages_by_account(db, "meta") == {account: [1] for account in ACCOUNTS["meta"]}


def test_failed_rerun_keeps_the_previous_snapshot(
    db: Db, delivery_dir: Path, sleeps: Sleeps
) -> None:
    first_run = uuid.uuid4()
    ingest_with(db, build_client(delivery_dir), sleeps, run_id=first_run)
    client = build_client(delivery_dir, {"meta": repeat(500)})
    second = ingest_with(db, client, sleeps)
    assert second.failures
    stored = stored_rows(db, "meta")
    assert len(stored) == 2 * PAGES_PER_ACCOUNT
    assert {row["run_id"] for row in stored} == {first_run}


def test_a_different_period_is_a_separate_snapshot(
    db: Db, delivery_dir: Path, sleeps: Sleeps
) -> None:
    ingest_with(db, build_client(delivery_dir), sleeps)
    first = row_count(db, "meta")
    narrower = build_settings(period_start=DAYS[0], period_end=DAYS[0])
    ingest_with(db, build_client(delivery_dir), sleeps, settings=narrower)
    assert row_count(db, "meta") == first + len(ACCOUNTS["meta"])


# --- database role --------------------------------------------------------------------------


def test_ingest_engine_logs_in_as_etl(db: Db) -> None:
    with db.etl.connect() as conn:
        assert conn.execute(text("SELECT current_user")).scalar_one() == "etl"


def test_etl_role_cannot_read_eval(db: Db) -> None:
    with db.etl.connect() as conn, pytest.raises(DBAPIError, match="permission denied"):
        conn.execute(text("SELECT * FROM eval.answer_key"))


def test_etl_url_swaps_the_owner_credentials() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://owner:secret@db:5432/matchbook",
        etl_db_password="etl-pass",
    )
    url = etl_database_url(settings)
    assert (url.username, url.password) == ("etl", "etl-pass")
    assert (url.host, url.port, url.database) == ("db", 5432, "matchbook")


def test_etl_url_requires_the_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ETL_DB_PASSWORD", raising=False)
    settings = Settings(_env_file=None, database_url="postgresql+psycopg://o:s@db/m")
    with pytest.raises(MissingRolePasswordError, match="ETL_DB_PASSWORD"):
        etl_database_url(settings)


def test_create_etl_engine_uses_the_etl_role(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://owner:secret@db/matchbook",
        etl_db_password="etl-pass",
    )
    monkeypatch.setattr(session, "get_settings", lambda: settings)
    engine = session.create_etl_engine()
    assert engine.url.username == "etl"
    engine.dispose()


def test_ingest_source_never_mentions_the_answer_key() -> None:
    source = Path(ingest.__file__).read_text(encoding="utf-8")
    assert "answer_key" not in source


# --- configuration and entry point ----------------------------------------------------------


def test_period_must_be_ordered() -> None:
    with pytest.raises(ValidationError, match="period_start"):
        build_settings(period_start=PERIOD_END, period_end=PERIOD_START)


def test_token_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MOCK_PLATFORMS_TOKEN", raising=False)
    with pytest.raises(ValidationError):
        IngestSettings(
            _env_file=None, base_url="http://x", period_start=DAYS[0], period_end=DAYS[-1]
        )


@pytest.mark.parametrize("size", [0, 1001])
def test_page_size_must_fit_the_api(size: int) -> None:
    with pytest.raises(ValidationError):
        build_settings(page_size=size)


def test_settings_read_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("INGEST_BASE_URL", "http://mock:8100")
    monkeypatch.setenv("MOCK_PLATFORMS_TOKEN", "from-env")
    monkeypatch.setenv("INGEST_PERIOD_START", "2026-05-01")
    monkeypatch.setenv("INGEST_PERIOD_END", "2026-10-31")
    monkeypatch.delenv("INGEST_PAGE_SIZE", raising=False)
    settings = IngestSettings()
    assert settings.base_url == "http://mock:8100"
    assert settings.token.get_secret_value() == "from-env"
    assert (settings.period_start, settings.period_end) == (date(2026, 5, 1), date(2026, 10, 31))
    assert settings.page_size == 100


def test_ingest_from_env_runs_with_the_etl_engine(
    db: Db,
    delivery_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("INGEST_BASE_URL", "http://testserver")
    monkeypatch.setenv("MOCK_PLATFORMS_TOKEN", TOKEN)
    monkeypatch.setenv("INGEST_PERIOD_START", PERIOD_START.isoformat())
    monkeypatch.setenv("INGEST_PERIOD_END", PERIOD_END.isoformat())
    monkeypatch.setenv("INGEST_PAGE_SIZE", str(PAGE_SIZE))
    etl = db.url.set(username="etl", password=db.passwords["etl"])
    monkeypatch.setattr(ingest, "create_etl_engine", lambda: create_engine(etl))
    monkeypatch.setattr(ingest.httpx, "Client", lambda **kwargs: build_client(delivery_dir))
    run_id = uuid.uuid4()
    result = ingest_from_env(run_id)
    assert result.failures == []
    assert result.pages_fetched == TOTAL_PAGES
    assert {row["run_id"] for row in stored_rows(db, "tiktok")} == {run_id}
