"""Build the synthetic "true world" (docs/plan.md §5.1, step 1).

What was planned, delivered and spent, deterministically from the seed in config.yaml.
No documents, no errors: later generator steps render and corrupt this world.

Run: uv run python generator/world.py
"""

import calendar
import hashlib
import json
import math
import random
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import yaml

GENERATOR_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = GENERATOR_DIR / "config.yaml"
DISTRIBUTIONS_PATH = GENERATOR_DIR / "distributions.json"

# Mirrors core.BILLING_MODELS; the generator does not import the DB layer.
BILLING_MODELS = ("markup", "mgmt_fee", "client_owned")
MARKUP_RANGE = (Decimal("10"), Decimal("20"))
PLATFORMS_PER_CLIENT = (2, 3)
RATIO_METRICS = ("cpm", "ctr", "cvr")
ID_DIGITS = {"meta": 15, "google": 10, "tiktok": 19}
DEFAULT_ID_DIGITS = 12
CENT = Decimal("0.01")


class ConfigError(ValueError):
    """config.yaml is malformed or inconsistent."""


# --- config -------------------------------------------------------------------------------


@dataclass(frozen=True)
class PlatformConfig:
    key: str
    name: str
    timezone: str


@dataclass(frozen=True)
class RatioTargets:
    cpm: float
    ctr: float
    cvr: float


@dataclass(frozen=True)
class DeliveryConfig:
    noise_min: float
    noise_max: float
    daily_ratio_sigma_factor: float
    weekday_weights: tuple[Decimal, ...]
    month_multipliers: tuple[tuple[date, Decimal], ...]

    def multiplier(self, month: date) -> Decimal:
        return dict(self.month_multipliers)[month]


@dataclass(frozen=True)
class ClientConfig:
    code: str
    name: str
    industry: str
    billing_model: str
    markup_pct: Decimal | None
    mgmt_fee: Decimal | None
    monthly_budget: Decimal
    platform_shares: tuple[tuple[str, Decimal], ...]


@dataclass(frozen=True)
class Config:
    seed: int
    months: tuple[date, ...]
    error_rate: float
    platforms: tuple[PlatformConfig, ...]
    ratios: tuple[tuple[str, RatioTargets], ...]
    delivery: DeliveryConfig
    clients: tuple[ClientConfig, ...]

    def ratio_targets(self, platform_key: str) -> RatioTargets:
        return dict(self.ratios)[platform_key]


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    return parse_config(yaml.safe_load(path.read_text(encoding="utf-8")))


def parse_config(raw: dict[str, Any]) -> Config:
    platforms = tuple(
        PlatformConfig(key=key, name=value["name"], timezone=value["timezone"])
        for key, value in raw["platforms"].items()
    )
    platform_keys = {p.key for p in platforms}
    months = month_range(parse_month(raw["months"]["start"]), parse_month(raw["months"]["end"]))
    config = Config(
        seed=int(raw["seed"]),
        months=months,
        error_rate=float(raw["error_rate"]),
        platforms=platforms,
        ratios=tuple(
            (key, RatioTargets(**{m: float(value[m]) for m in RATIO_METRICS}))
            for key, value in raw["ratios"].items()
        ),
        delivery=parse_delivery(raw["delivery"]),
        clients=tuple(parse_client(c) for c in raw["clients"]),
    )
    validate_config(config, platform_keys)
    return config


def parse_delivery(raw: dict[str, Any]) -> DeliveryConfig:
    return DeliveryConfig(
        noise_min=float(raw["noise_pct"]["min"]),
        noise_max=float(raw["noise_pct"]["max"]),
        daily_ratio_sigma_factor=float(raw["daily_ratio_sigma_factor"]),
        weekday_weights=tuple(to_decimal(w) for w in raw["weekday_weights"]),
        month_multipliers=tuple(
            (parse_month(month), to_decimal(value))
            for month, value in raw["month_multipliers"].items()
        ),
    )


def parse_client(raw: dict[str, Any]) -> ClientConfig:
    return ClientConfig(
        code=raw["code"],
        name=raw["name"],
        industry=raw["industry"],
        billing_model=raw["billing_model"],
        markup_pct=optional_decimal(raw.get("markup_pct")),
        mgmt_fee=optional_decimal(raw.get("mgmt_fee")),
        monthly_budget=to_decimal(raw["monthly_budget"]),
        platform_shares=tuple((key, to_decimal(share)) for key, share in raw["platforms"].items()),
    )


def validate_config(config: Config, platform_keys: set[str]) -> None:
    if not 0 <= config.error_rate <= 1:
        raise ConfigError("error_rate must be between 0 and 1")
    if set(dict(config.ratios)) != platform_keys:
        raise ConfigError("ratios must be defined for exactly the configured platforms")
    validate_delivery(config.delivery, config.months)
    codes = [c.code for c in config.clients]
    if len(codes) != len(set(codes)):
        raise ConfigError("client codes must be unique")
    for client in config.clients:
        validate_client(client, platform_keys)


def validate_delivery(delivery: DeliveryConfig, months: tuple[date, ...]) -> None:
    if not 0 <= delivery.noise_min <= delivery.noise_max < 1:
        raise ConfigError("noise_pct must satisfy 0 <= min <= max < 1")
    if len(delivery.weekday_weights) != 7 or min(delivery.weekday_weights) <= 0:
        raise ConfigError("weekday_weights needs 7 positive values (Mon..Sun)")
    missing = set(months) - set(dict(delivery.month_multipliers))
    if missing:
        raise ConfigError(f"month_multipliers missing for {sorted(missing)}")


def validate_client(client: ClientConfig, platform_keys: set[str]) -> None:
    label = f"client {client.code}"
    if client.billing_model not in BILLING_MODELS:
        raise ConfigError(f"{label}: unknown billing_model {client.billing_model!r}")
    is_markup = client.billing_model == "markup"
    is_fee = client.billing_model == "mgmt_fee"
    if is_markup != (client.markup_pct is not None):
        raise ConfigError(f"{label}: markup_pct is required for and only for billing_model markup")
    if is_fee != (client.mgmt_fee is not None):
        raise ConfigError(f"{label}: mgmt_fee is required for and only for billing_model mgmt_fee")
    if client.markup_pct is not None and not (
        MARKUP_RANGE[0] <= client.markup_pct <= MARKUP_RANGE[1]
    ):
        raise ConfigError(f"{label}: markup_pct must be within {MARKUP_RANGE}")
    if client.mgmt_fee is not None and client.mgmt_fee <= 0:
        raise ConfigError(f"{label}: mgmt_fee must be positive")
    if client.monthly_budget <= 0:
        raise ConfigError(f"{label}: monthly_budget must be positive")
    validate_shares(label, client.platform_shares, platform_keys)


def validate_shares(
    label: str, shares: tuple[tuple[str, Decimal], ...], platform_keys: set[str]
) -> None:
    low, high = PLATFORMS_PER_CLIENT
    if not low <= len(shares) <= high:
        raise ConfigError(f"{label}: needs {low}-{high} platforms")
    unknown = {key for key, _ in shares} - platform_keys
    if unknown:
        raise ConfigError(f"{label}: unknown platforms {sorted(unknown)}")
    if any(share <= 0 for _, share in shares) or sum(s for _, s in shares) != 1:
        raise ConfigError(f"{label}: platform shares must be positive and sum to 1")


def to_decimal(value: Any) -> Decimal:
    # str() first: a YAML float like 0.9 becomes Decimal("0.9"), not its binary expansion.
    return Decimal(str(value))


def optional_decimal(value: Any) -> Decimal | None:
    return None if value is None else to_decimal(value)


def parse_month(value: str) -> date:
    year, month = str(value).split("-")
    return date(int(year), int(month), 1)


def month_range(start: date, end: date) -> tuple[date, ...]:
    if start > end:
        raise ConfigError("months.start must not be after months.end")
    months = []
    current = start
    while current <= end:
        months.append(current)
        current = next_month(current)
    return tuple(months)


def next_month(month: date) -> date:
    return date(month.year + month.month // 12, month.month % 12 + 1, 1)


def month_end(month: date) -> date:
    return month.replace(day=calendar.monthrange(month.year, month.month)[1])


def month_days(month: date) -> list[date]:
    return [month + timedelta(days=i) for i in range((month_end(month) - month).days + 1)]


# --- distributions ------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricShape:
    sigma: float
    zero_share: float


def load_shapes(path: Path = DISTRIBUTIONS_PATH) -> dict[str, MetricShape]:
    metrics = json.loads(path.read_text(encoding="utf-8"))["metrics"]
    return {
        name: MetricShape(
            sigma=float(metrics[name]["suggested"]["sigma"]),
            zero_share=float(metrics[name]["zero_share"]),
        )
        for name in RATIO_METRICS
    }


# --- randomness ---------------------------------------------------------------------------


def rng_for(seed: int, *key: str) -> random.Random:
    """An independent, reproducible stream per entity.

    String seeds are hashed with SHA-512 by `random`, so this does not depend on
    PYTHONHASHSEED, and adding an entity does not shift the streams of the others.
    """
    return random.Random(":".join((str(seed), *key)))


def lognormal_for_mean(rng: random.Random, mean: float, sigma: float) -> float:
    """Lognormal sample with the given arithmetic mean and log-space sigma."""
    return rng.lognormvariate(math.log(mean) - sigma**2 / 2, sigma)


# --- the world ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Client:
    code: str
    name: str
    industry: str
    billing_model: str
    markup_pct: Decimal | None
    mgmt_fee: Decimal | None


@dataclass(frozen=True)
class Platform:
    key: str
    name: str
    timezone: str


@dataclass(frozen=True)
class AdAccount:
    client_code: str
    platform_key: str
    external_account_id: str
    timezone: str


@dataclass(frozen=True)
class InsertionOrder:
    client_code: str
    platform_key: str
    campaign_ref: str
    start_date: date
    end_date: date
    budget: Decimal
    rate_type: str | None = None


@dataclass(frozen=True)
class Campaign:
    client_code: str
    platform_key: str
    external_account_id: str
    platform_campaign_id: str
    campaign_ref: str
    name: str
    base_cpm: float
    base_ctr: float
    base_cvr: float
    noise_pct: float


@dataclass(frozen=True)
class DailyDelivery:
    """One day of delivery in the ad account's local date (what the platform reports)."""

    campaign_ref: str
    platform_campaign_id: str
    date: date
    plan_spend: Decimal
    spend: Decimal
    impressions: int
    clicks: int
    conversions: int


@dataclass(frozen=True)
class World:
    seed: int
    clients: tuple[Client, ...]
    platforms: tuple[Platform, ...]
    ad_accounts: tuple[AdAccount, ...]
    insertion_orders: tuple[InsertionOrder, ...]
    campaigns: tuple[Campaign, ...]
    delivery: tuple[DailyDelivery, ...]


def build_world(config: Config, shapes: dict[str, MetricShape] | None = None) -> World:
    shapes = shapes if shapes is not None else load_shapes()
    platforms = build_platforms(config)
    ad_accounts = build_ad_accounts(config, platforms)
    insertion_orders = build_insertion_orders(config, platforms)
    campaigns = build_campaigns(config, shapes, platforms, ad_accounts, insertion_orders)
    ios_by_ref = {io.campaign_ref: io for io in insertion_orders}
    delivery = tuple(
        day
        for campaign in campaigns
        for day in build_delivery(config, shapes, campaign, ios_by_ref[campaign.campaign_ref])
    )
    return World(
        seed=config.seed,
        clients=build_clients(config),
        platforms=platforms,
        ad_accounts=ad_accounts,
        insertion_orders=insertion_orders,
        campaigns=campaigns,
        delivery=delivery,
    )


def build_clients(config: Config) -> tuple[Client, ...]:
    return tuple(
        Client(
            code=c.code,
            name=c.name,
            industry=c.industry,
            billing_model=c.billing_model,
            markup_pct=c.markup_pct,
            mgmt_fee=c.mgmt_fee,
        )
        for c in config.clients
    )


def build_platforms(config: Config) -> tuple[Platform, ...]:
    return tuple(Platform(key=p.key, name=p.name, timezone=p.timezone) for p in config.platforms)


def build_ad_accounts(config: Config, platforms: tuple[Platform, ...]) -> tuple[AdAccount, ...]:
    by_key = {p.key: p for p in platforms}
    return tuple(
        AdAccount(
            client_code=client.code,
            platform_key=key,
            external_account_id=external_id(rng_for(config.seed, "account", client.code, key), key),
            timezone=by_key[key].timezone,
        )
        for client in config.clients
        for key, _ in client.platform_shares
    )


def build_insertion_orders(
    config: Config, platforms: tuple[Platform, ...]
) -> tuple[InsertionOrder, ...]:
    by_key = {p.key: p for p in platforms}
    return tuple(
        InsertionOrder(
            client_code=client.code,
            platform_key=key,
            campaign_ref=campaign_ref(client.code, by_key[key], month),
            start_date=month,
            end_date=month_end(month),
            budget=money(client.monthly_budget * config.delivery.multiplier(month) * share),
        )
        for client in config.clients
        for month in config.months
        for key, share in client.platform_shares
    )


def campaign_ref(client_code: str, platform: Platform, month: date) -> str:
    return f"{client_code}-{platform.name.upper()}-{month:%y%m}"


def build_campaigns(
    config: Config,
    shapes: dict[str, MetricShape],
    platforms: tuple[Platform, ...],
    ad_accounts: tuple[AdAccount, ...],
    insertion_orders: tuple[InsertionOrder, ...],
) -> tuple[Campaign, ...]:
    """One campaign per IO, with base ratios sampled once per campaign."""
    platform_names = {p.key: p.name for p in platforms}
    client_names = {c.code: c.name for c in config.clients}
    accounts = {(a.client_code, a.platform_key): a for a in ad_accounts}
    campaigns = []
    for io in insertion_orders:
        rng = rng_for(config.seed, "campaign", io.campaign_ref)
        targets = config.ratio_targets(io.platform_key)
        campaigns.append(
            Campaign(
                client_code=io.client_code,
                platform_key=io.platform_key,
                external_account_id=accounts[(io.client_code, io.platform_key)].external_account_id,
                platform_campaign_id=external_id(rng, io.platform_key),
                campaign_ref=io.campaign_ref,
                name=campaign_name(
                    client_names[io.client_code], platform_names[io.platform_key], io
                ),
                base_cpm=lognormal_for_mean(rng, targets.cpm, shapes["cpm"].sigma),
                base_ctr=lognormal_for_mean(rng, targets.ctr, shapes["ctr"].sigma),
                base_cvr=lognormal_for_mean(rng, targets.cvr, shapes["cvr"].sigma),
                noise_pct=rng.uniform(config.delivery.noise_min, config.delivery.noise_max),
            )
        )
    return tuple(campaigns)


def campaign_name(client_name: str, platform_name: str, io: InsertionOrder) -> str:
    brand = client_name.removeprefix("UAB ")
    return f"{brand} | {platform_name} | {io.start_date:%Y-%m} [{io.campaign_ref}]"


def external_id(rng: random.Random, platform_key: str) -> str:
    digits = ID_DIGITS.get(platform_key, DEFAULT_ID_DIGITS)
    return str(rng.randrange(10 ** (digits - 1), 10**digits))


def build_delivery(
    config: Config, shapes: dict[str, MetricShape], campaign: Campaign, io: InsertionOrder
) -> list[DailyDelivery]:
    rng = rng_for(config.seed, "delivery", campaign.campaign_ref)
    days = month_days(io.start_date)
    plans = split_budget(io.budget, [config.delivery.weekday_weights[d.weekday()] for d in days])
    wobble = config.delivery.daily_ratio_sigma_factor
    rows = []
    for day, plan in zip(days, plans, strict=True):
        spend = money(plan * noise_factor(rng, campaign.noise_pct))
        cpm = campaign.base_cpm * lognormal_for_mean(rng, 1.0, shapes["cpm"].sigma * wobble)
        ctr = campaign.base_ctr * lognormal_for_mean(rng, 1.0, shapes["ctr"].sigma * wobble)
        cvr = campaign.base_cvr * lognormal_for_mean(rng, 1.0, shapes["cvr"].sigma * wobble)
        no_conversions = rng.random() < shapes["cvr"].zero_share
        impressions = round_count(spend * 1000 / to_decimal(round(cpm, 6)))
        clicks = min(impressions, round_count(Decimal(impressions) * to_decimal(round(ctr, 8))))
        conversions = 0 if no_conversions else round_count(clicks * to_decimal(round(cvr, 8)))
        rows.append(
            DailyDelivery(
                campaign_ref=campaign.campaign_ref,
                platform_campaign_id=campaign.platform_campaign_id,
                date=day,
                plan_spend=plan,
                spend=spend,
                impressions=impressions,
                clicks=clicks,
                conversions=min(conversions, clicks),
            )
        )
    return rows


def split_budget(budget: Decimal, weights: list[Decimal]) -> list[Decimal]:
    """Split by weights into cents; the rounding remainder goes to the last day."""
    total = sum(weights, Decimal(0))
    parts = [money(budget * w / total) for w in weights[:-1]]
    return [*parts, budget - sum(parts, Decimal(0))]


def noise_factor(rng: random.Random, amplitude: float) -> Decimal:
    return 1 + to_decimal(round(rng.uniform(-amplitude, amplitude), 6))


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def round_count(value: Decimal | int) -> int:
    return int(Decimal(value).quantize(Decimal(1), rounding=ROUND_HALF_UP))


# --- fingerprint --------------------------------------------------------------------------


def world_digest(world: World) -> str:
    """SHA-256 of the world as canonical JSON; equal worlds give equal digests."""
    payload = json.dumps(asdict(world), default=json_default, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def json_default(value: Any) -> str:
    if isinstance(value, Decimal | date):
        return str(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def main() -> None:
    world = build_world(load_config())
    total = sum((io.budget for io in world.insertion_orders), Decimal(0))
    spend = sum((d.spend for d in world.delivery), Decimal(0))
    print(
        f"clients={len(world.clients)} ios={len(world.insertion_orders)} "
        f"campaigns={len(world.campaigns)} delivery_rows={len(world.delivery)}"
    )
    print(f"budget={total} EUR spend={spend} EUR digest={world_digest(world)}")


if __name__ == "__main__":
    main()
