"""Pipeline step 4: load the Excel media plans into `core.insertion_orders`."""

import re
import uuid
import zipfile
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.db.models.core import AdAccount, Campaign, Client, InsertionOrder, Platform
from app.db.session import create_etl_engine
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException
from sqlalchemy import Connection, Engine, bindparam, select, update

from pipelines.normalize import RowError, parse_text, to_cents, upsert

DEFAULT_DATA_DIR = Path("data/generated")
PLANS_DIR = "plans"
PLAN_HEADERS = ("Platform", "Campaign ref", "Flight start", "Flight end", "Budget (EUR)")
HEADER_ROW = 6
CLIENT_CODE_CELL = "B2"
ORDER_KEY = ("client_id", "platform_id", "campaign_ref", "start_date")
CAMPAIGN_REF_TAG = re.compile(r"\[([^\[\]]+)\]\s*$")
DUPLICATE_ORDER = "Duplicate insertion order for the same client, platform, ref and start date"


@dataclass(frozen=True)
class RejectedDocument:
    path: str  # Relative to the data directory.
    reason: str


@dataclass
class LoadPlansResult:
    run_id: uuid.UUID
    orders_written: int = 0
    campaigns_linked: int = 0
    campaigns_unlinked: int = 0
    rejected: list[RejectedDocument] = field(default_factory=list)


def require_directory(path: Path) -> None:
    """A missing folder is a broken deployment, not a night with nothing to load."""
    if not path.is_dir():
        raise FileNotFoundError(f"No such directory: {path}")


def client_ids(conn: Connection) -> dict[str, int]:
    return {row.code: row.id for row in conn.execute(select(Client.code, Client.id))}


def platform_ids(conn: Connection) -> dict[str, int]:
    return {row.name: row.id for row in conn.execute(select(Platform.name, Platform.id))}


def parse_budget(value: Any) -> Decimal:
    """openpyxl returns numeric cells as int or float; str() gives the digits the cell shows."""
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise RowError("Budget is not a number")
    amount = Decimal(str(value))
    if not amount.is_finite():
        raise RowError("Budget is not a number")
    return to_cents(amount, "Budget")


def parse_cell_date(value: Any, name: str) -> date:
    if isinstance(value, datetime):
        if value.time() != time.min:
            raise RowError(f"{name} is not a date")
        return value.date()
    if isinstance(value, date):
        return value
    raise RowError(f"{name} is not a date")


def parse_order(values: Sequence[Any], client_id: int, platforms: dict[str, int]) -> dict[str, Any]:
    label = parse_text(values[0], "Platform")
    platform_id = platforms.get(label.casefold())
    if platform_id is None:
        raise RowError(f"Unknown platform {label!r}")
    return {
        "client_id": client_id,
        "platform_id": platform_id,
        "campaign_ref": parse_text(values[1], "Campaign ref"),
        "start_date": parse_cell_date(values[2], "Flight start"),
        "end_date": parse_cell_date(values[3], "Flight end"),
        "budget": parse_budget(values[4]),
    }


def order_key(order: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(order[column] for column in ORDER_KEY)


def read_plan(
    path: Path, clients: dict[str, int], platforms: dict[str, int]
) -> list[dict[str, Any]]:
    """Every order in one plan, or RowError: a partial plan would look like missing IOs."""
    try:
        sheet = load_workbook(path, data_only=True).active
    except (zipfile.BadZipFile, InvalidFileException, KeyError, OSError, ValueError) as error:
        raise RowError("File is not a readable workbook") from error
    if sheet is None:
        raise RowError("Workbook has no active sheet")
    columns = len(PLAN_HEADERS)
    header = next(
        sheet.iter_rows(min_row=HEADER_ROW, max_row=HEADER_ROW, max_col=columns, values_only=True),
        None,
    )
    if header != PLAN_HEADERS:
        raise RowError(f"Row {HEADER_ROW} does not have the expected headers")
    code = parse_text(sheet[CLIENT_CODE_CELL].value, "Client code")
    if code not in clients:
        raise RowError(f"Unknown client code {code!r}")
    rows = sheet.iter_rows(min_row=HEADER_ROW + 1, max_col=columns, values_only=True)
    return [parse_order(row, clients[code], platforms) for row in rows if any_value(row)]


def any_value(row: Sequence[Any]) -> bool:
    return any(value is not None for value in row)


def collect_orders(
    result: LoadPlansResult,
    data_dir: Path,
    clients: dict[str, int],
    platforms: dict[str, int],
) -> list[dict[str, Any]]:
    plans_dir = data_dir / PLANS_DIR
    require_directory(plans_dir)
    kept: dict[tuple[Any, ...], dict[str, Any]] = {}
    for path in sorted(plans_dir.glob("*.xlsx")):
        try:
            orders = read_plan(path, clients, platforms)
            keys = [order_key(order) for order in orders]
            if len(set(keys)) != len(keys) or not kept.keys().isdisjoint(keys):
                raise RowError(DUPLICATE_ORDER)
        except RowError as error:
            relative = path.relative_to(data_dir).as_posix()
            result.rejected.append(RejectedDocument(relative, str(error)))
            continue
        kept.update(zip(keys, orders, strict=True))
    return list(kept.values())


def write_orders(conn: Connection, run_id: uuid.UUID, orders: Sequence[dict[str, Any]]) -> None:
    """`rate_type` is not in the plans, so it stays out of the upsert."""
    rows = [{**order, "run_id": run_id} for order in orders]
    upsert(conn, InsertionOrder, rows, ORDER_KEY, ("end_date", "budget", "run_id"))


def campaign_ref_of(name: str) -> str | None:
    match = CAMPAIGN_REF_TAG.search(name)
    return None if match is None else match.group(1)


def link_campaigns(conn: Connection, run_id: uuid.UUID) -> tuple[int, int]:
    """Set `campaigns.io_id` from the `[campaign_ref]` tag; (linked, left alone).

    `campaigns.name` is the earliest reported name, so it keeps the tag after a later rename. A
    ref that fits several IOs of the client and platform is left alone rather than guessed.
    """
    orders: dict[tuple[int, int, str], list[int]] = defaultdict(list)
    query = select(
        InsertionOrder.id,
        InsertionOrder.client_id,
        InsertionOrder.platform_id,
        InsertionOrder.campaign_ref,
    )
    for row in conn.execute(query):
        orders[(row.client_id, row.platform_id, row.campaign_ref)].append(row.id)
    campaigns = select(Campaign.id, Campaign.platform_id, Campaign.name, AdAccount.client_id).join(
        AdAccount, AdAccount.id == Campaign.ad_account_id
    )
    links: list[dict[str, int]] = []
    unlinked = 0
    for row in conn.execute(campaigns):
        ref = campaign_ref_of(row.name)
        matches = [] if ref is None else orders.get((row.client_id, row.platform_id, ref), [])
        if len(matches) == 1:
            links.append({"target_id": row.id, "target_io": matches[0]})
        else:
            unlinked += 1
    if links:
        conn.execute(
            update(Campaign)
            .where(Campaign.id == bindparam("target_id"))
            .values(io_id=bindparam("target_io"), run_id=run_id),
            links,
        )
    return len(links), unlinked


def load_plans(run_id: uuid.UUID, data_dir: Path, engine: Engine) -> LoadPlansResult:
    """Load every readable plan in one transaction; run it after normalizing."""
    result = LoadPlansResult(run_id=run_id)
    with engine.begin() as conn:
        orders = collect_orders(result, data_dir, client_ids(conn), platform_ids(conn))
        write_orders(conn, run_id, orders)
        result.orders_written = len(orders)
        result.campaigns_linked, result.campaigns_unlinked = link_campaigns(conn, run_id)
    return result


def load_plans_from_env(run_id: uuid.UUID, data_dir: Path = DEFAULT_DATA_DIR) -> LoadPlansResult:
    engine = create_etl_engine()
    try:
        return load_plans(run_id, data_dir, engine)
    finally:
        engine.dispose()
