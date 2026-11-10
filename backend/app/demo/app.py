"""App factory for the public demo API: GET only, synthetic data, read-only (decision 035).

Run with `make demo-api`; separate from any other Matchbook app.
"""

import logging
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

from app.demo import queries
from app.demo.db import create_demo_engine
from app.demo.eval_results import (
    DEFAULT_RESULTS_PATH,
    ResultRow,
    ResultsUnavailableError,
    read_results,
)
from app.demo.schemas import (
    EvalRow,
    EvalSummary,
    Health,
    Issue,
    IssuePage,
    RunStatus,
    Step,
    Summary,
    TypeSummary,
    money,
)
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from starlette.routing import BaseRoute

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 50
MAX_OFFSET = 10_000
MAX_FILTER_LENGTH = 64
READ_METHODS = frozenset({"GET", "HEAD"})

DESCRIPTION = """
Live demo of **Matchbook**, three-way media reconciliation for agencies
(plan ↔ platform delivery ↔ invoice).

* **Synthetic data only.** Every client, campaign, invoice and amount is generated; nothing
  belongs to a real company.
* **Read-only.** Only `GET` endpoints exist. The API logs in as a database role that can read
  two curated views and nothing else, in read-only transactions with a short query timeout.
* Money is EUR as a decimal string (`"1234.50"`), never a float.
* Pages hold at most 100 issues.
"""


def non_get_routes(app: FastAPI) -> list[str]:
    """Routes that accept any method other than GET/HEAD; must be empty for the demo."""
    return [
        f"{sorted(methods)} {getattr(route, 'path', '?')}"
        for route in app.routes
        if (methods := route_methods(route)) - READ_METHODS
    ]


def route_methods(route: BaseRoute) -> set[str]:
    methods = getattr(route, "methods", None)
    # A route without a method list (e.g. a websocket or a mount) is not a GET route either.
    return set(methods) if methods is not None else {"NON-HTTP"}


@contextmanager
def connection(request: Request) -> Iterator[Connection]:
    """Opened inside the handler, after FastAPI has validated the parameters."""
    state = request.app.state
    if state.engine is None:
        state.engine = create_demo_engine()
        state.owns_engine = True
    with state.engine.connect() as conn, conn.begin():
        yield conn


# Registered on the app itself in `create_demo_app`, so `non_get_routes` sees every route.
ROUTES: list[tuple[str, Callable[..., Any], dict[str, Any]]] = []


def get(path: str, **options: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def register(endpoint: Callable[..., Any]) -> Callable[..., Any]:
        ROUTES.append((path, endpoint, options))
        return endpoint

    return register


@get("/health", response_model=Health, summary="Liveness check (no database call)")
def health() -> Health:
    return Health(status="ok")


@get(
    "/api/status",
    response_model=RunStatus,
    summary="Latest finished pipeline run",
    responses={404: {"description": "No finished run yet"}},
)
def status(request: Request) -> RunStatus:
    with connection(request) as conn:
        run = queries.latest_finished_run(conn)
    if run is None:
        raise HTTPException(status_code=404, detail="no finished pipeline run")
    steps = [
        Step(
            name=step["name"],
            status=step["status"],
            counts={key: value for key, value in step["counts"].items() if is_count(value)},
        )
        for step in run.steps
    ]
    return RunStatus(
        status=run.status, started_at=run.started_at, finished_at=run.finished_at, steps=steps
    )


def is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@get("/api/summary", response_model=Summary, summary="Open issues by type, with EUR sums")
def summary(request: Request) -> Summary:
    with connection(request) as conn:
        totals = queries.open_issue_totals(conn)
    return Summary(
        total_count=sum(total.count for total in totals),
        total_amount_eur=money(sum((total.amount for total in totals), Decimal(0))),
        by_type=[
            TypeSummary(type=total.type, count=total.count, amount_eur=money(total.amount))
            for total in totals
        ],
    )


FilterText = Annotated[str | None, Query(min_length=1, max_length=MAX_FILTER_LENGTH)]


@get("/api/issues", response_model=IssuePage, summary="Open issues, paginated")
def issues(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
    client_code: FilterText = None,
    platform: FilterText = None,
    type: FilterText = None,
    period: Annotated[
        str | None, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="Month as YYYY-MM")
    ] = None,
) -> IssuePage:
    filters = queries.IssueFilters(
        client_code=client_code, platform=platform, type=type, period=month_start(period)
    )
    with connection(request) as conn:
        rows = queries.list_open_issues(conn, filters, limit, offset)
        total = queries.count_open_issues(conn, filters)
    return IssuePage(
        total=total,
        limit=limit,
        offset=offset,
        items=[
            Issue(
                type=row.type,
                severity=row.severity,
                amount=None if row.amount is None else money(row.amount),
                explanation=row.explanation,
                proposed_action=row.proposed_action,
                period=row.period,
                client_code=row.client_code,
                platform=row.platform,
            )
            for row in rows
        ],
    )


def month_start(period: str | None) -> date | None:
    if period is None:
        return None
    year, month = period.split("-")
    return date(int(year), int(month), 1)


def eval_row(row: ResultRow) -> EvalRow:
    return EvalRow(
        error_type=row.error_type,
        planted=row.planted,
        found=row.found,
        missed=row.missed,
        expected_missed=row.expected_missed,
        false_alarms=row.false_alarms,
        eur_planted=money(row.eur_planted),
        eur_found=money(row.eur_found),
    )


@get(
    "/api/eval",
    response_model=EvalSummary,
    summary="Stage 1 results: planted errors found, counts and EUR sums only",
    responses={503: {"description": "Results file missing or unreadable"}},
)
def stage1_results(request: Request) -> EvalSummary:
    try:
        results = read_results(request.app.state.eval_results_path)
    except ResultsUnavailableError as exc:
        logger.warning("stage 1 results unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="eval results unavailable") from None
    return EvalSummary(
        result=results.result,
        by_type=[eval_row(row) for row in results.rows],
        overall=eval_row(results.overall),
    )


async def database_error(request: Request, exc: Exception) -> JSONResponse:
    logger.error("database error on %s: %s", request.url.path, type(exc).__name__)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


async def internal_error(request: Request, exc: Exception) -> JSONResponse:
    logger.error("unhandled error on %s: %s", request.url.path, type(exc).__name__)
    return JSONResponse(status_code=500, content={"detail": "internal error"})


def create_demo_app(
    engine: Engine | None = None,
    eval_results_path: Path = DEFAULT_RESULTS_PATH,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        if app.state.owns_engine and app.state.engine is not None:
            app.state.engine.dispose()

    app = FastAPI(
        title="Matchbook demo API",
        description=DESCRIPTION,
        version="1.0.0",
        debug=False,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )
    app.state.engine = engine
    app.state.owns_engine = False
    app.state.eval_results_path = eval_results_path
    for path, endpoint, options in ROUTES:
        app.add_api_route(path, endpoint, methods=["GET"], **options)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET"],
        allow_headers=[],
        allow_credentials=False,
    )
    app.add_exception_handler(SQLAlchemyError, database_error)
    app.add_exception_handler(Exception, internal_error)
    return app
