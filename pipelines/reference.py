"""Load the agency's registry of clients, platforms and ad accounts into `core.*`."""

import json
import uuid
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.db.models.core import AdAccount, Client, Platform
from app.db.session import create_etl_engine
from sqlalchemy import Connection, Engine, select
from sqlalchemy.dialects.postgresql import insert

from pipelines.normalize import METRIC_MAPS

DEFAULT_REFERENCE_PATH = Path("data/generated/reference.json")


class ReferenceDataError(ValueError):
    """The reference file cannot be loaded as it stands."""


@dataclass(frozen=True)
class ReferenceResult:
    clients: int
    platforms: int
    ad_accounts: int


def optional_decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def metric_map_for(platform: str) -> dict[str, str]:
    if platform not in METRIC_MAPS:
        raise ReferenceDataError(f"No metric map for platform {platform!r}")
    return asdict(METRIC_MAPS[platform])


def upsert_clients(conn: Connection, clients: list[dict[str, Any]]) -> dict[str, int]:
    """Client ids by client code; the code exists only in the file, the key in `core` is the name."""
    rows = [
        {
            "name": client["name"],
            "industry": client["industry"],
            "billing_model": client["billing_model"],
            "markup_pct": optional_decimal(client["markup_pct"]),
            "mgmt_fee": optional_decimal(client["mgmt_fee"]),
        }
        for client in clients
    ]
    statement = insert(Client).values(rows)
    conn.execute(
        statement.on_conflict_do_update(
            index_elements=["name"],
            set_={
                column: statement.excluded[column]
                for column in ("industry", "billing_model", "markup_pct", "mgmt_fee")
            },
        )
    )
    ids = {row.name: row.id for row in conn.execute(select(Client.name, Client.id))}
    return {client["code"]: ids[client["name"]] for client in clients}


def upsert_platforms(conn: Connection, platforms: list[dict[str, Any]]) -> dict[str, int]:
    rows = [
        {
            "name": platform["name"],
            "timezone": platform["timezone"],
            "metric_map": metric_map_for(platform["name"]),
        }
        for platform in platforms
    ]
    statement = insert(Platform).values(rows)
    conn.execute(
        statement.on_conflict_do_update(
            index_elements=["name"],
            set_={
                "timezone": statement.excluded.timezone,
                "metric_map": statement.excluded.metric_map,
            },
        )
    )
    return {row.name: row.id for row in conn.execute(select(Platform.name, Platform.id))}


def upsert_ad_accounts(
    conn: Connection,
    run_id: uuid.UUID,
    accounts: list[dict[str, Any]],
    client_ids: dict[str, int],
    platform_ids: dict[str, int],
) -> None:
    try:
        rows = [
            {
                "client_id": client_ids[account["client_code"]],
                "platform_id": platform_ids[account["platform"]],
                "external_account_id": account["external_account_id"],
                "timezone": account["timezone"],
                "run_id": run_id,
            }
            for account in accounts
        ]
    except KeyError as error:
        raise ReferenceDataError(f"Ad account refers to unknown {error}") from error
    statement = insert(AdAccount).values(rows)
    conn.execute(
        statement.on_conflict_do_update(
            index_elements=["platform_id", "external_account_id"],
            set_={
                "client_id": statement.excluded.client_id,
                "timezone": statement.excluded.timezone,
                "run_id": statement.excluded.run_id,
            },
        )
    )


def load_reference(run_id: uuid.UUID, path: Path, engine: Engine) -> ReferenceResult:
    """Upsert the whole registry in one transaction; run it before normalizing."""
    reference = json.loads(path.read_text(encoding="utf-8"))
    with engine.begin() as conn:
        client_ids = upsert_clients(conn, reference["clients"])
        platform_ids = upsert_platforms(conn, reference["platforms"])
        upsert_ad_accounts(conn, run_id, reference["ad_accounts"], client_ids, platform_ids)
    return ReferenceResult(
        clients=len(reference["clients"]),
        platforms=len(reference["platforms"]),
        ad_accounts=len(reference["ad_accounts"]),
    )


def load_reference_from_env(
    run_id: uuid.UUID, path: Path = DEFAULT_REFERENCE_PATH
) -> ReferenceResult:
    engine = create_etl_engine()
    try:
        return load_reference(run_id, path, engine)
    finally:
        engine.dispose()
