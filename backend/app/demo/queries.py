"""Queries over the two demo views; the demo reads nothing else (decision 035)."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, Select, column, func, select, table
from sqlalchemy.dialects.postgresql import JSONB

ISSUES = table(
    "v_issues",
    column("id"),
    column("type"),
    column("severity"),
    column("amount"),
    column("explanation"),
    column("proposed_action"),
    column("period"),
    column("client_code"),
    column("platform"),
    schema="demo",
)

PIPELINE_RUNS = table(
    "v_pipeline_runs",
    column("status"),
    column("started_at"),
    column("finished_at"),
    column("steps", JSONB),
    schema="demo",
)

ZERO = Decimal("0.00")


@dataclass(frozen=True)
class IssueFilters:
    client_code: str | None = None
    platform: str | None = None
    type: str | None = None
    period: date | None = None


@dataclass(frozen=True)
class RunRow:
    status: str
    started_at: datetime
    finished_at: datetime
    steps: list[dict[str, Any]]


@dataclass(frozen=True)
class TypeTotal:
    type: str
    count: int
    amount: Decimal


@dataclass(frozen=True)
class IssueRow:
    type: str
    severity: str
    amount: Decimal | None
    explanation: str
    proposed_action: str | None
    period: date | None
    client_code: str | None
    platform: str | None


def latest_finished_run(conn: Connection) -> RunRow | None:
    query = (
        select(PIPELINE_RUNS)
        .where(PIPELINE_RUNS.c.finished_at.is_not(None))
        .order_by(PIPELINE_RUNS.c.finished_at.desc())
        .limit(1)
    )
    row = conn.execute(query).one_or_none()
    return RunRow(**row._mapping) if row else None


def open_issue_totals(conn: Connection) -> list[TypeTotal]:
    query = (
        select(
            ISSUES.c.type,
            func.count().label("count"),
            func.coalesce(func.sum(ISSUES.c.amount), ZERO).label("amount"),
        )
        .group_by(ISSUES.c.type)
        .order_by(ISSUES.c.type)
    )
    return [TypeTotal(**row._mapping) for row in conn.execute(query)]


def filtered(query: Select, filters: IssueFilters) -> Select:
    for name in ("client_code", "platform", "type", "period"):
        value = getattr(filters, name)
        if value is not None:
            query = query.where(ISSUES.c[name] == value)
    return query


def count_open_issues(conn: Connection, filters: IssueFilters) -> int:
    return conn.execute(filtered(select(func.count()).select_from(ISSUES), filters)).scalar_one()


def list_open_issues(
    conn: Connection, filters: IssueFilters, limit: int, offset: int
) -> list[IssueRow]:
    columns = [ISSUES.c[name] for name in IssueRow.__dataclass_fields__]
    query = filtered(select(*columns), filters).order_by(ISSUES.c.id).limit(limit).offset(offset)
    return [IssueRow(**row._mapping) for row in conn.execute(query)]
