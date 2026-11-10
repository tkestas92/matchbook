"""The public demo API: GET only, money as strings, no leaks (decision 035).

DB tests use a throwaway migrated database and log in as `demo_ro`; the rest need no database.
"""

import json
import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from app.config import Settings, get_settings
from app.db.roles import MissingRolePasswordError, ensure_roles, role_passwords
from app.demo.app import create_demo_app, non_get_routes
from app.demo.db import create_demo_engine, demo_database_url
from app.demo.eval_results import (
    DEFAULT_RESULTS_PATH,
    ResultsUnavailableError,
    parse_results,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Connection, create_engine, text
from tests.test_migrations import alembic_config, insert, temporary_database

UNREACHABLE_DB = "postgresql+psycopg://nobody:nothing@127.0.0.1:1/none"
PATHS = ("/health", "/api/status", "/api/summary", "/api/issues", "/api/eval")

SAMPLE_RESULTS = """# Stage 1 eval results

- Pipeline run: `cf9f0117-e4a6-4b03-a1b4-707b673a5362` (succeeded, finished 2026-11-10)
- Answer key: 41 planted errors, seed 20260501
- Result: **PASS**

| Error type | Planted | Found | Missed | Expected missed | False alarms | € planted | € found |
|---|---:|---:|---:|---:|---:|---:|---:|
| #1 invoice_above_delivery | 4 | 4 | 0 | 0 | 0 | 2,315.70 | 2,315.70 |
| #5 wrong_markup | 3 | 3 | 0 | 0 | 0 | -1,248.07 | -1,248.07 |
| other issue types | 0 | 0 | 0 | 0 | 0 | 0.00 | 0.00 |
| **overall** | 7 | 7 | 0 | 0 | 0 | 1,067.63 | 1,067.63 |

Expected missed: errors not detectable in stage 1.
"""


def offline_app(tmp_path: Path, results: str | None = SAMPLE_RESULTS) -> FastAPI:
    path = tmp_path / "results.md"
    if results is not None:
        path.write_text(results, encoding="utf-8")
    return create_demo_app(engine=create_engine(UNREACHABLE_DB), eval_results_path=path)


# --- GET only ------------------------------------------------------------------


def test_demo_app_registers_only_get_routes(tmp_path: Path) -> None:
    assert non_get_routes(offline_app(tmp_path)) == []


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_non_get_route_is_detected(tmp_path: Path, method: str) -> None:
    app = offline_app(tmp_path)
    app.add_api_route("/api/write", lambda: None, methods=[method])
    assert non_get_routes(app) == [f"['{method}'] /api/write"]


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
@pytest.mark.parametrize("path", PATHS)
def test_write_methods_are_rejected(tmp_path: Path, method: str, path: str) -> None:
    response = getattr(TestClient(offline_app(tmp_path)), method)(path)
    assert response.status_code == 405


def test_openapi_describes_a_read_only_synthetic_demo(tmp_path: Path) -> None:
    spec = TestClient(offline_app(tmp_path)).get("/openapi.json").json()
    assert "Synthetic data only" in spec["info"]["description"]
    assert "Read-only" in spec["info"]["description"]
    assert {method for item in spec["paths"].values() for method in item} == {"get"}
    assert set(spec["paths"]) == set(PATHS)


def test_cors_allows_get_from_any_origin(tmp_path: Path) -> None:
    response = TestClient(offline_app(tmp_path)).get(
        "/health", headers={"Origin": "https://example.org"}
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in response.headers


# --- errors never leak ------------------------------------------------------------


def test_database_failure_is_a_generic_503(tmp_path: Path) -> None:
    response = TestClient(offline_app(tmp_path)).get("/api/summary")
    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable"}


def test_unhandled_error_is_a_generic_500(tmp_path: Path) -> None:
    app = offline_app(tmp_path)

    def boom() -> None:
        raise RuntimeError("SELECT secret FROM somewhere")

    app.add_api_route("/boom", boom, methods=["GET"])
    response = TestClient(app, raise_server_exceptions=False).get("/boom")
    assert response.status_code == 500
    assert response.json() == {"detail": "internal error"}


@pytest.mark.parametrize(
    "query",
    ["limit=101", "limit=0", "offset=-1", "offset=10001", "period=2026-13", "period=2026-1"],
)
def test_issue_parameters_are_bounded(tmp_path: Path, query: str) -> None:
    assert TestClient(offline_app(tmp_path)).get(f"/api/issues?{query}").status_code == 422


# --- stage 1 results ----------------------------------------------------------------


def test_eval_returns_counts_and_money_strings_only(tmp_path: Path) -> None:
    response = TestClient(offline_app(tmp_path)).get("/api/eval")
    assert response.status_code == 200
    body = response.json()
    assert body["result"] == "PASS"
    assert body["by_type"][0] == {
        "error_type": "#1 invoice_above_delivery",
        "planted": 4,
        "found": 4,
        "missed": 0,
        "expected_missed": 0,
        "false_alarms": 0,
        "eur_planted": "2315.70",
        "eur_found": "2315.70",
    }
    assert body["by_type"][1]["eur_planted"] == "-1248.07"
    assert body["overall"]["planted"] == 7
    assert body["overall"]["eur_found"] == "1067.63"
    raw = response.text
    assert "cf9f0117" not in raw and "20260501" not in raw


@pytest.mark.parametrize(
    "results",
    [
        None,
        "",
        SAMPLE_RESULTS.replace("- Result: **PASS**", ""),
        SAMPLE_RESULTS.replace("| 2,315.70 |", "| 2315.7 |", 1),
        SAMPLE_RESULTS.replace("| **overall** |", "| total |"),
        SAMPLE_RESULTS.replace("| #1 invoice_above_delivery |", "| client LT-01 |"),
        SAMPLE_RESULTS.replace("| 4 | 4 | 0 |", "| 4 | 4 |", 1),
    ],
    ids=["missing", "empty", "no verdict", "bad money", "no overall", "bad label", "short row"],
)
def test_eval_refuses_unexpected_files(tmp_path: Path, results: str | None) -> None:
    response = TestClient(offline_app(tmp_path, results)).get("/api/eval")
    assert response.status_code == 503
    assert response.json() == {"detail": "eval results unavailable"}


def test_committed_results_file_parses() -> None:
    """The real `docs/eval-results.md` format (decision 033) is what the parser expects."""
    assert DEFAULT_RESULTS_PATH.parts[-2:] == ("docs", "eval-results.md")
    results = parse_results(DEFAULT_RESULTS_PATH.read_text(encoding="utf-8"))
    assert results.result in {"PASS", "FAIL"}
    assert results.overall.planted == sum(row.planted for row in results.rows)


def test_parser_rejects_a_missing_table() -> None:
    with pytest.raises(ResultsUnavailableError):
        parse_results("- Result: **PASS**\n")


# --- demo_ro login -------------------------------------------------------------------


def test_demo_url_requires_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    monkeypatch.delenv("DEMO_RO_DB_PASSWORD", raising=False)
    with pytest.raises(MissingRolePasswordError, match="DEMO_RO_DB_PASSWORD"):
        demo_database_url(Settings(_env_file=None))


def test_demo_url_logs_in_as_demo_ro(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://owner:secret@localhost/db")
    monkeypatch.setenv("DEMO_RO_DB_PASSWORD", "demo-pw")
    url = demo_database_url(Settings(_env_file=None))
    assert (url.username, url.password, url.database) == ("demo_ro", "demo-pw", "db")


# --- against a throwaway database -------------------------------------------------------

RUN_DETAILS_LEAKS = ("/srv/secret/data_dir", "ValueError: leaked error text", "duration_s")


def seed(conn: Connection) -> None:
    run_id = uuid.uuid4()
    meta = insert(
        conn,
        "INSERT INTO core.platforms (name, timezone, metric_map) "
        "VALUES ('meta', 'Europe/Vilnius', '{}') RETURNING id",
    )
    google = insert(
        conn,
        "INSERT INTO core.platforms (name, timezone, metric_map) "
        "VALUES ('google', 'Europe/Vilnius', '{}') RETURNING id",
    )
    clients = {
        code: insert(
            conn,
            "INSERT INTO core.clients (code, name, industry, billing_model) "
            "VALUES (:c, :n, 'retail', 'client_owned') RETURNING id",
            c=code,
            n=f"Client {code}",
        )
        for code in ("AAA", "BBB")
    }
    issues = [
        ("k1", "reconciliation_overcharge", "AAA", meta, "2026-10-01", "100.10", "open"),
        ("k2", "reconciliation_overcharge", "BBB", google, "2026-09-01", "50.05", "open"),
        ("k3", "quality_missing_days", "AAA", google, "2026-10-01", None, "open"),
        ("k4", "reconciliation_wrong_markup", "BBB", None, "2026-10-01", "-20.00", "open"),
        ("k5", "reconciliation_overcharge", "AAA", meta, "2026-08-01", "999.99", "approved"),
    ]
    for key, type_, client, platform, period, amount, status in issues:
        conn.execute(
            text(
                "INSERT INTO app.issues (dedup_key, type, severity, client_id, platform_id, "
                "period, entity_type, entity_id, amount, explanation, proposed_action, status, "
                "run_id) VALUES (:k, :t, 'high', :c, :p, :period, 'invoice', 'SECRET-ENTITY', "
                ":a, :e, 'Check it', :s, :r)"
            ),
            {
                "k": key,
                "t": type_,
                "c": clients[client],
                "p": platform,
                "period": period,
                "a": amount,
                "e": f"Explanation {key}",
                "s": status,
                "r": run_id,
            },
        )
    steps = [
        {
            "name": "load_invoices",
            "status": "succeeded",
            "duration_s": 1.5,
            "counts": {
                "invoices_written": 12,
                "rejected_list": ["/srv/secret/data_dir/invoices/x.json"],
            },
        },
        {
            "name": "pacing",
            "status": "succeeded",
            "duration_s": 0.5,
            "counts": {"as_of": "2026-10-31", "statuses": {"green": 3}, "issues_written": 2},
        },
    ]
    runs = [
        ("succeeded", "2026-11-01T03:00:00Z", "2026-11-01T03:10:00Z", steps),
        ("failed", "2026-11-02T03:00:00Z", "2026-11-02T03:05:00Z", steps),
        ("running", "2026-11-03T03:00:00Z", None, []),
    ]
    for status, started, finished, run_steps in runs:
        details = {
            "data_dir": "/srv/secret/data_dir",
            "steps": run_steps,
            "error": "ValueError: leaked error text",
        }
        conn.execute(
            text(
                "INSERT INTO app.pipeline_runs (status, started_at, finished_at, details) "
                "VALUES (:s, :st, :f, CAST(:d AS jsonb))"
            ),
            {"s": status, "st": started, "f": finished, "d": json.dumps(details)},
        )


@pytest.fixture(scope="module")
def client(database_url: str) -> Iterator[TestClient]:
    try:
        passwords = role_passwords(get_settings())
    except MissingRolePasswordError as exc:
        pytest.fail(str(exc))
    with temporary_database(database_url) as url:
        owner = create_engine(url)
        with owner.begin() as conn:
            ensure_roles(conn, passwords)
        command.upgrade(alembic_config(url), "head")
        with owner.begin() as conn:
            seed(conn)
        engine = create_demo_engine(url.set(username="demo_ro", password=passwords["demo_ro"]))
        try:
            yield TestClient(create_demo_app(engine=engine))
        finally:
            engine.dispose()
            owner.dispose()


def test_status_is_the_latest_finished_run_without_details_leaks(client: TestClient) -> None:
    response = client.get("/api/status")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["finished_at"].startswith("2026-11-02T03:05:00")
    assert body["steps"] == [
        {"name": "load_invoices", "status": "succeeded", "counts": {"invoices_written": 12}},
        {"name": "pacing", "status": "succeeded", "counts": {"issues_written": 2}},
    ]
    assert not [leak for leak in RUN_DETAILS_LEAKS if leak in response.text]


def test_summary_groups_open_issues_with_money_strings(client: TestClient) -> None:
    body = client.get("/api/summary").json()
    assert body == {
        "total_count": 4,
        "total_amount_eur": "130.15",
        "by_type": [
            {"type": "quality_missing_days", "count": 1, "amount_eur": "0.00"},
            {"type": "reconciliation_overcharge", "count": 2, "amount_eur": "150.15"},
            {"type": "reconciliation_wrong_markup", "count": 1, "amount_eur": "-20.00"},
        ],
    }


def test_issues_lists_open_issues_only(client: TestClient) -> None:
    body = client.get("/api/issues").json()
    assert (body["total"], body["limit"], body["offset"]) == (4, 50, 0)
    assert body["items"][0] == {
        "type": "reconciliation_overcharge",
        "severity": "high",
        "amount": "100.10",
        "explanation": "Explanation k1",
        "proposed_action": "Check it",
        "period": "2026-10-01",
        "client_code": "AAA",
        "platform": "meta",
    }
    assert body["items"][2]["amount"] is None
    assert body["items"][3]["platform"] is None
    assert "999.99" not in client.get("/api/issues").text
    assert "SECRET-ENTITY" not in client.get("/api/issues").text


@pytest.mark.parametrize(
    ("query", "explanations"),
    [
        ("client_code=AAA", ["k1", "k3"]),
        ("platform=google", ["k2", "k3"]),
        ("type=reconciliation_overcharge", ["k1", "k2"]),
        ("period=2026-10", ["k1", "k3", "k4"]),
        ("client_code=BBB&period=2026-10", ["k4"]),
        ("client_code=nobody", []),
    ],
)
def test_issue_filters(client: TestClient, query: str, explanations: list[str]) -> None:
    body = client.get(f"/api/issues?{query}").json()
    assert [item["explanation"] for item in body["items"]] == [
        f"Explanation {key}" for key in explanations
    ]
    assert body["total"] == len(explanations)


def test_issue_pagination(client: TestClient) -> None:
    body = client.get("/api/issues?limit=2&offset=1").json()
    assert body["total"] == 4
    assert [item["explanation"] for item in body["items"]] == ["Explanation k2", "Explanation k3"]


def test_money_is_never_a_json_number(client: TestClient) -> None:
    for path in ("/api/summary", "/api/issues"):
        body = client.get(path).json(parse_float=Decimal)
        assert not any(isinstance(value, Decimal) for value in walk(body))


def walk(value: object) -> Iterator[object]:
    if isinstance(value, dict):
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)
    else:
        yield value
