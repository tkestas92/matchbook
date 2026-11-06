"""Plant errors #1-#9 into the clean output (docs/plan.md §5.1 step 3, §5.2).

A separate pass: the true world and the clean documents are never mutated; planted copies are
returned instead. Every planted error is recorded exactly once in the answer key, written to
data/generated/answer_key.json. Only the eval loader (run as the DB owner) puts it into
eval.answer_key (decision 025). Error #10 (prompt injection) comes in stage 2.

Each error claims (client, platform, month) slots so no entity carries two errors and the
answer key stays unambiguous.
"""

import random
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from generator.documents.billing import Documents, fee_line, vat_and_total
from generator.documents.config import DocumentsConfig
from generator.documents.model import CURRENCY, Invoice, InvoiceLine
from generator.documents.numbering import PLATFORM_NUMBER_FORMATS
from generator.documents.writer import write_outputs
from generator.world import (
    DEFAULT_CONFIG_PATH,
    Campaign,
    ConfigError,
    World,
    money,
    month_end,
    next_month,
    rng_for,
    round_count,
    split_budget,
    to_decimal,
)

DEFAULT_MIN_PER_TYPE = 3
ANSWER_KEY_PATH = Path("answer_key.json")
DELIVERY_DIR = Path("delivery")
RENAMED_LABELS = ("Always-on", "Prospecting", "Retargeting", "Seasonal push", "Promo")

Slot = tuple[str, str, date]  # (client_code, platform_key, month)


# --- config -------------------------------------------------------------------------------


@dataclass(frozen=True)
class DecimalRange:
    min: Decimal
    max: Decimal


@dataclass(frozen=True)
class IntRange:
    min: int
    max: int


@dataclass(frozen=True)
class ErrorsConfig:
    error_rate: Decimal
    min_per_type: int
    overcharge_pct: DecimalRange
    duplicate_delay_days: IntRange
    rename_from_day: IntRange
    markup_delta_pp: DecimalRange
    pacing_month_share: Decimal
    cpc_multiplier: Decimal
    cpc_spike_days: int
    missing_days: IntRange


def load_errors_config(path: Path = DEFAULT_CONFIG_PATH) -> ErrorsConfig:
    return parse_errors_config(yaml.safe_load(path.read_text(encoding="utf-8")))


def parse_errors_config(raw: dict[str, Any]) -> ErrorsConfig:
    errors = raw["errors"]
    config = ErrorsConfig(
        error_rate=to_decimal(raw["error_rate"]),
        min_per_type=parse_min_per_type(errors.get("min_per_type", DEFAULT_MIN_PER_TYPE)),
        overcharge_pct=decimal_range(errors["invoice_above_delivery"]["pct"]),
        duplicate_delay_days=int_range(errors["duplicate_invoice"]["delay_days"]),
        rename_from_day=int_range(errors["campaign_renamed"]["from_day"]),
        markup_delta_pp=decimal_range(errors["wrong_markup"]["delta_pp"]),
        pacing_month_share=to_decimal(errors["pacing_drift"]["month_share"]),
        cpc_multiplier=to_decimal(errors["cpc_spike"]["multiplier"]),
        cpc_spike_days=int(errors["cpc_spike"]["days"]),
        missing_days=int_range(errors["missing_days"]["days"]),
    )
    validate_errors_config(config)
    return config


def parse_min_per_type(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError("errors.min_per_type must be a non-negative integer")
    return value


def decimal_range(raw: dict[str, Any]) -> DecimalRange:
    return DecimalRange(min=to_decimal(raw["min"]), max=to_decimal(raw["max"]))


def int_range(raw: dict[str, Any]) -> IntRange:
    return IntRange(min=int(raw["min"]), max=int(raw["max"]))


def validate_errors_config(config: ErrorsConfig) -> None:
    if not 0 <= config.error_rate <= 1:
        raise ConfigError("error_rate must be between 0 and 1")
    for name, low, high in (
        ("invoice_above_delivery.pct", config.overcharge_pct.min, config.overcharge_pct.max),
        ("wrong_markup.delta_pp", config.markup_delta_pp.min, config.markup_delta_pp.max),
    ):
        if not 0 < low <= high:
            raise ConfigError(f"errors.{name} must satisfy 0 < min <= max")
    for name, days, lowest, highest in (
        ("duplicate_invoice.delay_days", config.duplicate_delay_days, 1, 60),
        ("campaign_renamed.from_day", config.rename_from_day, 2, 28),
        ("missing_days.days", config.missing_days, 1, 7),
    ):
        if not lowest <= days.min <= days.max <= highest:
            raise ConfigError(f"errors.{name} must satisfy {lowest} <= min <= max <= {highest}")
    if not 0 < config.pacing_month_share < 1:
        raise ConfigError("errors.pacing_drift.month_share must be between 0 and 1")
    if config.cpc_multiplier <= 1:
        raise ConfigError("errors.cpc_spike.multiplier must be above 1")
    if not 1 <= config.cpc_spike_days <= 7:
        raise ConfigError("errors.cpc_spike.days must be between 1 and 7")


def error_count(rate: Decimal, min_per_type: int, candidates: int, available: int) -> int:
    """max(min_per_type, round(rate × candidates)), capped at the candidates still available."""
    target = max(min_per_type, round_count(rate * candidates))
    if target > 0 and available == 0:
        raise ConfigError("no candidates available for a planted error type")
    return min(available, target)


# --- error types and records --------------------------------------------------------------


@dataclass(frozen=True)
class ErrorType:
    no: int
    slug: str
    entity_type: str
    expected_detector: str
    detectable_in_stage: int


INVOICE_ABOVE_DELIVERY = ErrorType(
    1, "invoice_above_delivery", "platform_invoice", "reconciliation", 1
)
DUPLICATE_INVOICE = ErrorType(2, "duplicate_invoice", "platform_invoice", "reconciliation", 1)
CAMPAIGN_RENAMED = ErrorType(3, "campaign_renamed", "campaign", "reconciliation_ai", 2)
TIMEZONE_SHIFT = ErrorType(4, "timezone_shift", "campaign", "reconciliation", 1)
WRONG_MARKUP = ErrorType(5, "wrong_markup", "client_invoice", "reconciliation", 1)
CREDIT_NOT_APPLIED = ErrorType(6, "credit_not_applied", "client_invoice", "reconciliation", 1)
PACING_DRIFT = ErrorType(7, "pacing_drift", "campaign", "pacing", 1)
CPC_SPIKE = ErrorType(8, "cpc_spike", "campaign", "anomalies", 4)
MISSING_DAYS = ErrorType(9, "missing_days", "campaign", "data_quality", 1)
ERROR_TYPES = (
    INVOICE_ABOVE_DELIVERY,
    DUPLICATE_INVOICE,
    CAMPAIGN_RENAMED,
    TIMEZONE_SHIFT,
    WRONG_MARKUP,
    CREDIT_NOT_APPLIED,
    PACING_DRIFT,
    CPC_SPIKE,
    MISSING_DAYS,
)


@dataclass(frozen=True)
class ReportedDay:
    """One delivery row as the platform API will report it (after planted errors).

    `campaign_ref` is the true IO link, kept for bookkeeping; it is not written to the API files.
    """

    campaign_ref: str
    platform_campaign_id: str
    campaign_name: str
    date: date
    spend: Decimal
    impressions: int
    clicks: int
    conversions: int


@dataclass(frozen=True)
class AnswerKeyEntry:
    error_no: int
    error_type: str
    entity_type: str
    entity_ref: str
    client_code: str
    platform: str | None
    period_start: date
    period_end: date
    magnitude: Decimal  # EUR, net of VAT; signed (negative = under-billed)
    expected_detector: str
    detectable_in_stage: int
    details: dict[str, Any]


@dataclass(frozen=True)
class Allocation:
    error_type: str
    candidates: int
    available: int
    planted: int


@dataclass(frozen=True)
class PlantedOutput:
    documents: Documents
    delivery: tuple[ReportedDay, ...]
    answer_key: tuple[AnswerKeyEntry, ...]
    allocations: tuple[Allocation, ...]


# --- the planting pass --------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    key: str
    slots: tuple[Slot, ...]
    item: Any


@dataclass
class Workspace:
    """Mutable copies of the clean output while errors are planted."""

    world: World
    config: ErrorsConfig
    documents: Documents
    platform_invoices: dict[str, Invoice]
    client_invoices: dict[str, Invoice]
    delivery: dict[str, list[ReportedDay]]
    duplicates: dict[str, Invoice] = field(default_factory=dict)
    claimed: set[Slot] = field(default_factory=set)
    entries: list[AnswerKeyEntry] = field(default_factory=list)
    allocations: list[Allocation] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.campaigns = {c.campaign_ref: c for c in self.world.campaigns}
        self.months = {io.campaign_ref: io.start_date for io in self.world.insertion_orders}
        self.clients = {c.code: c for c in self.world.clients}
        self.platforms = {p.key: p for p in self.world.platforms}
        self.plan = {(d.campaign_ref, d.date): d.plan_spend for d in self.world.delivery}
        self.credited = {note.original_number for note in self.documents.credit_notes}
        self.last_month = max(self.months.values())

    def campaign_slot(self, campaign: Campaign) -> Slot:
        return (campaign.client_code, campaign.platform_key, self.months[campaign.campaign_ref])

    def client_slots(self, client_code: str, month: date) -> tuple[Slot, ...]:
        return tuple(
            (client_code, a.platform_key, month)
            for a in self.world.ad_accounts
            if a.client_code == client_code
        )

    def rng(self, error: ErrorType, *key: str) -> random.Random:
        return rng_for(self.world.seed, "errors", error.slug, *key)


def plant_errors(world: World, documents: Documents, config: ErrorsConfig) -> PlantedOutput:
    """Return planted copies of `documents` and the delivery, plus the answer key.

    VAT rate and payment terms are taken from each document, so no documents config is needed.
    """
    ws = Workspace(
        world=world,
        config=config,
        documents=documents,
        platform_invoices={i.number: i for i in documents.platform_invoices},
        client_invoices={i.number: i for i in documents.client_invoices},
        delivery=clean_reported_delivery(world),
    )
    # Most constrained first, so the rare candidates are not taken by broader types.
    for planter in (
        plant_credit_not_applied,
        plant_wrong_markup,
        plant_duplicate_invoice,
        plant_invoice_above_delivery,
        plant_timezone_shift,
        plant_missing_days,
        plant_pacing_drift,
        plant_cpc_spike,
        plant_campaign_renamed,
    ):
        planter(ws)
    return PlantedOutput(
        documents=Documents(
            platform_invoices=tuple(
                invoice
                for clean in documents.platform_invoices
                for invoice in (ws.platform_invoices[clean.number], ws.duplicates.get(clean.number))
                if invoice is not None
            ),
            credit_notes=documents.credit_notes,
            client_invoices=tuple(ws.client_invoices[i.number] for i in documents.client_invoices),
        ),
        delivery=tuple(day for c in world.campaigns for day in ws.delivery[c.campaign_ref]),
        answer_key=tuple(sorted(ws.entries, key=lambda e: (e.error_no, e.entity_ref))),
        allocations=tuple(sorted(ws.allocations, key=lambda a: a.error_type)),
    )


def clean_reported_delivery(world: World) -> dict[str, list[ReportedDay]]:
    names = {c.campaign_ref: c.name for c in world.campaigns}
    rows: dict[str, list[ReportedDay]] = defaultdict(list)
    for day in world.delivery:
        rows[day.campaign_ref].append(
            ReportedDay(
                campaign_ref=day.campaign_ref,
                platform_campaign_id=day.platform_campaign_id,
                campaign_name=names[day.campaign_ref],
                date=day.date,
                spend=day.spend,
                impressions=day.impressions,
                clicks=day.clicks,
                conversions=day.conversions,
            )
        )
    return {ref: sorted(days, key=lambda d: d.date) for ref, days in rows.items()}


def allocate(ws: Workspace, error: ErrorType, candidates: Iterable[Candidate]) -> list[Candidate]:
    """Pick candidates one by one among those whose slots are all still free."""
    pool = sorted(candidates, key=lambda c: c.key)
    free = [c for c in pool if is_free(ws, c)]
    available = len(free)
    count = error_count(ws.config.error_rate, ws.config.min_per_type, len(pool), available)
    rng = ws.rng(error)
    chosen: list[Candidate] = []
    while len(chosen) < count and free:
        pick = rng.choice(free)
        ws.claimed.update(pick.slots)
        chosen.append(pick)
        free = [c for c in free if is_free(ws, c)]
    ws.allocations.append(Allocation(error.slug, len(pool), available, len(chosen)))
    return sorted(chosen, key=lambda c: c.key)


def is_free(ws: Workspace, candidate: Candidate) -> bool:
    return ws.claimed.isdisjoint(candidate.slots)


def record(
    ws: Workspace,
    error: ErrorType,
    *,
    entity_ref: str,
    client_code: str,
    platform: str | None,
    month: date,
    magnitude: Decimal,
    details: dict[str, Any],
) -> None:
    ws.entries.append(
        AnswerKeyEntry(
            error_no=error.no,
            error_type=error.slug,
            entity_type=error.entity_type,
            entity_ref=entity_ref,
            client_code=client_code,
            platform=platform,
            period_start=month,
            period_end=month_end(month),
            magnitude=magnitude,
            expected_detector=error.expected_detector,
            detectable_in_stage=error.detectable_in_stage,
            details=details,
        )
    )


def with_lines(invoice: Invoice, lines: Iterable[InvoiceLine]) -> Invoice:
    """The invoice with new lines, renumbered, and subtotal/VAT/total recomputed."""
    numbered = tuple(replace(line, line_no=i) for i, line in enumerate(lines, start=1))
    subtotal = sum((line.amount for line in numbered), Decimal(0))
    vat, total = vat_and_total(subtotal, invoice.vat_rate)
    return replace(invoice, lines=numbered, subtotal=subtotal, vat=vat, total=total)


def uniform_decimal(rng: random.Random, span: DecimalRange, places: int) -> Decimal:
    return to_decimal(round(rng.uniform(float(span.min), float(span.max)), places))


# --- client invoice errors (#5, #6) -------------------------------------------------------


def client_invoice_candidates(ws: Workspace, invoices: Iterable[Invoice]) -> list[Candidate]:
    return [
        Candidate(i.number, ws.client_slots(i.client_code, i.period_start), i) for i in invoices
    ]


def plant_credit_not_applied(ws: Workspace) -> None:
    by_key = {(i.client_code, i.period_start): i for i in ws.client_invoices.values()}
    candidates = [
        Candidate(
            note.number,
            ws.client_slots(note.client_code, note.period_start),
            (note, by_key[(note.client_code, note.period_start)]),
        )
        for note in ws.documents.credit_notes
        if (note.client_code, note.period_start) in by_key
    ]
    for candidate in allocate(ws, CREDIT_NOT_APPLIED, candidates):
        note, invoice = candidate.item
        *media, fee = invoice.lines
        kept = [line for line in media if note.number not in line.description]
        net_media = sum((line.amount for line in kept), Decimal(0))
        _, fee_text, fee_amount = fee_line(ws.clients[invoice.client_code], net_media)
        planted = with_lines(
            invoice, [*kept, replace(fee, description=fee_text, amount=fee_amount)]
        )
        ws.client_invoices[invoice.number] = planted
        record(
            ws,
            CREDIT_NOT_APPLIED,
            entity_ref=invoice.number,
            client_code=invoice.client_code,
            platform=note.platform_key,
            month=invoice.period_start,
            magnitude=planted.subtotal - invoice.subtotal,
            details={
                "credit_note": note.number,
                "original_invoice": note.original_number,
                "credit_amount": note.subtotal,
                "fee_delta": fee_amount - fee.amount,
                "expected_subtotal": invoice.subtotal,
                "invoiced_subtotal": planted.subtotal,
            },
        )


def plant_wrong_markup(ws: Workspace) -> None:
    markup_invoices = [
        i
        for i in ws.client_invoices.values()
        if ws.clients[i.client_code].billing_model == "markup"
    ]
    for candidate in allocate(ws, WRONG_MARKUP, client_invoice_candidates(ws, markup_invoices)):
        invoice: Invoice = candidate.item
        client = ws.clients[invoice.client_code]
        assert client.markup_pct is not None
        rng = ws.rng(WRONG_MARKUP, invoice.number)
        delta = uniform_decimal(rng, ws.config.markup_delta_pp, 2) * rng.choice((1, -1))
        wrong_pct = money(client.markup_pct + delta)
        *media, fee = invoice.lines
        net_media = sum((line.amount for line in media), Decimal(0))
        _, fee_text, fee_amount = fee_line(replace(client, markup_pct=wrong_pct), net_media)
        planted = with_lines(
            invoice, [*media, replace(fee, description=fee_text, amount=fee_amount)]
        )
        ws.client_invoices[invoice.number] = planted
        record(
            ws,
            WRONG_MARKUP,
            entity_ref=invoice.number,
            client_code=invoice.client_code,
            platform=None,
            month=invoice.period_start,
            magnitude=fee_amount - fee.amount,
            details={
                "contract_markup_pct": client.markup_pct,
                "invoiced_markup_pct": wrong_pct,
                "net_media": net_media,
                "expected_fee": fee.amount,
                "invoiced_fee": fee_amount,
            },
        )


# --- platform invoice errors (#1, #2) -----------------------------------------------------


def platform_invoice_candidates(ws: Workspace) -> list[Candidate]:
    return [
        Candidate(i.number, ((i.client_code, i.platform_key or "", i.period_start),), i)
        for i in ws.platform_invoices.values()
        if i.number not in ws.credited
    ]


def plant_duplicate_invoice(ws: Workspace) -> None:
    taken = {*ws.platform_invoices, *ws.client_invoices}
    for candidate in allocate(ws, DUPLICATE_INVOICE, platform_invoice_candidates(ws)):
        original: Invoice = candidate.item
        assert original.platform_key is not None and original.account_id is not None
        rng = ws.rng(DUPLICATE_INVOICE, original.number)
        number = original.number
        while number in taken:
            number = PLATFORM_NUMBER_FORMATS[original.platform_key](
                rng, original.account_id, original.period_start
            )
        taken.add(number)
        delay = timedelta(
            days=rng.randint(ws.config.duplicate_delay_days.min, ws.config.duplicate_delay_days.max)
        )
        duplicate = replace(
            original,
            number=number,
            issue_date=original.issue_date + delay,
            due_date=original.due_date + delay,
        )
        ws.duplicates[original.number] = duplicate
        record(
            ws,
            DUPLICATE_INVOICE,
            entity_ref=number,
            client_code=original.client_code,
            platform=original.platform_key,
            month=original.period_start,
            magnitude=duplicate.subtotal,
            details={
                "original_number": original.number,
                "vendor": original.vendor,
                "issue_date": duplicate.issue_date,
                "total": duplicate.total,
            },
        )


def plant_invoice_above_delivery(ws: Workspace) -> None:
    for candidate in allocate(ws, INVOICE_ABOVE_DELIVERY, platform_invoice_candidates(ws)):
        invoice: Invoice = candidate.item
        rng = ws.rng(INVOICE_ABOVE_DELIVERY, invoice.number)
        pct = uniform_decimal(rng, ws.config.overcharge_pct, 4)
        target = rng.choice(invoice.lines)
        lines = [
            replace(line, amount=money(line.amount * (1 + pct))) if line is target else line
            for line in invoice.lines
        ]
        planted = with_lines(invoice, lines)
        ws.platform_invoices[invoice.number] = planted
        record(
            ws,
            INVOICE_ABOVE_DELIVERY,
            entity_ref=invoice.number,
            client_code=invoice.client_code,
            platform=invoice.platform_key,
            month=invoice.period_start,
            magnitude=planted.subtotal - invoice.subtotal,
            details={
                "pct": pct,
                "campaign_ref": target.campaign_ref,
                "delivered_subtotal": invoice.subtotal,
                "invoiced_subtotal": planted.subtotal,
            },
        )


# --- delivery errors (#3, #4, #7, #8, #9) -------------------------------------------------


def campaign_candidates(ws: Workspace, *, agency_billed_only: bool = False) -> list[Candidate]:
    return [
        Candidate(c.campaign_ref, (ws.campaign_slot(c),), c)
        for c in ws.world.campaigns
        if not agency_billed_only or ws.clients[c.client_code].billing_model != "client_owned"
    ]


def record_campaign(
    ws: Workspace,
    error: ErrorType,
    campaign: Campaign,
    magnitude: Decimal,
    details: dict[str, Any],
) -> None:
    record(
        ws,
        error,
        entity_ref=campaign.campaign_ref,
        client_code=campaign.client_code,
        platform=campaign.platform_key,
        month=ws.months[campaign.campaign_ref],
        magnitude=magnitude,
        details={"platform_campaign_id": campaign.platform_campaign_id, **details},
    )


def plant_timezone_shift(ws: Workspace) -> None:
    """The last day's row is reported on the 1st of the next month; the invoice still has it."""
    invoiced_month = {
        (i.client_code, i.platform_key, i.period_start): i.number
        for i in ws.platform_invoices.values()
    }
    candidates = []
    for candidate in campaign_candidates(ws, agency_billed_only=True):
        client, platform, month = candidate.slots[0]
        invoice = invoiced_month.get((client, platform, month))
        if month == ws.last_month or invoice is None or invoice in ws.credited:
            continue
        candidates.append(
            replace(candidate, slots=(*candidate.slots, (client, platform, next_month(month))))
        )
    for candidate in allocate(ws, TIMEZONE_SHIFT, candidates):
        campaign: Campaign = candidate.item
        rows = ws.delivery[campaign.campaign_ref]
        last = rows[-1]
        moved = replace(last, date=next_month(ws.months[campaign.campaign_ref]))
        rows[-1] = moved
        record_campaign(
            ws,
            TIMEZONE_SHIFT,
            campaign,
            last.spend,
            {
                "original_date": last.date,
                "reported_date": moved.date,
                "account_timezone": ws.platforms[campaign.platform_key].timezone,
            },
        )


def plant_missing_days(ws: Workspace) -> None:
    """1-3 consecutive mid-month days are absent from the API; the invoice still bills them."""
    for candidate in allocate(ws, MISSING_DAYS, campaign_candidates(ws)):
        campaign: Campaign = candidate.item
        rows = ws.delivery[campaign.campaign_ref]
        rng = ws.rng(MISSING_DAYS, campaign.campaign_ref)
        count = rng.randint(ws.config.missing_days.min, ws.config.missing_days.max)
        start = rng.randint(1, len(rows) - 1 - count)  # never the first or the last day
        missing = rows[start : start + count]
        del rows[start : start + count]
        record_campaign(
            ws,
            MISSING_DAYS,
            campaign,
            sum((day.spend for day in missing), Decimal(0)),
            {
                "missing_dates": [day.date for day in missing],
                "side_effect": "invoice_vs_delivery_gap",
            },
        )


def plant_pacing_drift(ws: Workspace) -> None:
    """The same monthly spend lands in the first share of the month; later days spend nothing."""
    share = ws.config.pacing_month_share
    for candidate in allocate(ws, PACING_DRIFT, campaign_candidates(ws)):
        campaign: Campaign = candidate.item
        rows = ws.delivery[campaign.campaign_ref]
        plans = [ws.plan[(campaign.campaign_ref, day.date)] for day in rows]
        active = round_count(share * len(rows))
        total = sum((day.spend for day in rows), Decimal(0))
        spends = split_budget(total, plans[:active]) + [Decimal("0.00")] * (len(rows) - active)
        rows[:] = [rescale(day, spend) for day, spend in zip(rows, spends, strict=True)]
        record_campaign(
            ws,
            PACING_DRIFT,
            campaign,
            peak_overpacing([d.spend for d in rows], plans),
            {
                "active_days": active,
                "last_active_date": rows[active - 1].date,
                "month_spend": total,
                "month_share": share,
            },
        )


def rescale(day: ReportedDay, spend: Decimal) -> ReportedDay:
    """The day with a new spend and its volume metrics scaled by the same factor."""
    factor = spend / day.spend
    impressions = round_count(day.impressions * factor)
    clicks = min(impressions, round_count(day.clicks * factor))
    conversions = min(clicks, round_count(day.conversions * factor))
    return replace(
        day, spend=spend, impressions=impressions, clicks=clicks, conversions=conversions
    )


def peak_overpacing(actual: list[Decimal], plan: list[Decimal]) -> Decimal:
    """Largest gap of cumulative actual spend over cumulative planned spend."""
    peak, cum_actual, cum_plan = Decimal(0), Decimal(0), Decimal(0)
    for spent, planned in zip(actual, plan, strict=True):
        cum_actual += spent
        cum_plan += planned
        peak = max(peak, cum_actual - cum_plan)
    return peak


def plant_cpc_spike(ws: Workspace) -> None:
    """Clicks drop by the multiplier for a few days at the same spend, so CPC spikes."""
    multiplier, days = ws.config.cpc_multiplier, ws.config.cpc_spike_days
    for candidate in allocate(ws, CPC_SPIKE, campaign_candidates(ws)):
        campaign: Campaign = candidate.item
        rows = ws.delivery[campaign.campaign_ref]
        start = ws.rng(CPC_SPIKE, campaign.campaign_ref).randint(1, len(rows) - 1 - days)
        spiked = rows[start : start + days]
        for offset, day in enumerate(spiked):
            clicks = round_count(Decimal(day.clicks) / multiplier)
            rows[start + offset] = replace(
                day, clicks=clicks, conversions=min(day.conversions, clicks)
            )
        spend = sum((day.spend for day in spiked), Decimal(0))
        record_campaign(
            ws,
            CPC_SPIKE,
            campaign,
            money(spend * (1 - 1 / multiplier)),
            {
                "dates": [day.date for day in spiked],
                "multiplier": multiplier,
                "spend": spend,
                "clicks_before": sum(day.clicks for day in spiked),
                "clicks_after": sum(day.clicks for day in rows[start : start + days]),
            },
        )


def plant_campaign_renamed(ws: Workspace) -> None:
    """From a mid-month day the API reports a new name without the campaign_ref tag."""
    for candidate in allocate(ws, CAMPAIGN_RENAMED, campaign_candidates(ws)):
        campaign: Campaign = candidate.item
        month = ws.months[campaign.campaign_ref]
        rng = ws.rng(CAMPAIGN_RENAMED, campaign.campaign_ref)
        renamed_from = month.replace(
            day=rng.randint(ws.config.rename_from_day.min, ws.config.rename_from_day.max)
        )
        brand = ws.clients[campaign.client_code].name.removeprefix("UAB ")
        platform = ws.platforms[campaign.platform_key].name
        new_name = f"{brand} {platform} – {rng.choice(RENAMED_LABELS)} {month:%B}"
        rows = ws.delivery[campaign.campaign_ref]
        rows[:] = [
            replace(day, campaign_name=new_name) if day.date >= renamed_from else day
            for day in rows
        ]
        record_campaign(
            ws,
            CAMPAIGN_RENAMED,
            campaign,
            sum((day.spend for day in rows if day.date >= renamed_from), Decimal(0)),
            {"old_name": campaign.name, "new_name": new_name, "renamed_from": renamed_from},
        )


# --- files --------------------------------------------------------------------------------


def jsonable(value: Any) -> Any:
    if isinstance(value, Decimal | date):
        return str(value)
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [jsonable(item) for item in value]
    return value


def answer_key_payload(
    world: World, config: ErrorsConfig, planted: PlantedOutput
) -> dict[str, Any]:
    return {
        "seed": world.seed,
        "error_rate": str(config.error_rate),
        "min_per_type": config.min_per_type,
        "allocations": [jsonable(vars(a)) for a in planted.allocations],
        "errors": [jsonable(vars(entry)) for entry in planted.answer_key],
    }


def reported_delivery_files(world: World, delivery: Iterable[ReportedDay]) -> dict[Path, Any]:
    """One file per ad account with the fields a platform would return (no campaign_ref)."""
    campaigns = {c.campaign_ref: c for c in world.campaigns}
    by_account: dict[tuple[str, str], list[ReportedDay]] = defaultdict(list)
    for day in delivery:
        campaign = campaigns[day.campaign_ref]
        by_account[(campaign.platform_key, campaign.external_account_id)].append(day)
    files: dict[Path, Any] = {}
    for account in world.ad_accounts:
        key = (account.platform_key, account.external_account_id)
        rows = sorted(by_account.get(key, []), key=lambda d: (d.date, d.platform_campaign_id))
        files[DELIVERY_DIR / account.platform_key / f"{account.external_account_id}.json"] = {
            "platform": account.platform_key,
            "account_id": account.external_account_id,
            "timezone": account.timezone,
            "currency": CURRENCY,
            "rows": [
                {
                    "campaign_id": day.platform_campaign_id,
                    "campaign_name": day.campaign_name,
                    "date": day.date.isoformat(),
                    "spend": str(day.spend),
                    "impressions": day.impressions,
                    "clicks": day.clicks,
                    "conversions": day.conversions,
                }
                for day in rows
            ],
        }
    return files


def planted_files(world: World, config: ErrorsConfig, planted: PlantedOutput) -> dict[Path, Any]:
    return {
        **reported_delivery_files(world, planted.delivery),
        ANSWER_KEY_PATH: answer_key_payload(world, config, planted),
    }


def generate_with_errors(
    world: World,
    documents: Documents,
    docs_config: DocumentsConfig,
    config: ErrorsConfig,
    out_dir: Path,
) -> PlantedOutput:
    """Plant errors into the clean documents and replace `out_dir` with the result."""
    planted = plant_errors(world, documents, config)
    files = planted_files(world, config, planted)
    write_outputs(world, planted.documents, docs_config, out_dir, files)
    return planted
