"""Pipeline step 6: reconcile plan, delivery and invoices; run it after quality and both loaders.

The rules in `app.reconciliation` are pure. This step reads `core`, writes `core` as `etl` and
`app.issues` as `app`. The two writes are separate transactions, so a failure between them is
repaired by running the step again.
"""

import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from app.config import get_settings
from app.db.models.app import Issue
from app.db.models.core import (
    AdAccount,
    Campaign,
    Client,
    CreditNote,
    DailyDelivery,
    InsertionOrder,
    InvoiceLine,
    Platform,
    ReconciliationResult,
)
from app.db.models.core import Invoice as InvoiceRow
from app.db.session import create_app_engine, create_etl_engine
from app.reconciliation.models import (
    BillingTerms,
    BoundaryShift,
    Budget,
    DeliveryGap,
    DeliveryRow,
    GapNote,
    Invoice,
    IssueDraft,
    Line,
    LineLink,
    PlanOrder,
    ReconciliationInput,
    SlotResult,
)
from app.reconciliation.models import CreditNote as ReconciliationCreditNote
from app.reconciliation.rules import run_rules
from sqlalchemy import Connection, Engine, bindparam, select, update
from sqlalchemy.orm import aliased

from pipelines.load_plans import client_ids, platform_ids
from pipelines.normalize import DeliveryRecord, upsert
from pipelines.quality import MONTH_BOUNDARY_SHIFT, check_missing_days, dedup_key

RESULT_KEY = ("period", "client_id", "platform_id")
RESULT_COLUMNS = (
    "planned",
    "delivered",
    "invoiced",
    "client_invoiced",
    "expected_client_amount",
    "variance",
    "status",
    "run_id",
)
ISSUE_COLUMNS = ("type", "severity", "amount", "explanation", "proposed_action", "run_id")

CampaignKey = tuple[str, str]


@dataclass
class ReconcileResult:
    run_id: uuid.UUID
    lines_linked: int = 0
    results_written: int = 0
    issues_written: int = 0
    gap_notes_attached: int = 0
    gap_notes_unattached: int = 0  # A variance with missing days but no quality issue to hold it.


def load_delivery(conn: Connection) -> tuple[list[DeliveryRecord], dict[CampaignKey, str]]:
    """Every stored day, and each campaign's client code."""
    query = (
        select(
            Client.code,
            Platform.name,
            AdAccount.external_account_id,
            AdAccount.timezone,
            Campaign.platform_campaign_id,
            Campaign.name.label("campaign_name"),
            DailyDelivery.date,
            DailyDelivery.spend,
            DailyDelivery.impressions,
            DailyDelivery.clicks,
            DailyDelivery.conversions,
        )
        .join(Campaign, Campaign.id == DailyDelivery.campaign_id)
        .join(AdAccount, AdAccount.id == Campaign.ad_account_id)
        .join(Client, Client.id == AdAccount.client_id)
        .join(Platform, Platform.id == Campaign.platform_id)
    )
    records: list[DeliveryRecord] = []
    clients: dict[CampaignKey, str] = {}
    for row in conn.execute(query):
        records.append(
            DeliveryRecord(
                platform=row.name,
                account_id=row.external_account_id,
                timezone=row.timezone,
                campaign_id=row.platform_campaign_id,
                campaign_name=row.campaign_name,
                date=row.date,
                spend=row.spend,
                impressions=row.impressions,
                clicks=row.clicks,
                conversions=row.conversions,
            )
        )
        clients[(row.name, row.platform_campaign_id)] = row.code
    return records, clients


def delivery_rows(
    records: Iterable[DeliveryRecord], clients: dict[CampaignKey, str]
) -> list[DeliveryRow]:
    return [
        DeliveryRow(
            client=clients[(r.platform, r.campaign_id)],
            platform=r.platform,
            campaign_id=r.campaign_id,
            day=r.date,
            spend=r.spend,
        )
        for r in records
        if r.spend is not None
    ]


def delivery_evidence(
    records: Sequence[DeliveryRecord], clients: dict[CampaignKey, str]
) -> tuple[list[BoundaryShift], list[DeliveryGap]]:
    """Quality's own gap check, so a problem cannot be defined two ways or keyed two ways."""
    by_day = {(r.platform, r.campaign_id, r.date): r for r in records}
    shifts: list[BoundaryShift] = []
    gaps: list[DeliveryGap] = []
    for finding in check_missing_days(records):
        client = clients[(finding.platform, finding.campaign_id)]
        if finding.check == MONTH_BOUNDARY_SHIFT:
            row_day = finding.date_to + timedelta(days=1)
            moved = by_day[(finding.platform, finding.campaign_id, row_day)]
            shifts.append(
                BoundaryShift(
                    client=client,
                    platform=finding.platform,
                    timezone=moved.timezone,
                    campaign_id=finding.campaign_id,
                    campaign_name=finding.campaign_name,
                    missing_day=finding.date_from,
                    row_day=row_day,
                    spend=moved.spend,
                    issue_key=dedup_key(finding),
                )
            )
        else:
            gaps.append(
                DeliveryGap(
                    client=client,
                    platform=finding.platform,
                    campaign_id=finding.campaign_id,
                    campaign_name=finding.campaign_name,
                    first_day=finding.date_from,
                    last_day=finding.date_to,
                    issue_key=dedup_key(finding),
                )
            )
    return shifts, gaps


def load_invoices(conn: Connection) -> list[Invoice]:
    lines: dict[int, list[Line]] = defaultdict(list)
    query = select(
        InvoiceLine.id,
        InvoiceLine.invoice_id,
        InvoiceLine.line_no,
        InvoiceLine.campaign_ref,
        InvoiceLine.amount,
    ).order_by(InvoiceLine.invoice_id, InvoiceLine.line_no)
    for row in conn.execute(query):
        lines[row.invoice_id].append(Line(row.id, row.line_no, row.campaign_ref, row.amount))
    invoices = (
        select(
            InvoiceRow.id,
            InvoiceRow.kind,
            InvoiceRow.vendor,
            InvoiceRow.number,
            Client.code,
            Platform.name,
            InvoiceRow.issue_date,
            InvoiceRow.period_start,
            InvoiceRow.period_end,
        )
        .join(Client, Client.id == InvoiceRow.client_id)
        .outerjoin(Platform, Platform.id == InvoiceRow.platform_id)
    )
    return [
        Invoice(
            kind=row.kind,
            vendor=row.vendor,
            number=row.number,
            client=row.code,
            platform=row.name,
            issue_date=row.issue_date,
            period_start=row.period_start,
            period_end=row.period_end,
            lines=tuple(lines[row.id]),
        )
        for row in conn.execute(invoices)
    ]


def load_orders(conn: Connection) -> list[PlanOrder]:
    query = (
        select(
            InsertionOrder.id,
            Client.code,
            Platform.name,
            InsertionOrder.campaign_ref,
            Campaign.id.label("campaign_id"),
        )
        .join(Client, Client.id == InsertionOrder.client_id)
        .join(Platform, Platform.id == InsertionOrder.platform_id)
        .outerjoin(Campaign, Campaign.io_id == InsertionOrder.id)
    )
    orders: dict[int, list[Any]] = defaultdict(list)
    for row in conn.execute(query):
        orders[row.id].append(row)
    return [
        PlanOrder(
            client=group[0].code,
            platform=group[0].name,
            campaign_ref=group[0].campaign_ref,
            campaign_ids=tuple(r.campaign_id for r in group if r.campaign_id is not None),
        )
        for group in orders.values()
    ]


def load_budgets(conn: Connection) -> list[Budget]:
    query = (
        select(
            Client.code,
            Platform.name,
            InsertionOrder.start_date,
            InsertionOrder.end_date,
            InsertionOrder.budget,
        )
        .join(Client, Client.id == InsertionOrder.client_id)
        .join(Platform, Platform.id == InsertionOrder.platform_id)
    )
    return [
        Budget(row.code, row.name, row.start_date, row.end_date, row.budget)
        for row in conn.execute(query)
    ]


def load_billing(conn: Connection) -> dict[str, BillingTerms]:
    query = select(Client.code, Client.billing_model, Client.markup_pct, Client.mgmt_fee)
    return {
        row.code: BillingTerms(row.billing_model, row.markup_pct, row.mgmt_fee)
        for row in conn.execute(query)
    }


def load_credit_notes(conn: Connection) -> list[ReconciliationCreditNote]:
    original = aliased(InvoiceRow)
    query = select(
        CreditNote.vendor,
        CreditNote.number,
        original.vendor.label("original_vendor"),
        original.number.label("original_number"),
        CreditNote.amount,
    ).join(original, original.id == CreditNote.original_invoice_id)
    return [
        ReconciliationCreditNote(
            vendor=row.vendor,
            number=row.number,
            original=(row.original_vendor, row.original_number),
            amount=row.amount,
        )
        for row in conn.execute(query)
    ]


def load_input(conn: Connection, agency_timezone: str) -> ReconciliationInput:
    records, clients = load_delivery(conn)
    shifts, gaps = delivery_evidence(records, clients)
    return ReconciliationInput(
        agency_timezone=agency_timezone,
        billing=load_billing(conn),
        delivery=delivery_rows(records, clients),
        shifts=shifts,
        gaps=gaps,
        invoices=load_invoices(conn),
        orders=load_orders(conn),
        budgets=load_budgets(conn),
        credit_notes=load_credit_notes(conn),
    )


def write_links(conn: Connection, run_id: uuid.UUID, links: Sequence[LineLink]) -> None:
    """Only found links are written, so a line a person linked later is never cleared."""
    if links:
        conn.execute(
            update(InvoiceLine)
            .where(InvoiceLine.id == bindparam("target_id"))
            .values(campaign_id=bindparam("target_campaign"), run_id=run_id),
            [{"target_id": link.line_id, "target_campaign": link.campaign_id} for link in links],
        )


def write_results(conn: Connection, run_id: uuid.UUID, results: Sequence[SlotResult]) -> None:
    clients, platforms = client_ids(conn), platform_ids(conn)
    rows = [
        {
            "period": result.slot.period,
            "client_id": clients[result.slot.client],
            "platform_id": platforms[result.slot.platform],
            "planned": result.planned,
            "delivered": result.delivered,
            "invoiced": result.invoiced,
            "client_invoiced": result.client_invoiced,
            "expected_client_amount": result.expected_client_amount,
            "variance": result.variance,
            "status": result.status,
            "run_id": run_id,
        }
        for result in results
    ]
    upsert(conn, ReconciliationResult, rows, RESULT_KEY, RESULT_COLUMNS)


def issue_row(
    run_id: uuid.UUID, draft: IssueDraft, clients: dict[str, int], platforms: dict[str, int]
) -> dict[str, Any]:
    return {
        "dedup_key": draft.dedup_key,
        "type": draft.type,
        "severity": draft.severity,
        "client_id": clients[draft.client],
        "platform_id": None if draft.platform is None else platforms[draft.platform],
        "period": draft.period,
        "entity_type": draft.entity_type,
        "entity_id": draft.entity_id,
        "amount": draft.amount,
        "explanation": draft.explanation,
        "proposed_action": draft.proposed_action,
        "run_id": run_id,
    }


def attach_gap_note(conn: Connection, run_id: uuid.UUID, note: GapNote) -> bool:
    """Update the quality issue only; a gap with no such issue must not create a second one."""
    statement = (
        update(Issue)
        .where(Issue.dedup_key == note.issue_key)
        .values(amount=note.amount, explanation=note.explanation, run_id=run_id)
    )
    return conn.execute(statement).rowcount == 1


def write_issues(
    result: ReconcileResult,
    issues: Sequence[IssueDraft],
    notes: Sequence[GapNote],
    engine: Engine,
) -> None:
    """Upsert into `app.issues`; status and decision columns are never updated."""
    with engine.begin() as conn:
        clients, platforms = client_ids(conn), platform_ids(conn)
        rows = [issue_row(result.run_id, draft, clients, platforms) for draft in issues]
        upsert(conn, Issue, rows, ("dedup_key",), ISSUE_COLUMNS)
        attached = sum(attach_gap_note(conn, result.run_id, note) for note in notes)
    result.issues_written = len(rows)
    result.gap_notes_attached = attached
    result.gap_notes_unattached = len(notes) - attached


def reconcile(
    run_id: uuid.UUID, etl_engine: Engine, app_engine: Engine, agency_timezone: str
) -> ReconcileResult:
    result = ReconcileResult(run_id=run_id)
    with etl_engine.begin() as conn:
        outcome = run_rules(load_input(conn, agency_timezone))
        write_links(conn, run_id, outcome.links)
        write_results(conn, run_id, outcome.results)
    result.lines_linked = len(outcome.links)
    result.results_written = len(outcome.results)
    write_issues(result, outcome.issues, outcome.gap_notes, app_engine)
    return result


def reconcile_from_env(run_id: uuid.UUID) -> ReconcileResult:
    etl_engine = create_etl_engine()
    app_engine = create_app_engine()
    try:
        return reconcile(run_id, etl_engine, app_engine, get_settings().agency_timezone)
    finally:
        app_engine.dispose()
        etl_engine.dispose()
