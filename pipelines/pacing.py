"""Pipeline step 7: pacing per insertion order; run it after quality and reconcile.

The rules in `app.reconciliation.pacing` are pure. This step logs in as `app` only: it reads
`core` and writes `app.issues`. Pacing runs as of an explicit date, by default the last date
with delivery, never the clock.
"""

import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from typing import Any

from app.db.models.app import Issue
from app.db.models.core import Campaign, Client, DailyDelivery, InsertionOrder, Platform
from app.db.session import create_app_engine
from app.reconciliation.models import PacingDraft, PacingOrder, ZeroSpendRun
from app.reconciliation.pacing import run_pacing
from sqlalchemy import Connection, Engine, func, select

from pipelines.load_plans import client_ids, platform_ids
from pipelines.normalize import DeliveryRecord, upsert
from pipelines.quality import check_zero_spend, dedup_key
from pipelines.reconcile import ISSUE_COLUMNS, issue_row, load_delivery

CampaignKey = tuple[str, str]  # (platform, platform_campaign_id)


@dataclass
class PaceResult:
    run_id: uuid.UUID
    as_of: date | None  # None only when there is no delivery and no date was given.
    orders_paced: int = 0
    orders_skipped: int = 0  # No linked campaign, or no budget.
    statuses: dict[str, int] = field(default_factory=dict)  # Current status per order.
    issues_written: int = 0
    quality_issues_taken_over: int = 0


@dataclass(frozen=True)
class OrderRow:
    client: str
    platform: str
    campaign_ref: str
    start_date: date
    end_date: date
    budget: Decimal
    campaigns: tuple[str, ...]  # Linked platform campaign ids.


def last_delivered_date(conn: Connection) -> date | None:
    return conn.execute(select(func.max(DailyDelivery.date))).scalar_one()


def load_order_rows(conn: Connection) -> list[OrderRow]:
    query = (
        select(
            InsertionOrder.id,
            Client.code,
            Platform.name,
            InsertionOrder.campaign_ref,
            InsertionOrder.start_date,
            InsertionOrder.end_date,
            InsertionOrder.budget,
            Campaign.platform_campaign_id,
        )
        .join(Client, Client.id == InsertionOrder.client_id)
        .join(Platform, Platform.id == InsertionOrder.platform_id)
        .outerjoin(Campaign, Campaign.io_id == InsertionOrder.id)
        .order_by(InsertionOrder.id)
    )
    grouped: dict[int, list[Any]] = defaultdict(list)
    for row in conn.execute(query):
        grouped[row.id].append(row)
    return [
        OrderRow(
            client=rows[0].code,
            platform=rows[0].name,
            campaign_ref=rows[0].campaign_ref,
            start_date=rows[0].start_date,
            end_date=rows[0].end_date,
            budget=rows[0].budget,
            campaigns=tuple(r.platform_campaign_id for r in rows if r.platform_campaign_id),
        )
        for rows in grouped.values()
    ]


def zero_spend_runs(records: Iterable[DeliveryRecord]) -> dict[CampaignKey, list[ZeroSpendRun]]:
    """Quality's own check and key, so a run cannot be defined or keyed two ways."""
    runs: dict[CampaignKey, list[ZeroSpendRun]] = defaultdict(list)
    for finding in check_zero_spend(records):
        if finding.campaign_id is None or finding.date_from is None or finding.date_to is None:
            continue
        runs[(finding.platform, finding.campaign_id)].append(
            ZeroSpendRun(finding.date_from, finding.date_to, dedup_key(finding))
        )
    return runs


def pacing_orders(
    order_rows: Iterable[OrderRow], records: Sequence[DeliveryRecord]
) -> list[PacingOrder]:
    """Each order with its linked campaigns' spend per day inside its own dates."""
    by_campaign: dict[CampaignKey, list[DeliveryRecord]] = defaultdict(list)
    for record in records:
        if record.spend is not None:
            by_campaign[(record.platform, record.campaign_id)].append(record)
    runs = zero_spend_runs(records)
    orders = []
    for row in order_rows:
        keys = [(row.platform, campaign) for campaign in row.campaigns]
        spend: dict[date, Decimal] = defaultdict(Decimal)
        for key in keys:
            for record in by_campaign.get(key, []):
                if row.start_date <= record.date <= row.end_date and record.spend is not None:
                    spend[record.date] += record.spend
        orders.append(
            PacingOrder(
                client=row.client,
                platform=row.platform,
                campaign_ref=row.campaign_ref,
                start_date=row.start_date,
                end_date=row.end_date,
                budget=row.budget,
                campaign_count=len(keys),
                spend=dict(spend),
                zero_spend_runs=tuple(run for key in keys for run in runs.get(key, [])),
            )
        )
    return orders


def existing_keys(conn: Connection, keys: Sequence[str]) -> set[str]:
    if not keys:
        return set()
    return set(conn.scalars(select(Issue.dedup_key).where(Issue.dedup_key.in_(keys))))


def write_issues(conn: Connection, result: PaceResult, drafts: Sequence[PacingDraft]) -> None:
    """Take over the quality zero-spend issue unless pacing already has its own issue.

    Status and decision columns are never updated, so a re-run keeps a human's decision.
    """
    own = existing_keys(conn, [draft.issue.dedup_key for draft in drafts])
    clients, platforms = client_ids(conn), platform_ids(conn)
    rows = []
    for draft in drafts:
        issue = draft.issue
        if draft.quality_key is not None and issue.dedup_key not in own:
            issue = replace(issue, dedup_key=draft.quality_key)
            result.quality_issues_taken_over += 1
        rows.append(issue_row(result.run_id, issue, clients, platforms))
    upsert(conn, Issue, rows, ("dedup_key",), ISSUE_COLUMNS)
    result.issues_written = len(rows)


def pace(run_id: uuid.UUID, engine: Engine, as_of: date | None = None) -> PaceResult:
    """`engine` must log in as `app`; `as_of` defaults to the last date with delivery."""
    with engine.begin() as conn:
        as_of = as_of or last_delivered_date(conn)
        result = PaceResult(run_id=run_id, as_of=as_of)
        if as_of is None:
            return result
        records, _ = load_delivery(conn)
        outcome = run_pacing(pacing_orders(load_order_rows(conn), records), as_of)
        write_issues(conn, result, outcome.drafts)
    result.orders_paced = len(outcome.results)
    result.orders_skipped = len(outcome.skipped)
    result.statuses = dict(
        Counter(r.current.status for r in outcome.results if r.current is not None)
    )
    return result


def pace_from_env(run_id: uuid.UUID, as_of: date | None = None) -> PaceResult:
    engine = create_app_engine()
    try:
        return pace(run_id, engine, as_of)
    finally:
        engine.dispose()
