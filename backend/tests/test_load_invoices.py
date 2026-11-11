"""Invoice and credit note documents into `core.*`, loaded exactly as they are written."""

import json
import uuid
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text
from tests.conftest import PipelineDb

from generator.documents.contacts import vendor_contact
from generator.documents.model import (
    CreditNote,
    Invoice,
    InvoiceLine,
    credit_note_ground_truth,
    invoice_ground_truth,
)
from generator.documents.writer import credit_note_dir, ensure_parent, invoice_dir, write_json
from pipelines import load_invoices as load_invoices_module
from pipelines.load_invoices import (
    json_path,
    load_invoices,
    load_invoices_from_env,
    parse_credit_note,
    parse_lines,
    parse_money,
)
from pipelines.normalize import RowError
from pipelines.reference import load_reference

AGENCY = "UAB Matchbook Media"
TABLES = ("core.invoices", "core.invoice_lines", "core.credit_notes")
REFERENCE = {
    "clients": [
        {
            "code": "AAA",
            "name": "UAB Aaa",
            "industry": "food",
            "billing_model": "markup",
            "markup_pct": "10.00",
            "mgmt_fee": None,
        }
    ],
    "platforms": [{"name": "meta", "timezone": "Europe/Vilnius"}],
    "ad_accounts": [
        {
            "client_code": "AAA",
            "platform": "meta",
            "external_account_id": "1001",
            "timezone": "Europe/Vilnius",
        }
    ],
}


def line(
    no: int, amount: str, ref: str | None = "AAA-META-2605", text_: str = "Media"
) -> InvoiceLine:
    return InvoiceLine(no, ref, text_, Decimal(amount))


def platform_invoice(number: str = "M-1", amount: str = "100.00", **changes: Any) -> Invoice:
    subtotal = Decimal(amount)
    vat = (subtotal * Decimal("0.21")).quantize(Decimal("0.01"))
    invoice = Invoice(
        kind="platform",
        layout="meta",
        vendor="Meta",
        number=number,
        client_code="AAA",
        platform_key="meta",
        bill_to=AGENCY,
        account_id="1001",
        issue_date=date(2026, 6, 3),
        due_date=date(2026, 7, 3),
        period_start=date(2026, 5, 1),
        period_end=date(2026, 5, 31),
        lines=(line(1, amount),),
        subtotal=subtotal,
        vat_rate=Decimal("0.21"),
        vat=vat,
        total=subtotal + vat,
        contact=vendor_contact(1, "Meta"),
    )
    return replace(invoice, **changes)


def client_invoice(number: str = "AG-AAA-2605") -> Invoice:
    lines = (
        line(1, "100.00"),
        line(2, "-10.00", text_="Credit note CN-1 (Meta)"),
        line(3, "17.10", None, "Agency commission 19.00% on media"),
    )
    return replace(
        platform_invoice(),
        kind="client",
        layout="agency",
        vendor=AGENCY,
        number=number,
        platform_key=None,
        bill_to="UAB Aaa",
        account_id=None,
        lines=lines,
        subtotal=Decimal("107.10"),
        vat=Decimal("22.49"),
        total=Decimal("129.59"),
    )


def credit_note(number: str = "CN-1", original: str = "M-1") -> CreditNote:
    return CreditNote(
        vendor="Meta",
        number=number,
        original_number=original,
        client_code="AAA",
        platform_key="meta",
        bill_to=AGENCY,
        issue_date=date(2026, 6, 10),
        period_start=date(2026, 5, 1),
        period_end=date(2026, 5, 31),
        campaign_ref="AAA-META-2605",
        reason="Invalid traffic adjustment",
        subtotal=Decimal("10.00"),
        vat_rate=Decimal("0.21"),
        vat=Decimal("2.10"),
        total=Decimal("12.10"),
    )


def write_invoice(data_dir: Path, invoice: Invoice) -> Path:
    path = ensure_parent(data_dir / invoice_dir(invoice) / f"{invoice.number}.json")
    write_json(path, invoice_ground_truth(invoice))
    return path


def write_note(data_dir: Path, note: CreditNote) -> Path:
    path = ensure_parent(data_dir / credit_note_dir(note) / f"{note.number}.json")
    write_json(path, credit_note_ground_truth(note))
    return path


def counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in TABLES}


@pytest.fixture
def seeded(pipeline_db: PipelineDb, tmp_path: Path) -> PipelineDb:
    path = tmp_path / "reference.json"
    path.write_text(json.dumps(REFERENCE), encoding="utf-8")
    load_reference(uuid.uuid4(), path, pipeline_db.etl)
    return pipeline_db


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    root = tmp_path / "generated"
    (root / "invoices").mkdir(parents=True)
    return root


def test_load_writes_all_documents_as_etl(seeded: PipelineDb, data_dir: Path) -> None:
    run_id = uuid.uuid4()
    write_invoice(data_dir, platform_invoice())
    write_invoice(data_dir, client_invoice())
    write_note(data_dir, credit_note())
    result = load_invoices(run_id, data_dir, seeded.etl)
    assert result.rejected == []
    assert (result.invoices_written, result.lines_written, result.credit_notes_written) == (2, 4, 1)
    with seeded.owner.connect() as conn:
        invoice = conn.execute(
            text(
                "SELECT i.kind, c.code, p.name, i.subtotal, i.vat, i.total, i.status, i.file_path, "
                "i.run_id FROM core.invoices i JOIN core.clients c ON c.id = i.client_id "
                "JOIN core.platforms p ON p.id = i.platform_id WHERE i.number = 'M-1'"
            )
        ).one()
        note = conn.execute(
            text(
                "SELECT i.number, n.amount, n.reason, n.file_path FROM core.credit_notes n "
                "JOIN core.invoices i ON i.id = n.original_invoice_id"
            )
        ).one()
    assert tuple(invoice) == (
        "platform",
        "AAA",
        "meta",
        Decimal("100.00"),
        Decimal("21.00"),
        Decimal("121.00"),
        "received",
        "invoices/platform/meta/2026-05/M-1.pdf",
        run_id,
    )
    assert tuple(note) == (
        "M-1",
        Decimal("10.00"),
        "Invalid traffic adjustment",
        "credit_notes/meta/2026-05/CN-1.pdf",
    )


def test_client_invoice_keeps_fee_and_negative_lines(seeded: PipelineDb, data_dir: Path) -> None:
    write_invoice(data_dir, client_invoice())
    load_invoices(uuid.uuid4(), data_dir, seeded.etl)
    with seeded.owner.connect() as conn:
        platform_id = conn.execute(text("SELECT platform_id FROM core.invoices")).scalar_one()
        rows = conn.execute(
            text("SELECT campaign_ref, amount FROM core.invoice_lines ORDER BY line_no")
        ).all()
    assert platform_id is None
    assert [tuple(r) for r in rows] == [
        ("AAA-META-2605", Decimal("100.00")),
        ("AAA-META-2605", Decimal("-10.00")),
        (None, Decimal("17.10")),
    ]


def test_duplicate_invoice_is_loaded_as_a_second_row(seeded: PipelineDb, data_dir: Path) -> None:
    write_invoice(data_dir, platform_invoice("M-1"))
    write_invoice(data_dir, platform_invoice("M-2", issue_date=date(2026, 6, 20)))
    result = load_invoices(uuid.uuid4(), data_dir, seeded.etl)
    assert (result.invoices_written, result.rejected) == (2, [])
    with seeded.owner.connect() as conn:
        rows = conn.execute(
            text("SELECT vendor, period_start, total FROM core.invoices ORDER BY number")
        ).all()
    assert len(rows) == 2 and rows[0] == rows[1]


def test_inconsistent_documents_are_stored_as_written(seeded: PipelineDb, data_dir: Path) -> None:
    wrong = replace(client_invoice(), total=Decimal("1.00"))
    write_invoice(data_dir, wrong)
    write_invoice(data_dir, platform_invoice())
    write_note(data_dir, credit_note())  # The client invoice need not apply it.
    result = load_invoices(uuid.uuid4(), data_dir, seeded.etl)
    assert result.rejected == []
    with seeded.owner.connect() as conn:
        total = conn.execute(
            text("SELECT total FROM core.invoices WHERE number = :n"), {"n": wrong.number}
        ).scalar_one()
    assert total == Decimal("1.00")


def test_load_twice_with_new_run_id_keeps_counts_and_updates_run_id(
    seeded: PipelineDb, data_dir: Path
) -> None:
    write_invoice(data_dir, platform_invoice())
    write_invoice(data_dir, client_invoice())
    write_note(data_dir, credit_note())
    load_invoices(uuid.uuid4(), data_dir, seeded.etl)
    first = counts(seeded.owner)
    second_run = uuid.uuid4()
    load_invoices(second_run, data_dir, seeded.etl)
    assert counts(seeded.owner) == first == dict(zip(TABLES, (2, 4, 1), strict=True))
    with seeded.owner.connect() as conn:
        for table in TABLES:
            run_ids = conn.execute(text(f"SELECT DISTINCT run_id FROM {table}")).scalars().all()
            assert run_ids == [second_run]


def test_reload_keeps_columns_owned_by_later_steps(seeded: PipelineDb, data_dir: Path) -> None:
    write_invoice(data_dir, platform_invoice())
    load_invoices(uuid.uuid4(), data_dir, seeded.etl)
    with seeded.owner.begin() as conn:
        campaign_id = conn.execute(
            text(
                "INSERT INTO core.campaigns "
                "(platform_id, ad_account_id, platform_campaign_id, name, run_id) "
                "SELECT platform_id, id, 'c-1', 'Name', :r FROM core.ad_accounts RETURNING id"
            ),
            {"r": uuid.uuid4()},
        ).scalar_one()
        conn.execute(text("UPDATE core.invoices SET status = 'approved'"))
        conn.execute(
            text("UPDATE core.invoice_lines SET campaign_id = :c, extracted_confidence = 0.9"),
            {"c": campaign_id},
        )
    load_invoices(uuid.uuid4(), data_dir, seeded.etl)
    with seeded.owner.connect() as conn:
        status = conn.execute(text("SELECT status FROM core.invoices")).scalar_one()
        kept = conn.execute(
            text("SELECT campaign_id, extracted_confidence FROM core.invoice_lines")
        ).one()
    assert status == "approved"
    assert tuple(kept) == (campaign_id, Decimal("0.900"))


def test_reload_removes_lines_the_document_no_longer_has(
    seeded: PipelineDb, data_dir: Path
) -> None:
    two_lines = platform_invoice(lines=(line(1, "60.00"), line(2, "40.00")))
    write_invoice(data_dir, two_lines)
    load_invoices(uuid.uuid4(), data_dir, seeded.etl)
    write_invoice(data_dir, platform_invoice())
    result = load_invoices(uuid.uuid4(), data_dir, seeded.etl)
    assert result.lines_removed == 1
    assert counts(seeded.owner)["core.invoice_lines"] == 1


def test_invoice_missing_from_a_later_run_stays(seeded: PipelineDb, data_dir: Path) -> None:
    path = write_invoice(data_dir, platform_invoice())
    load_invoices(uuid.uuid4(), data_dir, seeded.etl)
    path.unlink()
    load_invoices(uuid.uuid4(), data_dir, seeded.etl)
    assert counts(seeded.owner)["core.invoices"] == 1


def test_note_for_an_unknown_invoice_is_rejected(seeded: PipelineDb, data_dir: Path) -> None:
    write_note(data_dir, credit_note(original="NOPE"))
    result = load_invoices(uuid.uuid4(), data_dir, seeded.etl)
    assert [(r.path, r.reason) for r in result.rejected] == [
        ("credit_notes/meta/2026-05/CN-1.json", "Unknown original invoice 'NOPE'")
    ]
    assert counts(seeded.owner)["core.credit_notes"] == 0


def test_note_resolves_an_invoice_from_an_earlier_run(seeded: PipelineDb, data_dir: Path) -> None:
    path = write_invoice(data_dir, platform_invoice())
    load_invoices(uuid.uuid4(), data_dir, seeded.etl)
    path.unlink()
    write_note(data_dir, credit_note())
    assert load_invoices(uuid.uuid4(), data_dir, seeded.etl).credit_notes_written == 1


def test_same_vendor_and_number_in_two_files_is_reported(
    seeded: PipelineDb, data_dir: Path
) -> None:
    write_invoice(data_dir, platform_invoice())
    moved = platform_invoice(period_start=date(2026, 6, 1), period_end=date(2026, 6, 30))
    write_invoice(data_dir, moved)
    result = load_invoices(uuid.uuid4(), data_dir, seeded.etl)
    assert result.invoices_written == 1
    assert [r.path for r in result.rejected] == ["invoices/platform/meta/2026-06/M-1.json"]
    with seeded.owner.connect() as conn:
        kept = conn.execute(text("SELECT period_start FROM core.invoices")).scalar_one()
    assert kept == date(2026, 5, 1)


def test_float_money_rejects_the_document(seeded: PipelineDb, data_dir: Path) -> None:
    path = write_invoice(data_dir, platform_invoice())
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["total"] = 121.0
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = load_invoices(uuid.uuid4(), data_dir, seeded.etl)
    assert [r.reason for r in result.rejected] == ["total is not a decimal string"]
    assert counts(seeded.owner)["core.invoices"] == 0


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"client_code": "ZZZ"}, "Unknown client code 'ZZZ'"),
        ({"platform": "snapchat"}, "Unknown platform 'snapchat'"),
        ({"currency": "USD"}, "Currency is not EUR"),
        ({"type": "credit_note"}, "Document type is not invoice"),
        ({"kind": "other"}, "kind is not platform or client"),
        ({"issue_date": "03/06/2026"}, "issue_date does not match %Y-%m-%d"),
        ({"lines": []}, "lines is not a non-empty list"),
    ],
)
def test_bad_document_is_rejected_whole(
    seeded: PipelineDb, data_dir: Path, change: dict[str, Any], reason: str
) -> None:
    path = write_invoice(data_dir, platform_invoice())
    payload = {**json.loads(path.read_text(encoding="utf-8")), **change}
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = load_invoices(uuid.uuid4(), data_dir, seeded.etl)
    assert [r.reason for r in result.rejected] == [reason]
    assert counts(seeded.owner) == dict.fromkeys(TABLES, 0)


def test_unparseable_files_outside_the_document_folders_are_ignored(
    seeded: PipelineDb, data_dir: Path
) -> None:
    (data_dir / "answer_key.json").write_text("{not json", encoding="utf-8")
    write_invoice(data_dir, platform_invoice())
    result = load_invoices(uuid.uuid4(), data_dir, seeded.etl)
    assert (result.invoices_written, result.rejected) == (1, [])


def test_unreadable_json_is_rejected(seeded: PipelineDb, data_dir: Path) -> None:
    path = write_invoice(data_dir, platform_invoice())
    path.write_text("{not json", encoding="utf-8")
    result = load_invoices(uuid.uuid4(), data_dir, seeded.etl)
    assert [r.reason for r in result.rejected] == ["File is not readable JSON"]


def test_missing_invoices_directory_fails(seeded: PipelineDb, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_invoices(uuid.uuid4(), tmp_path / "absent", seeded.etl)


def test_load_from_env_uses_the_etl_engine(
    seeded: PipelineDb, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(load_invoices_module, "create_etl_engine", lambda: seeded.etl)
    write_invoice(data_dir, platform_invoice())
    assert load_invoices_from_env(uuid.uuid4(), data_dir).invoices_written == 1


def test_parse_money_is_exact_and_keeps_the_sign() -> None:
    parsed = parse_money("-1234.56", "amount")
    assert isinstance(parsed, Decimal)
    assert str(parsed) == "-1234.56"


@pytest.mark.parametrize("value", [None, 12.5, 12, "1e3", "abc"])
def test_parse_money_rejects_anything_but_decimal_text(value: object) -> None:
    with pytest.raises(RowError):
        parse_money(value, "amount")


def test_parse_lines_rejects_a_repeated_line_no() -> None:
    raw = {"line_no": 1, "campaign_ref": None, "description": "x", "amount": "1.00"}
    with pytest.raises(RowError, match="repeated"):
        parse_lines([raw, raw])


def test_credit_note_amount_is_net_and_positive(tmp_path: Path) -> None:
    note = parse_credit_note(credit_note_ground_truth(credit_note()), Path("credit_notes/a.json"))
    assert note.amount == Decimal("10.00")
    assert json_path(note) == "credit_notes/a.json"


def test_source_never_mentions_the_answer_key() -> None:
    source = Path(load_invoices_module.__file__).read_text(encoding="utf-8")
    assert "answer_key" not in source
