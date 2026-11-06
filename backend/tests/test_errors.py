import json
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pypdf import PdfReader

from generator.documents.billing import Documents, build_documents, fee_line
from generator.documents.config import DocumentsConfig, load_documents_config
from generator.documents.model import Invoice, invoice_ground_truth
from generator.documents.pdf import format_amount
from generator.documents.writer import MANIFEST_NAME, generate_documents, invoice_dir
from generator.errors import (
    ANSWER_KEY_PATH,
    DEFAULT_MIN_PER_TYPE,
    ERROR_TYPES,
    AnswerKeyEntry,
    ErrorsConfig,
    PlantedOutput,
    ReportedDay,
    error_count,
    generate_with_errors,
    load_errors_config,
    parse_errors_config,
    plant_errors,
)
from generator.world import (
    CENT,
    DEFAULT_CONFIG_PATH,
    Config,
    ConfigError,
    World,
    build_world,
    load_config,
    month_end,
    next_month,
    world_digest,
)


@pytest.fixture(scope="module")
def config() -> Config:
    return load_config()


@pytest.fixture(scope="module")
def world(config: Config) -> World:
    return build_world(config)


@pytest.fixture(scope="module")
def docs_config() -> DocumentsConfig:
    return load_documents_config()


@pytest.fixture(scope="module")
def errors_config() -> ErrorsConfig:
    return load_errors_config()


@pytest.fixture(scope="module")
def clean(world: World, docs_config: DocumentsConfig) -> Documents:
    return build_documents(world, docs_config)


@pytest.fixture(scope="module")
def planted(world: World, clean: Documents, errors_config: ErrorsConfig) -> PlantedOutput:
    return plant_errors(world, clean, errors_config)


@pytest.fixture(scope="module")
def output_dirs(
    world: World,
    clean: Documents,
    docs_config: DocumentsConfig,
    errors_config: ErrorsConfig,
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path]:
    first, second = tmp_path_factory.mktemp("planted1"), tmp_path_factory.mktemp("planted2")
    for out in (first, second):
        generate_with_errors(world, clean, docs_config, errors_config, out)
    return first, second


@pytest.fixture(scope="module")
def out_dir(output_dirs: tuple[Path, Path]) -> Path:
    return output_dirs[0]


def entries_of(planted: PlantedOutput, slug: str) -> list[AnswerKeyEntry]:
    return [e for e in planted.answer_key if e.error_type == slug]


def by_number(invoices: tuple[Invoice, ...]) -> dict[str, Invoice]:
    return {i.number: i for i in invoices}


def rows_by_ref(rows) -> dict[str, list]:  # noqa: ANN001 - DailyDelivery or ReportedDay rows
    grouped: dict[str, list] = defaultdict(list)
    for row in rows:
        grouped[row.campaign_ref].append(row)
    return grouped


def changed_campaigns(world: World, planted: PlantedOutput) -> set[str]:
    clean = {
        ref: [(d.date, d.spend, d.impressions, d.clicks, d.conversions) for d in days]
        for ref, days in rows_by_ref(world.delivery).items()
    }
    names = {c.campaign_ref: c.name for c in world.campaigns}
    changed = set()
    for ref, days in rows_by_ref(planted.delivery).items():
        reported = [(d.date, d.spend, d.impressions, d.clicks, d.conversions) for d in days]
        if reported != clean[ref] or any(d.campaign_name != names[ref] for d in days):
            changed.add(ref)
    return changed


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def pdf_text(path: Path) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(path).pages)


# --- planted and recorded -----------------------------------------------------------------


def test_every_error_type_is_planted(planted: PlantedOutput, errors_config: ErrorsConfig) -> None:
    counts = Counter(e.error_type for e in planted.answer_key)
    assert set(counts) == {t.slug for t in ERROR_TYPES}
    for allocation in planted.allocations:
        expected = min(allocation.available, errors_config.min_per_type)
        assert counts[allocation.error_type] >= expected >= 1


def test_counts_follow_the_formula(planted: PlantedOutput, errors_config: ErrorsConfig) -> None:
    counts = Counter(e.error_type for e in planted.answer_key)
    for a in planted.allocations:
        expected = error_count(
            errors_config.error_rate, errors_config.min_per_type, a.candidates, a.available
        )
        assert a.planted == expected == counts[a.error_type], a


def test_each_error_is_recorded_once(planted: PlantedOutput) -> None:
    keys = [(e.error_type, e.entity_ref) for e in planted.answer_key]
    assert len(keys) == len(set(keys))
    entities = [e.entity_ref for e in planted.answer_key]
    assert len(entities) == len(set(entities))


def test_changed_artifacts_match_the_answer_key(
    world: World, clean: Documents, planted: PlantedOutput
) -> None:
    """Every changed document or campaign has exactly one entry, and every entry changed one."""
    clean_platform = by_number(clean.platform_invoices)
    planted_platform = by_number(planted.documents.platform_invoices)
    refs = defaultdict(set)
    for e in planted.answer_key:
        refs[e.entity_type].add(e.entity_ref)

    changed_platform = {
        n for n, i in planted_platform.items() if n in clean_platform and i != clean_platform[n]
    }
    added_platform = set(planted_platform) - set(clean_platform)
    assert set(clean_platform) <= set(planted_platform)
    assert changed_platform | added_platform == refs["platform_invoice"]
    assert added_platform == {e.entity_ref for e in entries_of(planted, "duplicate_invoice")}

    clean_client = by_number(clean.client_invoices)
    changed_client = {
        i.number for i in planted.documents.client_invoices if i != clean_client[i.number]
    }
    assert changed_client == refs["client_invoice"]
    assert planted.documents.credit_notes == clean.credit_notes

    assert changed_campaigns(world, planted) == refs["campaign"]


def test_answer_key_fields(planted: PlantedOutput) -> None:
    types = {t.slug: t for t in ERROR_TYPES}
    for e in planted.answer_key:
        error = types[e.error_type]
        assert (e.error_no, e.entity_type) == (error.no, error.entity_type)
        assert e.expected_detector == error.expected_detector
        assert e.detectable_in_stage == error.detectable_in_stage
        assert e.period_start.day == 1
        assert e.period_end == month_end(e.period_start)
        assert e.magnitude == e.magnitude.quantize(CENT)
        assert e.magnitude != 0
    stage1 = {e.error_no for e in planted.answer_key if e.detectable_in_stage == 1}
    assert stage1 == {1, 2, 4, 5, 6, 7, 9}


def test_no_two_errors_share_a_slot(world: World, planted: PlantedOutput) -> None:
    platforms = defaultdict(list)
    for account in world.ad_accounts:
        platforms[account.client_code].append(account.platform_key)
    claimed: list[tuple[str, str, date]] = []
    for e in planted.answer_key:
        if e.entity_type == "client_invoice":
            claimed += [(e.client_code, p, e.period_start) for p in platforms[e.client_code]]
        else:
            assert e.platform is not None
            claimed.append((e.client_code, e.platform, e.period_start))
            if e.error_type == "timezone_shift":
                claimed.append((e.client_code, e.platform, next_month(e.period_start)))
    assert len(claimed) == len(set(claimed))


# --- magnitudes match the changed artifacts ------------------------------------------------


def test_invoice_above_delivery_magnitude(clean: Documents, planted: PlantedOutput) -> None:
    before, after = (
        by_number(clean.platform_invoices),
        by_number(planted.documents.platform_invoices),
    )
    for e in entries_of(planted, "invoice_above_delivery"):
        assert e.magnitude == after[e.entity_ref].subtotal - before[e.entity_ref].subtotal
        pct = Decimal(e.details["pct"])
        assert Decimal("0.03") <= pct <= Decimal("0.15")
        assert e.magnitude == (before[e.entity_ref].subtotal * pct).quantize(CENT, "ROUND_HALF_UP")


def test_duplicate_invoice_magnitude(clean: Documents, planted: PlantedOutput) -> None:
    before, after = (
        by_number(clean.platform_invoices),
        by_number(planted.documents.platform_invoices),
    )
    for e in entries_of(planted, "duplicate_invoice"):
        duplicate, original = after[e.entity_ref], before[e.details["original_number"]]
        assert e.magnitude == duplicate.subtotal == original.subtotal
        assert duplicate.number != original.number
        assert duplicate.lines == original.lines
        assert (duplicate.vendor, duplicate.period_start) == (
            original.vendor,
            original.period_start,
        )
        assert duplicate.issue_date > original.issue_date


def test_wrong_markup_magnitude(world: World, clean: Documents, planted: PlantedOutput) -> None:
    clients = {c.code: c for c in world.clients}
    before, after = by_number(clean.client_invoices), by_number(planted.documents.client_invoices)
    for e in entries_of(planted, "wrong_markup"):
        old, new = before[e.entity_ref], after[e.entity_ref]
        client = clients[e.client_code]
        assert client.billing_model == "markup"
        assert (
            e.magnitude
            == new.subtotal - old.subtotal
            == new.lines[-1].amount - old.lines[-1].amount
        )
        wrong_pct = Decimal(e.details["invoiced_markup_pct"])
        assert Decimal("2") <= abs(wrong_pct - client.markup_pct) <= Decimal("5")
        assert f"{wrong_pct}%" in new.lines[-1].description
        assert new.lines[:-1] == old.lines[:-1]


def test_credit_not_applied_magnitude(
    world: World, clean: Documents, planted: PlantedOutput
) -> None:
    clients = {c.code: c for c in world.clients}
    notes = {n.number: n for n in clean.credit_notes}
    before, after = by_number(clean.client_invoices), by_number(planted.documents.client_invoices)
    for e in entries_of(planted, "credit_not_applied"):
        old, new = before[e.entity_ref], after[e.entity_ref]
        note = notes[e.details["credit_note"]]
        assert not any(note.number in line.description for line in new.lines)
        assert len(new.lines) == len(old.lines) - 1
        assert [line.line_no for line in new.lines] == list(range(1, len(new.lines) + 1))
        net_media = sum((line.amount for line in new.lines[:-1]), Decimal(0))
        assert new.lines[-1].amount == fee_line(clients[e.client_code], net_media)[2]
        assert e.magnitude == new.subtotal - old.subtotal
        assert e.magnitude >= note.subtotal


def test_delivery_magnitudes(world: World, planted: PlantedOutput) -> None:
    clean_rows = rows_by_ref(world.delivery)
    reported = rows_by_ref(planted.delivery)

    for e in entries_of(planted, "timezone_shift"):
        last = clean_rows[e.entity_ref][-1]
        assert last.date == e.period_end
        shifted = reported[e.entity_ref][-1]
        assert shifted.date == next_month(e.period_start)
        assert e.magnitude == last.spend == shifted.spend

    for e in entries_of(planted, "missing_days"):
        missing = set(e.details["missing_dates"])
        reported_dates = {d.date for d in reported[e.entity_ref]}
        assert 1 <= len(missing) <= 3
        assert not missing & reported_dates
        assert {e.period_start, e.period_end} <= reported_dates
        assert e.magnitude == sum(d.spend for d in clean_rows[e.entity_ref] if d.date in missing)

    for e in entries_of(planted, "cpc_spike"):
        dates = set(e.details["dates"])
        spend = sum(d.spend for d in reported[e.entity_ref] if d.date in dates)
        assert spend == sum(d.spend for d in clean_rows[e.entity_ref] if d.date in dates)
        assert e.magnitude == (spend * (1 - 1 / Decimal("1.8"))).quantize(CENT, "ROUND_HALF_UP")
        for before, after in zip(clean_rows[e.entity_ref], reported[e.entity_ref], strict=True):
            if after.date in dates:
                assert after.clicks < before.clicks
                assert float(after.spend / after.clicks) == pytest.approx(
                    1.8 * float(before.spend / before.clicks), rel=0.05
                )

    for e in entries_of(planted, "campaign_renamed"):
        renamed_from = e.details["renamed_from"]
        renamed = [d for d in reported[e.entity_ref] if d.campaign_name == e.details["new_name"]]
        assert renamed and all(d.date >= renamed_from for d in renamed)
        assert e.entity_ref not in e.details["new_name"]
        assert e.magnitude == sum(d.spend for d in renamed)


def test_pacing_drift_keeps_monthly_spend(world: World, planted: PlantedOutput) -> None:
    clean_rows = rows_by_ref(world.delivery)
    reported = rows_by_ref(planted.delivery)
    for e in entries_of(planted, "pacing_drift"):
        rows, plan = reported[e.entity_ref], clean_rows[e.entity_ref]
        assert sum(d.spend for d in rows) == sum(d.spend for d in plan)
        active = e.details["active_days"]
        assert active == round(0.6 * len(rows))
        assert all(d.spend > 0 for d in rows[:active])
        assert all(d.spend == 0 and d.clicks == 0 for d in rows[active:])
        gaps, cum_actual, cum_plan = [], Decimal(0), Decimal(0)
        for actual, planned in zip(rows, plan, strict=True):
            cum_actual += actual.spend
            cum_plan += planned.plan_spend
            gaps.append(cum_actual - cum_plan)
        assert e.magnitude == max(gaps) > 0


def test_platform_errors_do_not_spill_into_client_invoices(
    clean: Documents, planted: PlantedOutput
) -> None:
    client_errors = {e.entity_ref for e in planted.answer_key if e.entity_type == "client_invoice"}
    for invoice in planted.documents.client_invoices:
        if invoice.number not in client_errors:
            assert invoice == by_number(clean.client_invoices)[invoice.number]


# --- the clean pass stays clean ------------------------------------------------------------


def test_planting_does_not_change_the_clean_output(
    config: Config, docs_config: DocumentsConfig, errors_config: ErrorsConfig
) -> None:
    world = build_world(config)
    digest = world_digest(world)
    documents = build_documents(world, docs_config)
    plant_errors(world, documents, errors_config)
    assert world_digest(world) == digest
    assert build_documents(world, docs_config) == documents


def test_no_errors_gives_the_clean_output(
    world: World,
    clean: Documents,
    docs_config: DocumentsConfig,
    errors_config: ErrorsConfig,
    tmp_path: Path,
) -> None:
    none = replace(errors_config, error_rate=Decimal(0), min_per_type=0)
    result = generate_with_errors(world, clean, docs_config, none, tmp_path / "planted")
    assert result.answer_key == ()
    assert result.documents == clean
    assert [(d.date, d.spend, d.clicks) for d in result.delivery] == [
        (d.date, d.spend, d.clicks) for d in world.delivery
    ]
    generate_documents(world, docs_config, tmp_path / "clean")
    clean_files = load_json(tmp_path / "clean" / MANIFEST_NAME)["files"]
    planted_files = load_json(tmp_path / "planted" / MANIFEST_NAME)["files"]
    documents_only = {
        path: digest
        for path, digest in planted_files.items()
        if not path.startswith("delivery/") and path != ANSWER_KEY_PATH.as_posix()
    }
    assert documents_only == clean_files


# --- determinism --------------------------------------------------------------------------


def test_same_seed_gives_identical_files(output_dirs: tuple[Path, Path]) -> None:
    first, second = (load_json(d / MANIFEST_NAME) for d in output_dirs)
    assert first == second
    assert ANSWER_KEY_PATH.as_posix() in first["files"]


def test_different_seed_gives_different_errors(
    config: Config,
    docs_config: DocumentsConfig,
    errors_config: ErrorsConfig,
    planted: PlantedOutput,
) -> None:
    world = build_world(replace(config, seed=config.seed + 1))
    other = plant_errors(world, build_documents(world, docs_config), errors_config)
    campaigns = {e.entity_ref for e in planted.answer_key if e.entity_type == "campaign"}
    assert campaigns != {e.entity_ref for e in other.answer_key if e.entity_type == "campaign"}


# --- files on disk ------------------------------------------------------------------------


def test_answer_key_file_matches_entries(out_dir: Path, planted: PlantedOutput) -> None:
    key = load_json(out_dir / ANSWER_KEY_PATH)
    assert len(key["errors"]) == len(planted.answer_key)
    for row, entry in zip(key["errors"], planted.answer_key, strict=True):
        assert row["entity_ref"] == entry.entity_ref
        assert Decimal(row["magnitude"]) == entry.magnitude
        assert row["period_start"] == entry.period_start.isoformat()
        assert row["expected_detector"] == entry.expected_detector


def test_changed_invoices_on_disk_are_consistent(
    clean: Documents, planted: PlantedOutput, out_dir: Path
) -> None:
    """Ground-truth JSON equals the planted invoice and the PDF shows the same amounts."""
    clean_numbers = {i.number: i for i in (*clean.platform_invoices, *clean.client_invoices)}
    touched = [
        i
        for i in (*planted.documents.platform_invoices, *planted.documents.client_invoices)
        if clean_numbers.get(i.number) != i
    ]
    assert len(touched) == sum(
        e.entity_type in {"platform_invoice", "client_invoice"} for e in planted.answer_key
    )
    for invoice in touched:
        base = out_dir / invoice_dir(invoice) / invoice.number
        assert load_json(base.with_suffix(".json")) == invoice_ground_truth(invoice)
        text = pdf_text(base.with_suffix(".pdf"))
        assert invoice.number in text
        for amount in (invoice.subtotal, invoice.total, *(line.amount for line in invoice.lines)):
            assert format_amount(invoice.layout, amount) in text, (invoice.number, amount)
        for line in invoice.lines:
            assert line.description in text


def test_delivery_files_hold_the_reported_rows(
    world: World, planted: PlantedOutput, out_dir: Path
) -> None:
    files = sorted((out_dir / "delivery").rglob("*.json"))
    assert len(files) == len(world.ad_accounts)
    rows = [row for path in files for row in load_json(path)["rows"]]
    assert len(rows) == len(planted.delivery)
    assert all("campaign_ref" not in row for row in rows)
    on_disk = {(row["campaign_id"], row["date"]): row for row in rows}
    for day in planted.delivery:
        row = on_disk[(day.platform_campaign_id, day.date.isoformat())]
        assert (Decimal(row["spend"]), row["clicks"], row["campaign_name"]) == (
            day.spend,
            day.clicks,
            day.campaign_name,
        )
    assert isinstance(planted.delivery[0], ReportedDay)


# --- count formula and config ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("rate", "min_per_type", "candidates", "available", "expected"),
    [
        ("0.05", 3, 114, 80, 6),  # rate term wins
        ("0.05", 3, 24, 22, 3),  # min_per_type wins
        ("0.05", 3, 4, 2, 2),  # capped at what is available
        ("0.05", 3, 2, 2, 2),  # fewer candidates than min_per_type: use them all
        ("0.50", 0, 5, 5, 3),  # round half up
        ("0", 0, 10, 10, 0),
        ("0", 0, 0, 0, 0),  # nothing wanted, nothing needed
    ],
)
def test_error_count(
    rate: str, min_per_type: int, candidates: int, available: int, expected: int
) -> None:
    assert error_count(Decimal(rate), min_per_type, candidates, available) == expected


def test_error_count_without_candidates_is_rejected() -> None:
    with pytest.raises(ConfigError):
        error_count(Decimal("0.05"), 3, 0, 0)


def test_count_is_capped_at_available_credit_notes(
    world: World, docs_config: DocumentsConfig, errors_config: ErrorsConfig
) -> None:
    two = replace(docs_config, credit_notes=replace(docs_config.credit_notes, count=2))
    result = plant_errors(world, build_documents(world, two), errors_config)
    assert len(entries_of(result, "credit_not_applied")) == 2


def test_zero_candidates_is_rejected(
    world: World, docs_config: DocumentsConfig, errors_config: ErrorsConfig
) -> None:
    none = replace(docs_config, credit_notes=replace(docs_config.credit_notes, count=0))
    with pytest.raises(ConfigError):
        plant_errors(world, build_documents(world, none), errors_config)


@pytest.fixture
def raw_config() -> dict:
    return yaml.safe_load(Path(DEFAULT_CONFIG_PATH).read_text(encoding="utf-8"))


def test_min_per_type_defaults_to_three(raw_config: dict) -> None:
    del raw_config["errors"]["min_per_type"]
    assert parse_errors_config(raw_config).min_per_type == DEFAULT_MIN_PER_TYPE == 3


def test_errors_config_parses_decimals(errors_config: ErrorsConfig) -> None:
    assert errors_config.error_rate == Decimal("0.05")
    assert errors_config.cpc_multiplier == Decimal("1.8")
    assert errors_config.min_per_type == 3


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("errors", "min_per_type"), -1),
        (("errors", "min_per_type"), "3"),
        (("errors", "min_per_type"), True),
        (("errors", "invoice_above_delivery", "pct", "min"), "0.20"),
        (("errors", "invoice_above_delivery", "pct", "min"), "0"),
        (("errors", "wrong_markup", "delta_pp", "max"), "1.00"),
        (("errors", "pacing_drift", "month_share"), "1.0"),
        (("errors", "cpc_spike", "multiplier"), "1.0"),
        (("errors", "cpc_spike", "days"), 0),
        (("errors", "missing_days", "days", "min"), 0),
        (("errors", "campaign_renamed", "from_day", "max"), 30),
        (("errors", "duplicate_invoice", "delay_days", "min"), 0),
        (("error_rate",), 1.5),
    ],
)
def test_invalid_errors_config_is_rejected(raw_config: dict, path: tuple, value: object) -> None:
    target = raw_config
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ConfigError):
        parse_errors_config(raw_config)
