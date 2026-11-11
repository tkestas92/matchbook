import json
from collections import defaultdict
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from openpyxl import load_workbook
from pypdf import PdfReader

from generator.documents.billing import Documents, build_documents, vat_and_total
from generator.documents.config import (
    DocumentsConfig,
    load_documents_config,
    parse_documents_config,
)
from generator.documents.contacts import (
    FAKE_BANK_CODE,
    PHONE_PREFIX,
    grouped_iban,
    iban_check_digits,
    iban_is_valid,
    vendor_contact,
)
from generator.documents.excel import HEADER_ROW, PLAN_HEADERS
from generator.documents.pdf import format_amount
from generator.documents.writer import (
    MANIFEST_NAME,
    generate_documents,
    invoice_dir,
    plan_path,
)
from generator.world import (
    CENT,
    DEFAULT_CONFIG_PATH,
    Config,
    ConfigError,
    World,
    build_world,
    load_config,
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
def documents(world: World, docs_config: DocumentsConfig) -> Documents:
    return build_documents(world, docs_config)


@pytest.fixture(scope="module")
def output_dirs(
    world: World, docs_config: DocumentsConfig, tmp_path_factory: pytest.TempPathFactory
) -> tuple[Path, Path]:
    first, second = tmp_path_factory.mktemp("run1"), tmp_path_factory.mktemp("run2")
    generate_documents(world, docs_config, first)
    generate_documents(world, docs_config, second)
    return first, second


@pytest.fixture(scope="module")
def out_dir(output_dirs: tuple[Path, Path]) -> Path:
    return output_dirs[0]


def billing_models(world: World) -> dict[str, str]:
    return {c.code: c.billing_model for c in world.clients}


def delivered_spend(world: World) -> dict[tuple[str, str, date], dict[str, Decimal]]:
    """(client, platform, month) -> campaign_ref -> spend, straight from the world."""
    campaigns = {c.campaign_ref: c for c in world.campaigns}
    totals: dict[tuple[str, str, date], dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    for day in world.delivery:
        c = campaigns[day.campaign_ref]
        totals[(c.client_code, c.platform_key, day.date.replace(day=1))][day.campaign_ref] += (
            day.spend
        )
    return totals


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# --- platform invoices --------------------------------------------------------------------


def test_platform_invoice_per_agency_billed_account_and_month(
    world: World, documents: Documents
) -> None:
    models = billing_models(world)
    expected = {key for key in delivered_spend(world) if models[key[0]] != "client_owned"}
    actual = {(i.client_code, i.platform_key, i.period_start) for i in documents.platform_invoices}
    assert actual == expected
    assert len(documents.platform_invoices) == len(expected)


def test_platform_invoice_lines_equal_delivered_spend(world: World, documents: Documents) -> None:
    spend = delivered_spend(world)
    for invoice in documents.platform_invoices:
        assert invoice.platform_key is not None
        by_ref = spend[(invoice.client_code, invoice.platform_key, invoice.period_start)]
        assert {line.campaign_ref: line.amount for line in invoice.lines} == by_ref
        assert invoice.subtotal == sum(by_ref.values())


def test_client_owned_clients_get_no_invoices(world: World, documents: Documents) -> None:
    owned = {c.code for c in world.clients if c.billing_model == "client_owned"}
    assert owned
    invoiced = {i.client_code for i in (*documents.platform_invoices, *documents.client_invoices)}
    assert not owned & invoiced


def test_platform_layouts_follow_platform(documents: Documents) -> None:
    assert {i.layout for i in documents.platform_invoices} == {"meta", "google", "tiktok"}
    assert all(i.layout == i.platform_key for i in documents.platform_invoices)


def test_vat_and_totals_are_consistent(documents: Documents, docs_config: DocumentsConfig) -> None:
    for doc in (*documents.platform_invoices, *documents.client_invoices, *documents.credit_notes):
        for amount in (doc.subtotal, doc.vat, doc.total):
            assert amount == amount.quantize(CENT)
        assert doc.vat == (doc.subtotal * docs_config.vat_rate).quantize(CENT, "ROUND_HALF_UP")
        assert doc.total == doc.subtotal + doc.vat


def test_vat_rounds_half_up() -> None:
    # 0.50 × 0.21 = 0.105 -> 0.11 (banker's rounding would give 0.10)
    assert vat_and_total(Decimal("0.50"), Decimal("0.21")) == (Decimal("0.11"), Decimal("0.61"))
    assert vat_and_total(Decimal("100.00"), Decimal("0.21")) == (
        Decimal("21.00"),
        Decimal("121.00"),
    )


# --- credit notes -------------------------------------------------------------------------


def test_credit_notes_link_to_platform_invoices(
    documents: Documents, docs_config: DocumentsConfig
) -> None:
    settings = docs_config.credit_notes
    originals = {i.number: i for i in documents.platform_invoices}
    assert len(documents.credit_notes) == settings.count
    for note in documents.credit_notes:
        original = originals[note.original_number]
        assert note.vendor == original.vendor
        assert note.client_code == original.client_code
        assert note.period_start == original.period_start
        assert note.issue_date > original.issue_date
        assert note.campaign_ref in {line.campaign_ref for line in original.lines}
        low = original.subtotal * settings.pct_min - CENT
        high = original.subtotal * settings.pct_max + CENT
        assert low <= note.subtotal <= high


def test_credit_note_count_is_config_driven(world: World, docs_config: DocumentsConfig) -> None:
    none = replace(docs_config, credit_notes=replace(docs_config.credit_notes, count=0))
    assert build_documents(world, none).credit_notes == ()


# --- client invoices ----------------------------------------------------------------------


def test_client_invoice_per_agency_billed_client_and_month(
    world: World, config: Config, documents: Documents
) -> None:
    billed = [c.code for c in world.clients if c.billing_model != "client_owned"]
    keys = {(i.client_code, i.period_start) for i in documents.client_invoices}
    assert keys == {(code, month) for code in billed for month in config.months}


def test_client_invoices_follow_billing_model(world: World, documents: Documents) -> None:
    clients = {c.code: c for c in world.clients}
    platform_by_key = defaultdict(list)
    for invoice in documents.platform_invoices:
        platform_by_key[(invoice.client_code, invoice.period_start)].append(invoice)
    credits = {n.original_number: n for n in documents.credit_notes}

    for invoice in documents.client_invoices:
        client = clients[invoice.client_code]
        sources = platform_by_key[(invoice.client_code, invoice.period_start)]
        media = sum((s.subtotal for s in sources), Decimal(0))
        credit = sum(
            (credits[s.number].subtotal for s in sources if s.number in credits), Decimal(0)
        )
        net_media = media - credit
        *media_lines, fee = invoice.lines
        assert sum(line.amount for line in media_lines) == net_media
        assert fee.campaign_ref is None
        if client.billing_model == "markup":
            assert fee.amount == (net_media * client.markup_pct / 100).quantize(
                CENT, "ROUND_HALF_UP"
            )
        else:
            assert fee.amount == client.mgmt_fee
        assert invoice.subtotal == net_media + fee.amount


def test_credit_notes_appear_on_client_invoices(documents: Documents) -> None:
    by_key = {(i.client_code, i.period_start): i for i in documents.client_invoices}
    for note in documents.credit_notes:
        invoice = by_key[(note.client_code, note.period_start)]
        credit_lines = [line for line in invoice.lines if note.number in line.description]
        assert len(credit_lines) == 1
        assert credit_lines[0].amount == -note.subtotal
        assert credit_lines[0].campaign_ref == note.campaign_ref


# --- numbers ------------------------------------------------------------------------------


def test_document_numbers_are_unique_per_vendor(documents: Documents) -> None:
    keys = [
        (doc.vendor, doc.number)
        for doc in (
            *documents.platform_invoices,
            *documents.client_invoices,
            *documents.credit_notes,
        )
    ]
    assert len(keys) == len(set(keys))


def test_different_seed_gives_different_numbers(
    config: Config, docs_config: DocumentsConfig, documents: Documents
) -> None:
    other = build_documents(build_world(replace(config, seed=config.seed + 1)), docs_config)
    platform_numbers = {i.number for i in documents.platform_invoices}
    assert platform_numbers.isdisjoint(i.number for i in other.platform_invoices)


# --- files on disk ------------------------------------------------------------------------


def test_ground_truth_json_matches_world(world: World, out_dir: Path, documents: Documents) -> None:
    spend = delivered_spend(world)
    for invoice in documents.platform_invoices:
        assert invoice.platform_key is not None
        truth = load_json(out_dir / invoice_dir(invoice) / f"{invoice.number}.json")
        lines = {line["campaign_ref"]: Decimal(line["amount"]) for line in truth["lines"]}
        assert lines == spend[(invoice.client_code, invoice.platform_key, invoice.period_start)]
        subtotal = Decimal(truth["subtotal"])
        assert subtotal == sum(lines.values())
        assert Decimal(truth["vat"]) == (subtotal * Decimal("0.21")).quantize(CENT, "ROUND_HALF_UP")
        assert Decimal(truth["total"]) == subtotal + Decimal(truth["vat"])
        assert truth["vendor"] == invoice.vendor
        assert truth["number"] == invoice.number
        assert truth["period_start"] == invoice.period_start.isoformat()
        assert truth["currency"] == "EUR"


def test_every_document_has_pdf_and_ground_truth(out_dir: Path) -> None:
    pdfs = sorted(p.with_suffix("") for p in out_dir.rglob("*.pdf"))
    jsons = sorted(p.with_suffix("") for p in out_dir.rglob("*.json") if p.name != MANIFEST_NAME)
    assert pdfs == jsons
    assert pdfs


def test_ground_truth_files_round_trip(out_dir: Path, documents: Documents) -> None:
    on_disk = {
        load_json(p)["number"]: load_json(p)
        for p in out_dir.rglob("*.json")
        if p.name != MANIFEST_NAME
    }
    for doc in (*documents.platform_invoices, *documents.client_invoices, *documents.credit_notes):
        truth = on_disk[doc.number]
        assert Decimal(truth["subtotal"]) == doc.subtotal
        assert Decimal(truth["total"]) == doc.total
    credit = [t for t in on_disk.values() if t["type"] == "credit_note"]
    assert {t["original_number"] for t in credit} <= set(on_disk)


def test_same_seed_gives_identical_files(output_dirs: tuple[Path, Path]) -> None:
    first, second = (load_json(d / MANIFEST_NAME) for d in output_dirs)
    assert first == second
    assert any(path.endswith(".pdf") for path in first["files"])
    assert any(path.endswith(".xlsx") for path in first["files"])


def test_pdf_text_contains_number_and_total(out_dir: Path) -> None:
    seen_layouts = set()
    for truth_path in sorted(out_dir.rglob("*.json")):
        if truth_path.name == MANIFEST_NAME:
            continue
        truth = load_json(truth_path)
        layout = truth["platform"] if truth.get("kind") != "client" else "agency"
        text = "\n".join(
            page.extract_text() for page in PdfReader(truth_path.with_suffix(".pdf")).pages
        )
        assert truth["number"] in text, truth_path
        assert format_amount(layout, Decimal(truth["total"])) in text, truth_path
        seen_layouts.add(layout)
    assert seen_layouts == {"meta", "google", "tiktok", "agency"}


# --- synthetic contact details (PII for masking tests, decision 037) ----------------------


def test_iban_check_digits_follow_iso_13616() -> None:
    assert iban_check_digits("GB", "WEST12345698765432") == "82"  # the ISO 13616 example
    assert iban_is_valid("GB82 WEST 1234 5698 7654 32")
    assert not iban_is_valid("GB83 WEST 1234 5698 7654 32")


def test_contacts_are_synthetic_and_valid(documents: Documents) -> None:
    invoices = (*documents.platform_invoices, *documents.client_invoices)
    for invoice in invoices:
        contact = invoice.contact
        assert iban_is_valid(contact.iban)
        assert len(contact.iban) == 20
        assert contact.iban.startswith("LT") and contact.iban[4:9] == FAKE_BANK_CODE
        assert contact.email.startswith("billing@") and contact.email.endswith(".example")
        assert contact.phone.startswith(PHONE_PREFIX)
        assert len(contact.phone) == len(PHONE_PREFIX) + 3
    assert len({i.contact for i in invoices}) == len({i.vendor for i in invoices})


def test_contacts_are_deterministic_per_seed_and_vendor(config: Config) -> None:
    assert vendor_contact(config.seed, "Meta") == vendor_contact(config.seed, "Meta")
    assert vendor_contact(config.seed, "Meta") != vendor_contact(config.seed, "Google")
    assert vendor_contact(config.seed, "Meta") != vendor_contact(config.seed + 1, "Meta")


def test_pdfs_show_the_contact_details(out_dir: Path, documents: Documents) -> None:
    for invoice in (*documents.platform_invoices, *documents.client_invoices):
        path = out_dir / invoice_dir(invoice) / f"{invoice.number}.pdf"
        text = "\n".join(page.extract_text() for page in PdfReader(path).pages)
        assert grouped_iban(invoice.contact.iban) in text, path
        assert invoice.contact.email in text, path
        assert invoice.contact.phone in text, path


def test_ground_truth_holds_no_contact_details(out_dir: Path, documents: Documents) -> None:
    """Data minimization: extraction never needs PII, so the ground truth never carries it."""
    for invoice in (*documents.platform_invoices, *documents.client_invoices):
        path = out_dir / invoice_dir(invoice) / f"{invoice.number}.json"
        raw = path.read_text(encoding="utf-8")
        assert not {"iban", "email", "phone", "contact"} & set(load_json(path))
        for value in (invoice.contact.iban, invoice.contact.email, invoice.contact.phone):
            assert value not in raw, path


def test_media_plan_round_trips_insertion_orders(world: World, out_dir: Path) -> None:
    platform_names = {p.key: p.name for p in world.platforms}
    for client in world.clients:
        sheet = load_workbook(out_dir / plan_path(client.code)).active
        assert sheet is not None
        header = next(sheet.iter_rows(min_row=HEADER_ROW, max_row=HEADER_ROW, values_only=True))
        assert header == PLAN_HEADERS
        rows = {
            (platform, ref, start.date(), end.date(), Decimal(str(budget)))
            for platform, ref, start, end, budget in sheet.iter_rows(
                min_row=HEADER_ROW + 1, values_only=True
            )
        }
        expected = {
            (
                platform_names[io.platform_key],
                io.campaign_ref,
                io.start_date,
                io.end_date,
                io.budget,
            )
            for io in world.insertion_orders
            if io.client_code == client.code
        }
        assert rows == expected


def test_generate_replaces_stale_files(
    world: World, docs_config: DocumentsConfig, tmp_path: Path
) -> None:
    stale = tmp_path / "invoices" / "stale.pdf"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old")
    generate_documents(world, docs_config, tmp_path)
    assert not stale.exists()


# --- config -------------------------------------------------------------------------------


@pytest.fixture
def raw_config() -> dict:
    return yaml.safe_load(Path(DEFAULT_CONFIG_PATH).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("documents", "vat_rate"), "1.5"),
        (("documents", "payment_terms_days"), -1),
        (("documents", "credit_notes", "count"), -1),
        (("documents", "credit_notes", "pct", "min"), "0"),
        (("documents", "credit_notes", "pct", "min"), "0.10"),
        (("agency", "invoice_prefix"), "MB-M"),
    ],
)
def test_invalid_documents_config_is_rejected(raw_config: dict, path: tuple, value: object) -> None:
    target = raw_config
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ConfigError):
        parse_documents_config(raw_config)


def test_documents_config_parses_money_as_decimal(docs_config: DocumentsConfig) -> None:
    assert docs_config.vat_rate == Decimal("0.21")
    assert isinstance(docs_config.credit_notes.pct_min, Decimal)
