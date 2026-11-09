"""Pipeline step 2: map each platform's raw delivery to `core.campaigns` and `core.daily_delivery`."""

import re
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal
from itertools import batched
from typing import Any

from app.db.base import Base
from app.db.models.core import AdAccount, Campaign, CampaignAlias, DailyDelivery, Platform
from app.db.session import create_etl_engine
from sqlalchemy import Connection, Engine, select
from sqlalchemy.dialects.postgresql import insert

from pipelines.ingest import PLATFORMS, PlatformApi

DECIMAL_STRING = "decimal_string"
MICROS_INTEGER = "micros_integer"
MICROS_EXPONENT = -6
CENTS = Decimal("0.01")
MAX_SPEND = Decimal("9999999999.99")  # NUMERIC(12,2)
MAX_COUNT = 2**63 - 1  # BIGINT
DECIMAL_TEXT = re.compile(r"-?\d+(\.\d+)?", re.ASCII)
BATCH_SIZE = 1000
ALIAS_SOURCE = "rule"
ALIAS_CONFIDENCE = Decimal("1.000")
DUPLICATE_ROW = "Duplicate row for the same campaign and date"
FOREIGN_CAMPAIGN = "Campaign is already reported by another account"

API_BY_NAME = {api.name: api for api in PLATFORMS}

CampaignKey = tuple[str, str]


@dataclass(frozen=True)
class MetricMap:
    """Where one platform keeps each field of the shared delivery schema."""

    spend: str
    spend_encoding: str
    date_format: str
    campaign_id: str = "campaign_id"
    campaign_name: str = "campaign_name"
    date: str = "date"
    impressions: str = "impressions"
    clicks: str = "clicks"
    conversions: str = "conversions"


METRIC_MAPS = {
    "meta": MetricMap("spend", DECIMAL_STRING, API_BY_NAME["meta"].date_format),
    "google": MetricMap("cost_micros", MICROS_INTEGER, API_BY_NAME["google"].date_format),
    "tiktok": MetricMap("cost", DECIMAL_STRING, API_BY_NAME["tiktok"].date_format),
}


@dataclass(frozen=True)
class DeliveryRecord:
    """One reported day in the shared schema; `date` is account-local, as reported."""

    platform: str
    account_id: str
    timezone: str
    campaign_id: str
    campaign_name: str
    date: date
    spend: Decimal | None
    impressions: int | None
    clicks: int | None
    conversions: int | None

    @property
    def is_complete(self) -> bool:
        metrics = (self.spend, self.impressions, self.clicks, self.conversions)
        return all(metric is not None for metric in metrics)


@dataclass(frozen=True)
class RejectedRow:
    platform: str
    account_id: str
    reason: str
    date: date | None  # None when the row's date itself could not be read.


@dataclass(frozen=True)
class UnknownAccount:
    platform: str
    account_id: str
    date_from: date
    date_to: date


@dataclass(frozen=True)
class Snapshot:
    platform: str
    account_id: str
    period_start: date
    period_end: date
    fetched_at: datetime
    rows: tuple[Any, ...]


@dataclass(frozen=True)
class AccountRef:
    id: int
    platform_id: int
    timezone: str


@dataclass
class NormalizeResult:
    run_id: uuid.UUID
    records: list[DeliveryRecord] = field(default_factory=list)
    rejected: list[RejectedRow] = field(default_factory=list)
    unknown_accounts: list[UnknownAccount] = field(default_factory=list)
    campaigns_written: int = 0
    aliases_written: int = 0
    delivery_written: int = 0


class RowError(ValueError):
    """This row cannot be read; the message is a reason shared by rows with the same fault."""


def parse_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise RowError(f"{name} is not a non-empty string")
    return value


def parse_date(value: Any, name: str, date_format: str) -> date:
    if not isinstance(value, str):
        raise RowError(f"{name} is not a date string")
    try:
        parsed = datetime.strptime(value, date_format)
    except ValueError as error:
        raise RowError(f"{name} does not match {date_format}") from error
    # strptime accepts unpadded numbers, so a round trip is what proves the format.
    if parsed.strftime(date_format) != value or parsed.time() != time.min:
        raise RowError(f"{name} does not match {date_format}")
    return parsed.date()


def to_cents(amount: Decimal, name: str) -> Decimal:
    if abs(amount) > MAX_SPEND:
        raise RowError(f"{name} is too large")
    return amount.quantize(CENTS, rounding=ROUND_HALF_UP)


def parse_spend(value: Any, name: str, encoding: str) -> Decimal | None:
    if value is None:
        return None
    if encoding == MICROS_INTEGER:
        if not isinstance(value, int) or isinstance(value, bool):
            raise RowError(f"{name} is not an integer")
        return to_cents(Decimal(value).scaleb(MICROS_EXPONENT), name)
    if not isinstance(value, str) or DECIMAL_TEXT.fullmatch(value) is None:
        raise RowError(f"{name} is not a decimal string")
    return to_cents(Decimal(value), name)


def parse_count(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise RowError(f"{name} is not an integer")
    if abs(value) > MAX_COUNT:
        raise RowError(f"{name} is too large")
    return value


def parse_row(
    platform: str, account_id: str, timezone: str, raw: Any
) -> DeliveryRecord | RejectedRow:
    """Map one API row to the shared schema; a null metric stays None for the quality checks."""
    if not isinstance(raw, dict):
        return RejectedRow(platform, account_id, "Row is not an object", None)
    mapping = METRIC_MAPS[platform]
    day: date | None = None
    try:
        day = parse_date(raw.get(mapping.date), mapping.date, mapping.date_format)
        return DeliveryRecord(
            platform=platform,
            account_id=account_id,
            timezone=timezone,
            campaign_id=parse_text(raw.get(mapping.campaign_id), mapping.campaign_id),
            campaign_name=parse_text(raw.get(mapping.campaign_name), mapping.campaign_name),
            date=day,
            spend=parse_spend(raw.get(mapping.spend), mapping.spend, mapping.spend_encoding),
            impressions=parse_count(raw.get(mapping.impressions), mapping.impressions),
            clicks=parse_count(raw.get(mapping.clicks), mapping.clicks),
            conversions=parse_count(raw.get(mapping.conversions), mapping.conversions),
        )
    except RowError as error:
        return RejectedRow(platform, account_id, str(error), day)


def snapshot_age(snapshot: Snapshot) -> tuple[datetime, date, date]:
    return snapshot.fetched_at, snapshot.period_start, snapshot.period_end


def is_covered(day: date, snapshots: Iterable[Snapshot]) -> bool:
    return any(snapshot.period_start <= day <= snapshot.period_end for snapshot in snapshots)


def account_records(
    snapshots: Sequence[Snapshot], timezone: str
) -> tuple[list[DeliveryRecord], list[RejectedRow]]:
    """One account's rows; a newer snapshot owns its whole period, including days it omits."""
    kept: dict[tuple[str, date], DeliveryRecord] = {}
    rejected: list[RejectedRow] = []
    newer: list[Snapshot] = []
    for snapshot in sorted(snapshots, key=snapshot_age, reverse=True):
        own: dict[tuple[str, date], DeliveryRecord] = {}
        for raw in snapshot.rows:
            parsed = parse_row(snapshot.platform, snapshot.account_id, timezone, raw)
            if parsed.date is not None and is_covered(parsed.date, newer):
                continue
            if isinstance(parsed, RejectedRow):
                rejected.append(parsed)
                continue
            key = (parsed.campaign_id, parsed.date)
            if key in own:
                rejected.append(
                    RejectedRow(snapshot.platform, snapshot.account_id, DUPLICATE_ROW, parsed.date)
                )
            own[key] = parsed
        for key, record in own.items():
            kept.setdefault(key, record)
        newer.append(snapshot)
    return list(kept.values()), rejected


def campaign_key(record: DeliveryRecord) -> CampaignKey:
    return record.platform, record.campaign_id


def keep_one_account_per_campaign(
    records: Iterable[DeliveryRecord],
) -> tuple[list[DeliveryRecord], list[RejectedRow]]:
    """Campaign ids are unique per platform, so a second account cannot claim the same one."""
    owners: dict[CampaignKey, str] = {}
    kept: list[DeliveryRecord] = []
    rejected: list[RejectedRow] = []
    for record in records:
        if owners.setdefault(campaign_key(record), record.account_id) == record.account_id:
            kept.append(record)
        else:
            rejected.append(
                RejectedRow(record.platform, record.account_id, FOREIGN_CAMPAIGN, record.date)
            )
    return kept, rejected


def campaign_names(records: Iterable[DeliveryRecord]) -> dict[CampaignKey, list[str]]:
    """Each campaign's names by first reported date: the first is its name, the rest aliases."""
    names: dict[CampaignKey, list[str]] = defaultdict(list)
    for record in sorted(records, key=lambda r: r.date):
        known = names[campaign_key(record)]
        if record.campaign_name not in known:
            known.append(record.campaign_name)
    return dict(names)


def group_snapshots(
    snapshots: Iterable[Snapshot],
) -> dict[tuple[str, str], list[Snapshot]]:
    grouped: dict[tuple[str, str], list[Snapshot]] = defaultdict(list)
    for snapshot in snapshots:
        grouped[(snapshot.platform, snapshot.account_id)].append(snapshot)
    return dict(grouped)


def collect_records(
    result: NormalizeResult,
    snapshots: Iterable[Snapshot],
    accounts: dict[tuple[str, str], AccountRef],
) -> None:
    records: list[DeliveryRecord] = []
    for (platform, account_id), group in sorted(group_snapshots(snapshots).items()):
        account = accounts.get((platform, account_id))
        if account is None:
            result.unknown_accounts.append(
                UnknownAccount(
                    platform,
                    account_id,
                    min(snapshot.period_start for snapshot in group),
                    max(snapshot.period_end for snapshot in group),
                )
            )
            continue
        account_rows, rejected = account_records(group, account.timezone)
        records.extend(account_rows)
        result.rejected.extend(rejected)
    kept, foreign = keep_one_account_per_campaign(records)
    result.rejected.extend(foreign)
    result.records = sorted(kept, key=lambda r: (r.platform, r.account_id, r.campaign_id, r.date))


def load_accounts(conn: Connection) -> dict[tuple[str, str], AccountRef]:
    query = select(
        Platform.name,
        AdAccount.external_account_id,
        AdAccount.id,
        AdAccount.platform_id,
        AdAccount.timezone,
    ).join(Platform, Platform.id == AdAccount.platform_id)
    return {
        (row.name, row.external_account_id): AccountRef(row.id, row.platform_id, row.timezone)
        for row in conn.execute(query)
    }


def load_platform_snapshots(conn: Connection, api: PlatformApi) -> list[Snapshot]:
    model = api.model
    query = select(
        model.account_id, model.period_start, model.period_end, model.fetched_at, model.payload
    ).order_by(model.account_id, model.period_start, model.period_end, model.page)
    pages: dict[tuple[str, date, date], list[Any]] = defaultdict(list)
    for row in conn.execute(query):
        pages[(row.account_id, row.period_start, row.period_end)].append(row)
    return [
        Snapshot(
            platform=api.name,
            account_id=account_id,
            period_start=period_start,
            period_end=period_end,
            fetched_at=max(page.fetched_at for page in group),
            rows=tuple(row for page in group for row in api.rows_of(page.payload)),
        )
        for (account_id, period_start, period_end), group in pages.items()
    ]


def upsert(
    conn: Connection,
    model: type[Base],
    rows: Sequence[dict[str, Any]],
    keys: tuple[str, ...],
    updated: tuple[str, ...],
) -> None:
    for batch in batched(rows, BATCH_SIZE):
        statement = insert(model).values(list(batch))
        conn.execute(
            statement.on_conflict_do_update(
                index_elements=list(keys),
                set_={column: statement.excluded[column] for column in updated},
            )
        )


def write_campaigns(
    conn: Connection,
    run_id: uuid.UUID,
    records: Sequence[DeliveryRecord],
    names: dict[CampaignKey, list[str]],
    accounts: dict[tuple[str, str], AccountRef],
) -> dict[CampaignKey, int]:
    """Upsert campaigns and return their ids; `io_id` belongs to plan loading and stays untouched."""
    owners = {campaign_key(r): accounts[(r.platform, r.account_id)] for r in records}
    rows = [
        {
            "platform_id": owners[key].platform_id,
            "ad_account_id": owners[key].id,
            "platform_campaign_id": key[1],
            "name": names[key][0],
            "run_id": run_id,
        }
        for key in sorted(names)
    ]
    upsert(
        conn,
        Campaign,
        rows,
        ("platform_id", "platform_campaign_id"),
        ("ad_account_id", "name", "run_id"),
    )
    query = select(Platform.name, Campaign.platform_campaign_id, Campaign.id).join(
        Platform, Platform.id == Campaign.platform_id
    )
    return {(row.name, row.platform_campaign_id): row.id for row in conn.execute(query)}


def write_aliases(
    conn: Connection,
    run_id: uuid.UUID,
    names: dict[CampaignKey, list[str]],
    ids: dict[CampaignKey, int],
) -> int:
    """Only `run_id` changes on conflict, so an alias a human or AI confirmed keeps its source."""
    rows = [
        {
            "campaign_id": ids[key],
            "alias": alias,
            "source": ALIAS_SOURCE,
            "confidence": ALIAS_CONFIDENCE,
            "run_id": run_id,
        }
        for key in sorted(names)
        for alias in names[key][1:]
    ]
    upsert(conn, CampaignAlias, rows, ("campaign_id", "alias"), ("run_id",))
    return len(rows)


def write_delivery(
    conn: Connection,
    run_id: uuid.UUID,
    records: Sequence[DeliveryRecord],
    ids: dict[CampaignKey, int],
) -> int:
    rows = [
        {
            "campaign_id": ids[campaign_key(record)],
            "date": record.date,
            "spend": record.spend,
            "impressions": record.impressions,
            "clicks": record.clicks,
            "conversions": record.conversions,
            "run_id": run_id,
        }
        for record in records
        if record.is_complete
    ]
    upsert(
        conn,
        DailyDelivery,
        rows,
        ("campaign_id", "date"),
        ("spend", "impressions", "clicks", "conversions", "run_id"),
    )
    return len(rows)


def run_normalize(run_id: uuid.UUID, engine: Engine) -> NormalizeResult:
    """Normalize every stored snapshot in one transaction, so a failure leaves `core` unchanged."""
    result = NormalizeResult(run_id=run_id)
    with engine.begin() as conn:
        accounts = load_accounts(conn)
        snapshots = [s for api in PLATFORMS for s in load_platform_snapshots(conn, api)]
        collect_records(result, snapshots, accounts)
        names = campaign_names(result.records)
        ids = write_campaigns(conn, run_id, result.records, names, accounts)
        result.campaigns_written = len(names)
        result.aliases_written = write_aliases(conn, run_id, names, ids)
        result.delivery_written = write_delivery(conn, run_id, result.records, ids)
    return result


def normalize_from_env(run_id: uuid.UUID) -> NormalizeResult:
    engine = create_etl_engine()
    try:
        return run_normalize(run_id, engine)
    finally:
        engine.dispose()
