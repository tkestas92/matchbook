"""Reported delivery, loaded once from the generator's per-account JSON files."""

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

PLATFORMS = ("meta", "google", "tiktok")


@dataclass(frozen=True)
class DeliveryRow:
    campaign_id: str
    campaign_name: str
    date: date
    spend: Decimal
    impressions: int
    clicks: int
    conversions: int

    def shared_fields(self, date_format: str) -> dict[str, Any]:
        """Every API field except spend, which each platform names and encodes differently."""
        return {
            "campaign_id": self.campaign_id,
            "campaign_name": self.campaign_name,
            "date": self.date.strftime(date_format),
            "impressions": self.impressions,
            "clicks": self.clicks,
            "conversions": self.conversions,
        }


@dataclass(frozen=True)
class Account:
    account_id: str
    timezone: str
    currency: str
    rows: tuple[DeliveryRow, ...]

    def summary(self) -> dict[str, str]:
        return {
            "account_id": self.account_id,
            "timezone": self.timezone,
            "currency": self.currency,
        }

    def rows_between(self, start: date, end: date) -> tuple[DeliveryRow, ...]:
        return tuple(row for row in self.rows if start <= row.date <= end)


Store = dict[str, dict[str, Account]]


def parse_row(raw: dict[str, Any]) -> DeliveryRow:
    return DeliveryRow(
        campaign_id=raw["campaign_id"],
        campaign_name=raw["campaign_name"],
        date=date.fromisoformat(raw["date"]),
        spend=Decimal(raw["spend"]),
        impressions=raw["impressions"],
        clicks=raw["clicks"],
        conversions=raw["conversions"],
    )


def load_account(path: Path) -> Account:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return Account(
        account_id=raw["account_id"],
        timezone=raw["timezone"],
        currency=raw["currency"],
        rows=tuple(parse_row(row) for row in raw["rows"]),
    )


def load_store(delivery_dir: Path) -> Store:
    """Read `<delivery_dir>/<platform>/*.json`; nothing else under the directory is opened."""
    store: Store = {}
    for platform in PLATFORMS:
        accounts = (load_account(path) for path in sorted((delivery_dir / platform).glob("*.json")))
        store[platform] = {account.account_id: account for account in accounts}
    if not any(store.values()):
        raise FileNotFoundError(f"No delivery files under {delivery_dir}; run `make generate`")
    return store
