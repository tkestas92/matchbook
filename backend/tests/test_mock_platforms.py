import ast
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

import mock_platforms
from mock_platforms import __main__ as entrypoint
from mock_platforms.app import create_app
from mock_platforms.config import MockPlatformsSettings
from mock_platforms.deps import parse_date, parse_date_range
from mock_platforms.faults import FaultInjector
from mock_platforms.pagination import decode_offset, encode_offset
from mock_platforms.store import load_store

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ACCOUNT_IDS = {"meta": "1001", "google": "2002", "tiktok": "3003"}
OCT_DAYS = [date(2026, 10, day) for day in range(13, 18)]
RENAMED_FROM = date(2026, 10, 16)
MISSING_DAY = date(2026, 10, 15)


def campaign_rows() -> list[dict[str, Any]]:
    """Two campaigns over five days; one is renamed (#3), the other misses a day (#9)."""
    rows = []
    for day_index, day in enumerate(OCT_DAYS):
        for campaign_id in ("111", "222"):
            if campaign_id == "222" and day == MISSING_DAY:
                continue
            name = "Renamed" if campaign_id == "111" and day >= RENAMED_FROM else "Tagged [REF]"
            rows.append(
                {
                    "campaign_id": campaign_id,
                    "campaign_name": name,
                    "date": day.isoformat(),
                    "spend": f"{100 + day_index}.45",
                    "impressions": 1000 + day_index,
                    "clicks": 10 + day_index,
                    "conversions": day_index,
                }
            )
    return rows


ROWS = campaign_rows()


@dataclass(frozen=True)
class Case:
    platform: str
    accounts_path: str
    report_path: str
    start_param: str
    end_param: str
    date_format: str
    spend_field: str
    accounts_of: Callable[[dict[str, Any]], list[dict[str, Any]]]
    rows_of: Callable[[dict[str, Any]], list[dict[str, Any]]]
    size_param: str

    def params(self, start: date = OCT_DAYS[0], end: date = OCT_DAYS[-1]) -> dict[str, str]:
        return {self.start_param: self.date_text(start), self.end_param: self.date_text(end)}

    def date_text(self, day: date) -> str:
        return day.strftime(self.date_format)


META = Case(
    platform="meta",
    accounts_path="/meta/adaccounts",
    report_path="/meta/adaccounts/1001/insights",
    start_param="since",
    end_param="until",
    date_format="%Y-%m-%d",
    spend_field="spend",
    accounts_of=lambda body: body["data"],
    rows_of=lambda body: body["data"],
    size_param="limit",
)
GOOGLE = Case(
    platform="google",
    accounts_path="/google/customers",
    report_path="/google/customers/2002/report",
    start_param="start_date",
    end_param="end_date",
    date_format="%Y%m%d",
    spend_field="cost_micros",
    accounts_of=lambda body: body["results"],
    rows_of=lambda body: body["results"],
    size_param="page_size",
)
TIKTOK = Case(
    platform="tiktok",
    accounts_path="/tiktok/advertisers",
    report_path="/tiktok/advertisers/3003/report",
    start_param="start_date",
    end_param="end_date",
    date_format="%Y-%m-%d %H:%M:%S",
    spend_field="cost",
    accounts_of=lambda body: body["data"]["list"],
    rows_of=lambda body: body["data"]["list"],
    size_param="page_size",
)
CASES = [META, GOOGLE, TIKTOK]
SPEND_FIELDS = {case.spend_field for case in CASES}
each_platform = pytest.mark.parametrize("case", CASES, ids=lambda case: case.platform)
each_endpoint = pytest.mark.parametrize(
    ("case", "kind"),
    [(case, kind) for case in CASES for kind in ("accounts", "report")],
    ids=lambda value: value.platform if isinstance(value, Case) else value,
)


def write_delivery(delivery_dir: Path) -> None:
    timezones = {"meta": "Europe/Vilnius", "google": "Europe/Vilnius", "tiktok": "UTC"}
    for platform, account_id in ACCOUNT_IDS.items():
        (delivery_dir / platform).mkdir(parents=True)
        payload = {
            "platform": platform,
            "account_id": account_id,
            "timezone": timezones[platform],
            "currency": "EUR",
            "rows": ROWS,
        }
        (delivery_dir / platform / f"{account_id}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )


@pytest.fixture
def delivery_dir(tmp_path: Path) -> Path:
    path = tmp_path / "generated" / "delivery"
    write_delivery(path)
    return path


def build_settings(delivery_dir: Path, **overrides: Any) -> MockPlatformsSettings:
    values = {"token": TOKEN, "delivery_dir": delivery_dir, "faults_enabled": False, **overrides}
    return MockPlatformsSettings(_env_file=None, **values)


def build_client(delivery_dir: Path, **overrides: Any) -> TestClient:
    return TestClient(create_app(build_settings(delivery_dir, **overrides)))


@pytest.fixture
def client(delivery_dir: Path) -> TestClient:
    return build_client(delivery_dir)


def request_endpoint(client: TestClient, case: Case, kind: str, **kwargs: Any) -> Any:
    if kind == "accounts":
        return client.get(case.accounts_path, **kwargs)
    return client.get(case.report_path, params=case.params(), **kwargs)


def get_report(client: TestClient, case: Case, **params: Any) -> Any:
    return client.get(case.report_path, params={**case.params(), **params}, headers=AUTH)


def walk_token_pages(
    client: TestClient,
    case: Case,
    size: int,
    token_param: str,
    next_token: Callable[[dict[str, Any]], str | None],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    params: dict[str, Any] = {case.size_param: size}
    while True:
        response = get_report(client, case, **params)
        assert response.status_code == 200
        body = response.json()
        rows.extend(case.rows_of(body))
        token = next_token(body)
        if token is None:
            return rows
        params[token_param] = token


def walk_tiktok_pages(client: TestClient, size: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        response = get_report(client, TIKTOK, page=page, page_size=size)
        assert response.status_code == 200
        data = response.json()["data"]
        rows.extend(data["list"])
        if page >= data["page_info"]["total_page"]:
            return rows
        page += 1


def walk_all_pages(client: TestClient, case: Case, size: int) -> list[dict[str, Any]]:
    if case is META:
        return walk_token_pages(
            client, case, size, "after", lambda body: body["paging"]["cursors"].get("after")
        )
    if case is GOOGLE:
        return walk_token_pages(
            client, case, size, "page_token", lambda body: body.get("nextPageToken")
        )
    return walk_tiktok_pages(client, size)


def status_sequence(client: TestClient, case: Case, count: int) -> list[int]:
    return [client.get(case.accounts_path, headers=AUTH).status_code for _ in range(count)]


# --- auth -----------------------------------------------------------------------------------


@each_endpoint
def test_missing_token_is_401(client: TestClient, case: Case, kind: str) -> None:
    response = request_endpoint(client, case, kind)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@each_endpoint
def test_wrong_token_is_401(client: TestClient, case: Case, kind: str) -> None:
    headers = {"Authorization": "Bearer nope"}
    assert request_endpoint(client, case, kind, headers=headers).status_code == 401


@each_endpoint
def test_non_ascii_token_is_401(client: TestClient, case: Case, kind: str) -> None:
    headers = {"Authorization": "Bearer žetonas".encode()}
    assert request_endpoint(client, case, kind, headers=headers).status_code == 401


@each_endpoint
def test_correct_token_is_200(client: TestClient, case: Case, kind: str) -> None:
    assert request_endpoint(client, case, kind, headers=AUTH).status_code == 200


@each_platform
def test_auth_runs_before_faults(delivery_dir: Path, case: Case) -> None:
    client = build_client(delivery_dir, faults_enabled=True, rate_429=1.0, rate_500=0.0)
    assert client.get(case.accounts_path).status_code == 401


def test_rejected_requests_do_not_advance_the_fault_sequence(delivery_dir: Path) -> None:
    fault_settings = {"faults_enabled": True, "rate_429": 0.3, "rate_500": 0.3}
    untouched = build_client(delivery_dir, **fault_settings)
    probed = build_client(delivery_dir, **fault_settings)
    for _ in range(10):
        assert probed.get(META.accounts_path).status_code == 401
    assert status_sequence(probed, META, 30) == status_sequence(untouched, META, 30)


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_routes_are_not_served(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404


# --- accounts -------------------------------------------------------------------------------


@each_platform
def test_accounts_list_describes_the_platforms_accounts(client: TestClient, case: Case) -> None:
    accounts = case.accounts_of(client.get(case.accounts_path, headers=AUTH).json())
    timezone = "UTC" if case is TIKTOK else "Europe/Vilnius"
    assert accounts == [
        {"account_id": ACCOUNT_IDS[case.platform], "timezone": timezone, "currency": "EUR"}
    ]


@each_platform
def test_unknown_account_is_404(client: TestClient, case: Case) -> None:
    path = case.report_path.replace(ACCOUNT_IDS[case.platform], "999")
    assert client.get(path, params=case.params(), headers=AUTH).status_code == 404


@each_platform
def test_other_platforms_account_is_404(client: TestClient, case: Case) -> None:
    other = next(p for p in ACCOUNT_IDS if p != case.platform)
    path = case.report_path.replace(ACCOUNT_IDS[case.platform], ACCOUNT_IDS[other])
    assert client.get(path, params=case.params(), headers=AUTH).status_code == 404


# --- pagination -----------------------------------------------------------------------------


@each_platform
@pytest.mark.parametrize("size", [1, 2, 3, len(ROWS), 1000])
def test_all_rows_come_back_exactly_once_in_order(
    client: TestClient, case: Case, size: int
) -> None:
    served = [(row["campaign_id"], row["date"]) for row in walk_all_pages(client, case, size)]
    expected = [
        (row["campaign_id"], case.date_text(date.fromisoformat(row["date"]))) for row in ROWS
    ]
    assert served == expected


@pytest.mark.parametrize(
    ("case", "param"), [(META, "after"), (GOOGLE, "page_token")], ids=["meta", "google"]
)
@pytest.mark.parametrize("token", ["garbage", encode_offset(-1)])
def test_invalid_page_token_is_400(client: TestClient, case: Case, param: str, token: str) -> None:
    assert get_report(client, case, **{param: token}).status_code == 400


def test_meta_last_page_has_no_cursor(client: TestClient) -> None:
    assert get_report(client, META, limit=1000).json()["paging"] == {"cursors": {}}


def test_google_last_page_has_no_token(client: TestClient) -> None:
    assert "nextPageToken" not in get_report(client, GOOGLE, page_size=1000).json()


def test_tiktok_page_info_counts_rows_and_pages(client: TestClient) -> None:
    data = get_report(client, TIKTOK, page=2, page_size=4).json()["data"]
    assert data["page_info"] == {"page": 2, "page_size": 4, "total_number": 9, "total_page": 3}
    assert len(data["list"]) == 4


def test_tiktok_page_past_the_end_is_empty(client: TestClient) -> None:
    assert get_report(client, TIKTOK, page=99, page_size=4).json()["data"]["list"] == []


@each_platform
@pytest.mark.parametrize("size", [0, 1001])
def test_out_of_range_page_size_is_422(client: TestClient, case: Case, size: int) -> None:
    assert get_report(client, case, **{case.size_param: size}).status_code == 422


# --- formats --------------------------------------------------------------------------------


def first_row(client: TestClient, case: Case) -> dict[str, Any]:
    return case.rows_of(get_report(client, case).json())[0]


@each_platform
def test_row_has_the_platforms_fields_and_no_others(client: TestClient, case: Case) -> None:
    assert set(first_row(client, case)) == {
        "campaign_id",
        "campaign_name",
        "date",
        "impressions",
        "clicks",
        "conversions",
        case.spend_field,
    }


@pytest.mark.parametrize(
    ("case", "pattern"),
    [
        (META, r"\d{4}-\d{2}-\d{2}"),
        (GOOGLE, r"\d{8}"),
        (TIKTOK, r"\d{4}-\d{2}-\d{2} 00:00:00"),
    ],
    ids=["meta", "google", "tiktok"],
)
def test_date_format(client: TestClient, case: Case, pattern: str) -> None:
    assert re.fullmatch(pattern, first_row(client, case)["date"])


@pytest.mark.parametrize("case", [META, TIKTOK], ids=["meta", "tiktok"])
def test_spend_is_an_exact_decimal_string(client: TestClient, case: Case) -> None:
    assert first_row(client, case)[case.spend_field] == "100.45"


def test_google_cost_micros_is_an_exact_integer(client: TestClient) -> None:
    cost = first_row(client, GOOGLE)["cost_micros"]
    assert type(cost) is int
    assert cost == 100_450_000


@each_platform
def test_no_value_is_a_float(client: TestClient, case: Case) -> None:
    for row in case.rows_of(get_report(client, case).json()):
        assert not any(isinstance(value, float) for value in row.values())


@each_platform
def test_row_metrics_match_the_file(client: TestClient, case: Case) -> None:
    row = first_row(client, case)
    assert (row["impressions"], row["clicks"], row["conversions"]) == (1000, 10, 0)
    assert row["campaign_id"] == "111"
    assert row["campaign_name"] == "Tagged [REF]"


@each_platform
def test_planted_rename_and_missing_day_are_served_as_written(
    client: TestClient, case: Case
) -> None:
    rows = walk_all_pages(client, case, 1000)
    names = {r["campaign_name"] for r in rows if r["campaign_id"] == "111"}
    assert names == {"Tagged [REF]", "Renamed"}
    days_222 = {r["date"] for r in rows if r["campaign_id"] == "222"}
    assert case.date_text(MISSING_DAY) not in days_222
    assert len(days_222) == len(OCT_DAYS) - 1


# --- date range -----------------------------------------------------------------------------


@each_platform
def test_date_range_is_inclusive_at_both_ends(client: TestClient, case: Case) -> None:
    response = client.get(
        case.report_path, params=case.params(OCT_DAYS[1], OCT_DAYS[2]), headers=AUTH
    )
    days = {row["date"] for row in case.rows_of(response.json())}
    assert days == {case.date_text(OCT_DAYS[1]), case.date_text(OCT_DAYS[2])}


@each_platform
def test_single_day_range(client: TestClient, case: Case) -> None:
    response = client.get(
        case.report_path, params=case.params(OCT_DAYS[0], OCT_DAYS[0]), headers=AUTH
    )
    assert len(case.rows_of(response.json())) == 2


@each_platform
def test_range_outside_the_data_is_empty(client: TestClient, case: Case) -> None:
    response = client.get(
        case.report_path, params=case.params(date(2026, 1, 1), date(2026, 1, 31)), headers=AUTH
    )
    assert response.status_code == 200
    assert case.rows_of(response.json()) == []


def test_empty_range_has_no_next_page(client: TestClient) -> None:
    outside = META.params(date(2026, 1, 1), date(2026, 1, 31))
    body = client.get(META.report_path, params=outside, headers=AUTH).json()
    assert body["paging"] == {"cursors": {}}


@each_platform
def test_reversed_range_is_400(client: TestClient, case: Case) -> None:
    response = client.get(
        case.report_path, params=case.params(OCT_DAYS[-1], OCT_DAYS[0]), headers=AUTH
    )
    assert response.status_code == 400


@each_platform
@pytest.mark.parametrize("format_of", CASES, ids=lambda case: f"in-{case.platform}-format")
def test_each_platform_accepts_only_its_own_date_format(
    client: TestClient, case: Case, format_of: Case
) -> None:
    params = {
        case.start_param: format_of.date_text(OCT_DAYS[0]),
        case.end_param: format_of.date_text(OCT_DAYS[-1]),
    }
    response = client.get(case.report_path, params=params, headers=AUTH)
    assert response.status_code == (200 if format_of is case else 400)


@each_platform
def test_missing_date_param_is_422(client: TestClient, case: Case) -> None:
    params = {case.start_param: case.date_text(OCT_DAYS[0])}
    assert client.get(case.report_path, params=params, headers=AUTH).status_code == 422


def test_parse_date_rejects_unpadded_values() -> None:
    with pytest.raises(HTTPException) as error:
        parse_date("2026-1-5", "%Y-%m-%d")
    assert error.value.status_code == 400


def test_parse_date_range_returns_dates() -> None:
    assert parse_date_range("20261001", "20261002", "%Y%m%d") == (
        date(2026, 10, 1),
        date(2026, 10, 2),
    )


# --- fault injection ------------------------------------------------------------------------


@each_endpoint
def test_rate_limit_fault_has_retry_after(delivery_dir: Path, case: Case, kind: str) -> None:
    client = build_client(
        delivery_dir, faults_enabled=True, rate_429=1.0, rate_500=0.0, retry_after_seconds=7
    )
    response = request_endpoint(client, case, kind, headers=AUTH)
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "7"


@each_endpoint
def test_server_error_fault(delivery_dir: Path, case: Case, kind: str) -> None:
    client = build_client(delivery_dir, faults_enabled=True, rate_429=0.0, rate_500=1.0)
    assert request_endpoint(client, case, kind, headers=AUTH).status_code == 500


@each_endpoint
def test_off_switch_disables_faults(delivery_dir: Path, case: Case, kind: str) -> None:
    client = build_client(delivery_dir, faults_enabled=False, rate_429=0.5, rate_500=0.5)
    for _ in range(20):
        assert request_endpoint(client, case, kind, headers=AUTH).status_code == 200


def test_mixed_rates_produce_every_status(delivery_dir: Path) -> None:
    client = build_client(delivery_dir, faults_enabled=True, rate_429=0.3, rate_500=0.3)
    assert set(status_sequence(client, META, 60)) == {200, 429, 500}


def test_same_seed_gives_the_same_statuses(delivery_dir: Path) -> None:
    fault_settings = {"faults_enabled": True, "rate_429": 0.3, "rate_500": 0.3, "fault_seed": 7}
    first = build_client(delivery_dir, **fault_settings)
    second = build_client(delivery_dir, **fault_settings)
    assert status_sequence(first, GOOGLE, 40) == status_sequence(second, GOOGLE, 40)


def test_different_seed_changes_the_statuses(delivery_dir: Path) -> None:
    fault_settings = {"faults_enabled": True, "rate_429": 0.3, "rate_500": 0.3}
    first = build_client(delivery_dir, fault_seed=1, **fault_settings)
    second = build_client(delivery_dir, fault_seed=2, **fault_settings)
    assert status_sequence(first, GOOGLE, 40) != status_sequence(second, GOOGLE, 40)


def test_platforms_have_independent_fault_sequences(delivery_dir: Path) -> None:
    fault_settings = {"faults_enabled": True, "rate_429": 0.3, "rate_500": 0.3}
    alone = build_client(delivery_dir, **fault_settings)
    interleaved = build_client(delivery_dir, **fault_settings)
    expected = status_sequence(alone, META, 30)
    observed = []
    for _ in range(30):
        status_sequence(interleaved, TIKTOK, 2)
        observed.extend(status_sequence(interleaved, META, 1))
    assert observed == expected


def test_retry_after_a_fault_returns_the_same_page(delivery_dir: Path) -> None:
    client = build_client(delivery_dir, faults_enabled=True, rate_429=0.5, rate_500=0.0)
    bodies = []
    for _ in range(30):
        response = get_report(client, META, limit=3)
        if response.status_code == 200:
            bodies.append(response.json())
    assert len(bodies) > 1
    assert all(body == bodies[0] for body in bodies)


def test_fault_injector_disabled_returns_none() -> None:
    injector = FaultInjector(enabled=False, seed=1, rate_429=1.0, rate_500=0.0)
    assert injector.next_status("meta") is None


# --- settings -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rates", [{"rate_429": 0.7, "rate_500": 0.5}, {"rate_429": -0.1}, {"rate_500": -0.1}]
)
def test_invalid_fault_rates_are_rejected(tmp_path: Path, rates: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        build_settings(tmp_path, **rates)


def test_token_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MOCK_PLATFORMS_TOKEN", raising=False)
    with pytest.raises(ValidationError):
        MockPlatformsSettings(_env_file=None)


def test_empty_token_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        build_settings(tmp_path, token="")


# --- isolation and startup ------------------------------------------------------------------


def test_answer_key_files_are_never_parsed(delivery_dir: Path) -> None:
    (delivery_dir.parent / "answer_key.json").write_text("{not json", encoding="utf-8")
    (delivery_dir / "answer_key.json").write_text("{not json", encoding="utf-8")
    client = build_client(delivery_dir)
    assert get_report(client, META).status_code == 200


def mock_platforms_sources() -> list[Path]:
    return sorted(Path(mock_platforms.__file__).parent.glob("*.py"))


def test_package_has_no_database_or_app_imports() -> None:
    forbidden = {"app", "sqlalchemy", "psycopg", "alembic"}
    for path in mock_platforms_sources():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert not {name.split(".")[0] for name in names} & forbidden, path


def test_package_never_mentions_the_answer_key() -> None:
    for path in mock_platforms_sources():
        assert "answer_key" not in path.read_text(encoding="utf-8"), path


def test_missing_delivery_dir_fails_at_startup(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="make generate"):
        create_app(build_settings(tmp_path / "absent"))


def test_load_store_keeps_money_as_decimal(delivery_dir: Path) -> None:
    account = load_store(delivery_dir)["meta"]["1001"]
    assert account.rows[0].spend == Decimal("100.45")
    assert isinstance(account.rows[0].spend, Decimal)


def test_rows_between_filters_by_date(delivery_dir: Path) -> None:
    account = load_store(delivery_dir)["meta"]["1001"]
    rows = account.rows_between(OCT_DAYS[0], OCT_DAYS[0])
    assert {row.date for row in rows} == {OCT_DAYS[0]}


def test_offset_token_round_trips() -> None:
    assert decode_offset(encode_offset(42)) == 42


def test_entrypoint_serves_on_the_configured_port(
    delivery_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MOCK_PLATFORMS_TOKEN", TOKEN)
    monkeypatch.setenv("MOCK_PLATFORMS_PORT", "8123")
    monkeypatch.setenv("MOCK_PLATFORMS_DELIVERY_DIR", str(delivery_dir))
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))
    entrypoint.main()
    assert calls == [{"host": "127.0.0.1", "port": 8123}]
