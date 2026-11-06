import copy
import random
import statistics
from collections import defaultdict
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from app.db.models.core import BILLING_MODELS

from generator.world import (
    DEFAULT_CONFIG_PATH,
    MARKUP_RANGE,
    Config,
    ConfigError,
    World,
    build_world,
    load_config,
    lognormal_for_mean,
    month_end,
    parse_config,
    rng_for,
    split_budget,
    world_digest,
)


@pytest.fixture(scope="module")
def raw_config() -> dict:
    return yaml.safe_load(Path(DEFAULT_CONFIG_PATH).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def config() -> Config:
    return load_config()


@pytest.fixture(scope="module")
def world(config: Config) -> World:
    return build_world(config)


def test_same_seed_gives_identical_world(config: Config, world: World) -> None:
    again = build_world(load_config())
    assert again == world
    assert world_digest(again) == world_digest(world)


def test_different_seed_gives_different_world(config: Config, world: World) -> None:
    other = build_world(replace(config, seed=config.seed + 1))
    assert world_digest(other) != world_digest(world)


def test_config_defines_eight_clients_with_valid_billing(config: Config) -> None:
    assert len(config.clients) == 8
    assert {c.billing_model for c in config.clients} == set(BILLING_MODELS)
    for client in config.clients:
        assert 2 <= len(client.platform_shares) <= 3
        if client.billing_model == "markup":
            assert MARKUP_RANGE[0] <= client.markup_pct <= MARKUP_RANGE[1]


def test_config_covers_may_to_october(config: Config) -> None:
    assert config.months[0] == date(2026, 5, 1)
    assert config.months[-1] == date(2026, 10, 1)
    assert len(config.months) == 6


def test_one_io_per_client_month_platform(config: Config, world: World) -> None:
    expected = sum(len(c.platform_shares) for c in config.clients) * len(config.months)
    assert len(world.insertion_orders) == expected
    refs = [io.campaign_ref for io in world.insertion_orders]
    assert len(refs) == len(set(refs))
    for io in world.insertion_orders:
        assert io.start_date.day == 1
        assert io.end_date == month_end(io.start_date)


def test_every_io_has_one_campaign_in_an_ad_account(world: World) -> None:
    accounts = {(a.client_code, a.platform_key): a.external_account_id for a in world.ad_accounts}
    assert [c.campaign_ref for c in world.campaigns] == [
        io.campaign_ref for io in world.insertion_orders
    ]
    for campaign in world.campaigns:
        key = (campaign.client_code, campaign.platform_key)
        assert campaign.external_account_id == accounts[key]
        assert campaign.campaign_ref in campaign.name


def test_plan_spend_sums_exactly_to_io_budget(world: World) -> None:
    plan = defaultdict(Decimal)
    for day in world.delivery:
        plan[day.campaign_ref] += day.plan_spend
    for io in world.insertion_orders:
        assert plan[io.campaign_ref] == io.budget


def test_delivery_covers_every_day_of_the_flight(world: World) -> None:
    days = defaultdict(list)
    for row in world.delivery:
        days[row.campaign_ref].append(row.date)
    for io in world.insertion_orders:
        assert days[io.campaign_ref][0] == io.start_date
        assert days[io.campaign_ref][-1] == io.end_date
        assert len(days[io.campaign_ref]) == io.end_date.day


def test_money_is_decimal_with_cents(world: World) -> None:
    amounts = [io.budget for io in world.insertion_orders]
    amounts += [d.spend for d in world.delivery] + [d.plan_spend for d in world.delivery]
    for amount in amounts:
        assert isinstance(amount, Decimal)
        assert amount.as_tuple().exponent == -2


def test_actual_spend_is_within_noise_of_plan(world: World) -> None:
    cent = Decimal("0.01")
    for day in world.delivery:
        assert day.plan_spend * Decimal("0.90") - cent <= day.spend
        assert day.spend <= day.plan_spend * Decimal("1.10") + cent


def test_monthly_spend_is_within_ten_percent_of_budget(world: World) -> None:
    spend = defaultdict(Decimal)
    for day in world.delivery:
        spend[day.campaign_ref] += day.spend
    for io in world.insertion_orders:
        assert abs(spend[io.campaign_ref] - io.budget) <= io.budget * Decimal("0.10")


def test_spend_actually_varies_from_plan(world: World) -> None:
    assert sum(d.spend != d.plan_spend for d in world.delivery) > len(world.delivery) * 0.9


def test_counts_are_consistent(world: World) -> None:
    for day in world.delivery:
        assert day.impressions >= day.clicks >= day.conversions >= 0
        assert day.impressions > 0


@pytest.mark.parametrize("metric", ["cpm", "ctr", "cvr"])
def test_campaign_ratio_means_match_config_targets(
    config: Config, world: World, metric: str
) -> None:
    for platform in world.platforms:
        values = [
            getattr(c, f"base_{metric}") for c in world.campaigns if c.platform_key == platform.key
        ]
        target = getattr(config.ratio_targets(platform.key), metric)
        # CVR's Kaggle sigma (~1.2) is wide, so its campaign mean is noisier.
        tolerance = 0.5 if metric == "cvr" else 0.25
        assert statistics.fmean(values) == pytest.approx(target, rel=tolerance)


def test_realised_ctr_and_cpm_are_realistic(world: World) -> None:
    impressions = sum(d.impressions for d in world.delivery)
    clicks = sum(d.clicks for d in world.delivery)
    spend = sum((d.spend for d in world.delivery), Decimal(0))
    assert 0.005 < clicks / impressions < 0.03
    assert Decimal(2) < spend * 1000 / impressions < Decimal(10)


def test_rng_for_is_stable_and_keyed() -> None:
    first = [rng_for(1, "a", "b").random() for _ in range(2)]
    assert first[0] == first[1]
    assert rng_for(1, "a", "b").random() != rng_for(1, "a", "c").random()


def test_lognormal_for_mean_hits_target_mean() -> None:
    rng = random.Random(0)
    samples = [lognormal_for_mean(rng, 0.01, 0.45) for _ in range(20_000)]
    assert statistics.fmean(samples) == pytest.approx(0.01, rel=0.03)


def test_split_budget_puts_remainder_on_last_day() -> None:
    parts = split_budget(Decimal("100.00"), [Decimal(1)] * 3)
    assert parts == [Decimal("33.33"), Decimal("33.33"), Decimal("33.34")]


def _with_client_change(raw: dict, **changes: object) -> dict:
    changed = copy.deepcopy(raw)
    changed["clients"][0].update(changes)
    return changed


def test_config_rejects_shares_not_summing_to_one(raw_config: dict) -> None:
    raw = _with_client_change(raw_config, platforms={"meta": "0.50", "google": "0.40"})
    with pytest.raises(ConfigError, match="sum to 1"):
        parse_config(raw)


def test_config_rejects_markup_out_of_range(raw_config: dict) -> None:
    raw = _with_client_change(raw_config, billing_model="markup", markup_pct="25.00")
    with pytest.raises(ConfigError, match="markup_pct"):
        parse_config(raw)


def test_config_rejects_single_platform(raw_config: dict) -> None:
    raw = _with_client_change(raw_config, platforms={"meta": "1.00"})
    with pytest.raises(ConfigError, match="platforms"):
        parse_config(raw)


def test_config_rejects_mgmt_fee_without_amount(raw_config: dict) -> None:
    raw = _with_client_change(raw_config, billing_model="mgmt_fee", markup_pct=None)
    with pytest.raises(ConfigError, match="mgmt_fee"):
        parse_config(raw)
