"""Pipeline step 3: data-quality checks on normalized delivery, and their route to `app.issues`."""

import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import batched, pairwise
from typing import Any

from app.db.models.app import Issue
from app.db.models.core import AdAccount, Platform
from sqlalchemy import Connection, Engine, select
from sqlalchemy.dialects.postgresql import insert

from pipelines.normalize import DeliveryRecord, NormalizeResult

MISSING_DAYS = "missing_days"
MONTH_BOUNDARY_SHIFT = "month_boundary_shift"
NULL_VALUES = "null_values"
NEGATIVE_VALUES = "negative_values"
ZERO_SPEND = "zero_spend"
IMPRESSIONS_BELOW_CLICKS = "impressions_below_clicks"
REJECTED_ROWS = "rejected_rows"
UNKNOWN_ACCOUNT = "unknown_account"

SEVERITIES = {
    MISSING_DAYS: "high",
    MONTH_BOUNDARY_SHIFT: "low",
    NULL_VALUES: "high",
    NEGATIVE_VALUES: "high",
    ZERO_SPEND: "medium",
    IMPRESSIONS_BELOW_CLICKS: "medium",
    REJECTED_ROWS: "high",
    UNKNOWN_ACCOUNT: "high",
}

PROPOSED_ACTIONS = {
    MISSING_DAYS: "Re-fetch this account for the dates and ask the platform if they stay missing.",
    MONTH_BOUNDARY_SHIFT: "Check the account's timezone against the invoice before month close.",
    NULL_VALUES: "Re-fetch this account for the dates; these days are not loaded until fixed.",
    NEGATIVE_VALUES: "Check with the platform whether these are refunds or reporting errors.",
    ZERO_SPEND: "Check with the planner whether the campaign was paused on purpose.",
    IMPRESSIONS_BELOW_CLICKS: "Re-fetch this account for the dates and compare with the platform.",
    REJECTED_ROWS: "Inspect the raw response for this account; these rows are not loaded.",
    UNKNOWN_ACCOUNT: "Add this account and its client to the reference data.",
}

METRICS = ("spend", "impressions", "clicks", "conversions")
BATCH_SIZE = 1000

CampaignRows = list[DeliveryRecord]


@dataclass(frozen=True)
class Finding:
    check: str
    severity: str
    platform: str
    account_id: str
    campaign_id: str | None
    campaign_name: str | None
    date_from: date | None
    date_to: date | None
    message: str
    # Tells apart account-level findings of one check that differ only by cause.
    reason: str | None = None


def by_campaign(records: Iterable[DeliveryRecord]) -> list[CampaignRows]:
    """Each campaign's rows in date order, campaigns in a stable order."""
    grouped: dict[tuple[str, str, str], CampaignRows] = defaultdict(list)
    for record in records:
        grouped[(record.platform, record.account_id, record.campaign_id)].append(record)
    return [sorted(grouped[key], key=lambda r: r.date) for key in sorted(grouped)]


def date_range_text(first: date, last: date) -> str:
    return first.isoformat() if first == last else f"{first.isoformat()} to {last.isoformat()}"


def campaign_finding(check: str, rows: CampaignRows, first: date, last: date, text: str) -> Finding:
    """A finding on one campaign; the earliest name identifies it even after a rename."""
    head = rows[0]
    return Finding(
        check=check,
        severity=SEVERITIES[check],
        platform=head.platform,
        account_id=head.account_id,
        campaign_id=head.campaign_id,
        campaign_name=head.campaign_name,
        date_from=first,
        date_to=last,
        message=f"{head.campaign_name}: {text} on {date_range_text(first, last)} "
        f"({head.timezone} dates).",
    )


def flagged_runs(
    rows: CampaignRows, is_flagged: Callable[[DeliveryRecord], bool]
) -> list[CampaignRows]:
    """Flagged rows grouped into runs of consecutive calendar days."""
    runs: list[CampaignRows] = []
    for row in rows:
        if not is_flagged(row):
            continue
        if runs and row.date - runs[-1][-1].date == timedelta(days=1):
            runs[-1].append(row)
        else:
            runs.append([row])
    return runs


def is_month_boundary_shift(rows: CampaignRows, before: date, after: date) -> bool:
    """Only the month's last day is absent, and the 1st of the next month is the final row."""
    return after - before == timedelta(days=2) and after.day == 1 and after == rows[-1].date


def gap_finding(rows: CampaignRows, before: date, after: date) -> Finding:
    first = before + timedelta(days=1)
    last = after - timedelta(days=1)
    if is_month_boundary_shift(rows, before, after):
        text = f"the month's last day is absent and a row is dated {after.isoformat()},"
        return campaign_finding(MONTH_BOUNDARY_SHIFT, rows, first, last, text)
    return campaign_finding(MISSING_DAYS, rows, first, last, "no delivery was reported")


def check_missing_days(records: Iterable[DeliveryRecord]) -> list[Finding]:
    """Gaps between a campaign's first and last reported day."""
    return [
        gap_finding(rows, earlier.date, later.date)
        for rows in by_campaign(records)
        for earlier, later in pairwise(rows)
        if later.date - earlier.date > timedelta(days=1)
    ]


def fields_where(run: CampaignRows, matches: Callable[[Any], bool]) -> str:
    return ", ".join(m for m in METRICS if any(matches(getattr(row, m)) for row in run))


def is_null(value: Any) -> bool:
    return value is None


def is_negative(value: Any) -> bool:
    return value is not None and value < 0


def check_field_values(
    records: Iterable[DeliveryRecord], check: str, matches: Callable[[Any], bool], label: str
) -> list[Finding]:
    def is_flagged(row: DeliveryRecord) -> bool:
        return any(matches(getattr(row, metric)) for metric in METRICS)

    return [
        campaign_finding(
            check, rows, run[0].date, run[-1].date, f"{label} {fields_where(run, matches)}"
        )
        for rows in by_campaign(records)
        for run in flagged_runs(rows, is_flagged)
    ]


def check_null_values(records: Iterable[DeliveryRecord]) -> list[Finding]:
    return check_field_values(records, NULL_VALUES, is_null, "null")


def check_negative_values(records: Iterable[DeliveryRecord]) -> list[Finding]:
    return check_field_values(records, NEGATIVE_VALUES, is_negative, "negative")


def check_zero_spend(records: Iterable[DeliveryRecord]) -> list[Finding]:
    """Zero-spend runs that directly follow a reported day with spend."""
    findings: list[Finding] = []
    for rows in by_campaign(records):
        previous = {later.date: earlier for earlier, later in pairwise(rows)}
        for run in flagged_runs(rows, lambda row: row.spend == 0):
            before = previous.get(run[0].date)
            if before is not None and before.spend is not None and before.spend > 0:
                text = "spend dropped to zero"
                findings.append(campaign_finding(ZERO_SPEND, rows, run[0].date, run[-1].date, text))
    return findings


def has_impressions_below_clicks(row: DeliveryRecord) -> bool:
    return row.impressions is not None and row.clicks is not None and row.impressions < row.clicks


def check_impressions_below_clicks(records: Iterable[DeliveryRecord]) -> list[Finding]:
    return [
        campaign_finding(
            IMPRESSIONS_BELOW_CLICKS,
            rows,
            run[0].date,
            run[-1].date,
            "impressions were below clicks",
        )
        for rows in by_campaign(records)
        for run in flagged_runs(rows, has_impressions_below_clicks)
    ]


def check_rejected_rows(result: NormalizeResult) -> list[Finding]:
    grouped: dict[tuple[str, str, str], list[date | None]] = defaultdict(list)
    for row in result.rejected:
        grouped[(row.platform, row.account_id, row.reason)].append(row.date)
    findings = []
    for (platform, account_id, reason), days in sorted(grouped.items()):
        known = [day for day in days if day is not None]
        findings.append(
            Finding(
                check=REJECTED_ROWS,
                severity=SEVERITIES[REJECTED_ROWS],
                platform=platform,
                account_id=account_id,
                campaign_id=None,
                campaign_name=None,
                date_from=min(known, default=None),
                date_to=max(known, default=None),
                message=f"Account {account_id}: {len(days)} row(s) not loaded: {reason}.",
                reason=reason,
            )
        )
    return findings


def check_unknown_accounts(result: NormalizeResult) -> list[Finding]:
    return [
        Finding(
            check=UNKNOWN_ACCOUNT,
            severity=SEVERITIES[UNKNOWN_ACCOUNT],
            platform=account.platform,
            account_id=account.account_id,
            campaign_id=None,
            campaign_name=None,
            date_from=account.date_from,
            date_to=account.date_to,
            message=f"Account {account.account_id} on {account.platform} has delivery "
            "but is not in the reference data, so it was not loaded.",
        )
        for account in result.unknown_accounts
    ]


def finding_order(finding: Finding) -> tuple[str, str, str, date, str]:
    return (
        finding.platform,
        finding.account_id,
        finding.campaign_id or "",
        finding.date_from or date.min,
        finding.check,
    )


def run_quality_checks(result: NormalizeResult) -> list[Finding]:
    """All findings for one normalize result; reads nothing and writes nothing."""
    records = result.records
    findings = [
        *check_missing_days(records),
        *check_null_values(records),
        *check_negative_values(records),
        *check_zero_spend(records),
        *check_impressions_below_clicks(records),
        *check_rejected_rows(result),
        *check_unknown_accounts(result),
    ]
    return sorted(findings, key=finding_order)


def dedup_key(finding: Finding) -> str:
    parts = [
        "quality",
        finding.check,
        finding.platform,
        finding.account_id,
        finding.campaign_id or "-",
        finding.date_from.isoformat() if finding.date_from else "-",
        finding.date_to.isoformat() if finding.date_to else "-",
    ]
    if finding.reason:
        parts.append(finding.reason)
    return ":".join(parts)


def issue_row(
    run_id: uuid.UUID,
    finding: Finding,
    platform_id: int | None,
    client_id: int | None,
) -> dict[str, Any]:
    return {
        "dedup_key": dedup_key(finding),
        "type": f"quality_{finding.check}",
        "severity": finding.severity,
        "client_id": client_id,
        "platform_id": platform_id,
        "period": finding.date_from.replace(day=1) if finding.date_from else None,
        "entity_type": "campaign" if finding.campaign_id else "ad_account",
        "entity_id": finding.campaign_id or finding.account_id,
        "explanation": finding.message,
        "proposed_action": PROPOSED_ACTIONS[finding.check],
        "run_id": run_id,
    }


def load_client_ids(conn: Connection) -> dict[tuple[str, str], int]:
    query = select(Platform.name, AdAccount.external_account_id, AdAccount.client_id).join(
        Platform, Platform.id == AdAccount.platform_id
    )
    return {(row.name, row.external_account_id): row.client_id for row in conn.execute(query)}


def upsert_issues(conn: Connection, rows: Sequence[dict[str, Any]]) -> None:
    """Status and decision columns are never updated, so a re-run keeps a human's decision."""
    for batch in batched(rows, BATCH_SIZE):
        statement = insert(Issue).values(list(batch))
        conn.execute(
            statement.on_conflict_do_update(
                index_elements=["dedup_key"],
                set_={
                    "severity": statement.excluded.severity,
                    "explanation": statement.excluded.explanation,
                    "proposed_action": statement.excluded.proposed_action,
                    "run_id": statement.excluded.run_id,
                },
            )
        )


def write_findings(run_id: uuid.UUID, findings: Sequence[Finding], engine: Engine) -> int:
    """Upsert findings into `app.issues`; `engine` must log in as `app`, which `etl` cannot do."""
    with engine.begin() as conn:
        platform_ids = {
            row.name: row.id for row in conn.execute(select(Platform.name, Platform.id))
        }
        client_ids = load_client_ids(conn)
        rows = [
            issue_row(
                run_id,
                finding,
                platform_ids.get(finding.platform),
                client_ids.get((finding.platform, finding.account_id)),
            )
            for finding in findings
        ]
        upsert_issues(conn, rows)
    return len(rows)
