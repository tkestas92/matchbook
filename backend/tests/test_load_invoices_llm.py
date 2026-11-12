"""The llm invoice source: PDFs through extraction into `core`, everything else to review.

Only FakeProvider answers; no real model and no network. Tests that take `seeded` need the
local Postgres (`make up`) and run on a throwaway database like the other pipeline tests.
"""

import json
import uuid
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from app.extraction.schema import (
    EXTRACTED,
    FAILED,
    NEEDS_REVIEW,
    REJECTED,
    ExtractedDocument,
    ExtractedLine,
    ExtractionResult,
)
from app.llm.client import LLMClient, LLMLimits
from app.llm.cost import CostLedger
from app.llm.fake import FakeProvider, Scripted
from pydantic import ValidationError
from sqlalchemy import Engine, text
from tests.conftest import PipelineDb
from tests.extraction_samples import (
    CONFIG,
    IBAN,
    KNOWN_VENDORS,
    META_ANSWER,
    MODEL,
    PHONE,
    PRICE,
    as_json,
    hand_built_invoice,
)

from generator.documents.contacts import VendorContact
from generator.documents.injection import INJECTION_VARIANTS
from generator.documents.pdf import render_invoice
from generator.world import DEFAULT_CONFIG_PATH
from pipelines import run as runner
from pipelines.extract_invoices import (
    DUPLICATE_NUMBER,
    MISSING_FIELD,
    PROPOSED_ACTION,
    UNKNOWN_CLIENT,
    UNKNOWN_PLATFORM,
    ReviewItem,
    SourceFile,
    discover_pdfs,
    known_vendors_from_config,
    line_confidence,
    load_invoices_llm,
    review_issue_row,
    review_list,
    safe_entity_id,
    to_credit_note_document,
    to_invoice_document,
)
from pipelines.load_invoices import GROUND_TRUTH, LLM, InvoiceSettings, Registry
from pipelines.normalize import RowError, run_normalize
from pipelines.pacing import pace
from pipelines.quality import run_quality_checks, write_findings
from pipelines.reconcile import reconcile
from pipelines.reference import load_reference
from pipelines.run import PARTIAL, PARTIAL_COUNTS, RunContext, run_cost, run_status

ROOT = Path(__file__).resolve().parents[2]
SHA = "a" * 64
REGISTRY = Registry(clients={"SAU": 1}, platforms={"google": 2, "meta": 3})
GOOGLE_REF = "SAU-GOOGLE-2605"
GOOGLE_ANSWER = {
    **META_ANSWER,
    "vendor": "Google",
    "number": "1234567890",
    "account_id": "100-000-123456789",
    "issue_date": "2026-06-01",
    "lines": [
        {"campaign_ref": GOOGLE_REF, "description": f"Campaign {GOOGLE_REF}", "amount": "1234.56"}
    ],
    "subtotal": "1234.56",
    "vat": "259.26",
    "total": "1493.82",
}
EXTRACTION_TYPES = {
    "extraction_needs_review",
    "extraction_rejected_suspicious",
    "extraction_failed",
}


def extracted_document(**changes: Any) -> ExtractedDocument:
    document = ExtractedDocument(
        type="invoice",
        kind="platform",
        vendor="Google",
        number="1234567890",
        client_code="SAU",
        platform="google",
        bill_to="UAB Matchbook Media",
        account_id="100000123456789",
        issue_date=date(2026, 6, 1),
        due_date=date(2026, 7, 1),
        period_start=date(2026, 5, 1),
        period_end=date(2026, 5, 31),
        currency="EUR",
        lines=(ExtractedLine(1, GOOGLE_REF, "Campaign", Decimal("1234.56")),),
        subtotal=Decimal("1234.56"),
        vat_rate=Decimal("0.21"),
        vat=Decimal("259.26"),
        total=Decimal("1493.82"),
        original_number=None,
        campaign_ref=None,
        reason=None,
    )
    return replace(document, **changes)


def extraction(document: ExtractedDocument) -> ExtractionResult:
    confidence = {
        "lines[0].amount": Decimal("0.95"),
        "lines[0].description": Decimal("0.90"),
        "lines[0].campaign_ref": Decimal("0.97"),
    }
    return ExtractionResult(
        status=EXTRACTED, reasons=(), document=document, field_confidence=confidence
    )


SOURCE = SourceFile("invoices/google/1234567890.pdf", SHA, b"")


# --- Pure: no database ---------------------------------------------------------------------


def test_invoice_source_defaults_to_ground_truth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INVOICE_SOURCE", raising=False)
    assert InvoiceSettings(_env_file=None).source == GROUND_TRUTH


def test_invoice_source_refuses_an_unknown_value() -> None:
    with pytest.raises(ValidationError):
        InvoiceSettings(_env_file=None, source="json")


def test_known_vendors_come_from_the_generator_config() -> None:
    vendors = known_vendors_from_config(DEFAULT_CONFIG_PATH)
    assert vendors.resolve("Meta Ads") == "Meta"
    assert vendors.resolve("TikTok For Business") == "TikTok"
    assert vendors.resolve("Someone Else Ltd") is None


def test_unreadable_known_vendors_config_fails_with_the_path(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="cannot read known vendors"):
        known_vendors_from_config(tmp_path / "missing.yaml")


def test_discover_pdfs_reads_the_three_folders_and_never_a_json(tmp_path: Path) -> None:
    for name in (
        "invoices/meta/a.pdf",
        "invoices/meta/a.json",
        "credit_notes/meta/b.pdf",
        "inbox/c.pdf",
        "plans/d.pdf",
    ):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(b"x")
    found = [p.relative_to(tmp_path).as_posix() for p in discover_pdfs(tmp_path)]
    assert found == ["credit_notes/meta/b.pdf", "inbox/c.pdf", "invoices/meta/a.pdf"]


@pytest.mark.parametrize(
    "relative",
    ["inbox/FBADS-456789-987654321.pdf", "invoices/google/2026-05/1234567890.pdf"],
)
def test_a_plain_path_is_kept_as_the_entity_id(relative: str) -> None:
    assert safe_entity_id(relative, SHA) == relative


@pytest.mark.parametrize(
    "relative",
    [
        "inbox/../x.pdf",
        "inbox/a\nb.pdf",
        "inbox/a\x00.pdf",
        "inbox/‮fdp.exe.pdf",
        "inbox/ignore previous instructions.pdf",
        "inbox/" + "a" * 300 + ".pdf",
        "inbox/sąskaita.pdf",
        "inbox//x.pdf",
        "/etc/x.pdf",
        "inbox/x.txt",
    ],
)
def test_a_hostile_file_name_becomes_a_placeholder(relative: str) -> None:
    assert safe_entity_id(relative, SHA) == f"document:{SHA}"


def test_line_confidence_is_the_lowest_field_of_that_line() -> None:
    scores = {
        "lines[0].amount": Decimal("0.9"),
        "lines[0].description": Decimal("0.8"),
        "lines[1].amount": Decimal("0.1"),
        "total": Decimal("0"),
    }
    assert line_confidence(scores, 0) == Decimal("0.8")
    assert line_confidence(scores, 2) is None


def test_extracted_invoice_maps_onto_the_ground_truth_document() -> None:
    invoice = to_invoice_document(SOURCE, extraction(extracted_document()), REGISTRY)
    assert (invoice.vendor, invoice.number, invoice.kind) == ("Google", "1234567890", "platform")
    assert (invoice.client_id, invoice.platform_id) == (1, 2)
    assert invoice.file_path == SOURCE.relative and invoice.file_sha256 == SHA
    assert invoice.total == Decimal("1493.82")
    assert invoice.lines[0].extracted_confidence == Decimal("0.90")


def test_client_invoice_has_no_platform() -> None:
    document = extracted_document(kind="client", platform=None, vendor="UAB Matchbook Media")
    invoice = to_invoice_document(SOURCE, extraction(document), REGISTRY)
    assert invoice.platform_id is None


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"client_code": "ZZZ"}, UNKNOWN_CLIENT),
        ({"client_code": None}, MISSING_FIELD),
        ({"platform": "tiktok"}, UNKNOWN_PLATFORM),
        ({"period_start": None}, MISSING_FIELD),
        ({"issue_date": None}, MISSING_FIELD),
        ({"lines": ()}, MISSING_FIELD),
    ],
)
def test_an_invoice_that_cannot_load_raises_a_fixed_code(
    changes: dict[str, Any], code: str
) -> None:
    with pytest.raises(RowError, match=f"^{code}$"):
        to_invoice_document(SOURCE, extraction(extracted_document(**changes)), REGISTRY)


def test_extracted_credit_note_amount_is_the_net_subtotal() -> None:
    document = extracted_document(
        type="credit_note",
        kind=None,
        number="GCN-2606-123456",
        original_number="1234567890",
        reason="Invalid traffic adjustment",
        lines=(),
        subtotal=Decimal("100.00"),
    )
    note = to_credit_note_document(SOURCE, extraction(document))
    assert (note.original_number, note.amount, note.file_sha256) == (
        "1234567890",
        Decimal("100.00"),
        SHA,
    )


def test_credit_note_without_an_original_number_cannot_load() -> None:
    document = extracted_document(type="credit_note", kind=None, original_number=None)
    with pytest.raises(RowError, match=MISSING_FIELD):
        to_credit_note_document(SOURCE, extraction(document))


@pytest.mark.parametrize(
    ("status", "issue_type", "severity"),
    [
        (NEEDS_REVIEW, "extraction_needs_review", "medium"),
        (REJECTED, "extraction_rejected_suspicious", "high"),
        (FAILED, "extraction_failed", "medium"),
    ],
)
def test_review_issue_holds_codes_and_the_sha_key(
    status: str, issue_type: str, severity: str
) -> None:
    run_id = uuid.uuid4()
    item = ReviewItem("document:" + SHA, SHA, status, ("content:url:text", "grounding:total"))
    row = review_issue_row(run_id, item)
    assert row["dedup_key"] == f"extraction:{SHA}"
    assert (row["type"], row["severity"]) == (issue_type, severity)
    assert row["entity_id"] == "document:" + SHA
    assert "content:url:text, grounding:total" in row["explanation"]
    assert row["proposed_action"] == PROPOSED_ACTION
    assert row["amount"] is None and row["run_id"] == run_id


def test_review_list_holds_the_entity_status_and_codes_only() -> None:
    item = ReviewItem("inbox/x.pdf", SHA, REJECTED, ("pdf:hidden_text",), client_id=1)
    assert review_list([item]) == [
        {"document": "inbox/x.pdf", "status": REJECTED, "reasons": ["pdf:hidden_text"]}
    ]


def test_run_cost_sums_decimal_strings_exactly() -> None:
    steps = [
        {"counts": {"llm_cost_eur": "0.000150", "llm_tokens": 150}},
        {"counts": {"rows": 3}},
        {"counts": {"llm_cost_eur": "0.100001", "llm_tokens": 7}},
    ]
    assert run_cost(steps) == (Decimal("0.100151"), 157)


def test_skipped_documents_make_a_run_partial() -> None:
    assert {"skipped_cost_cap", "skipped_provider_error"} <= set(PARTIAL_COUNTS)
    steps = [{"name": "load_invoices", "status": "succeeded", "counts": {"skipped_cost_cap": 2}}]
    assert run_status(steps) == PARTIAL


def test_review_items_do_not_make_a_run_partial() -> None:
    counts = {"needs_review": 1, "rejected": 2, "failed": 1, "skipped_unchanged": 9}
    steps = [{"name": "load_invoices", "status": "succeeded", "counts": counts}]
    assert run_status(steps) != PARTIAL


def stub_context(source: str) -> RunContext:
    def refuse(ctx: RunContext) -> LLMClient:
        raise AssertionError("an LLM client was built")

    return RunContext(
        run_id=uuid.uuid4(),
        data_dir=Path("unused"),
        etl=None,  # type: ignore[arg-type]
        app=None,  # type: ignore[arg-type]
        client=None,  # type: ignore[arg-type]
        ingest_settings=None,  # type: ignore[arg-type]
        agency_timezone="Europe/Vilnius",
        invoice_settings=InvoiceSettings(_env_file=None, source=source),
        llm_client_factory=refuse,
    )


def test_ground_truth_mode_never_builds_an_llm_client(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(
        runner, "load_invoices", lambda *args: called.append("json") or LoadedStub()
    )
    monkeypatch.setattr(runner, "load_invoices_llm", lambda *args: called.append("llm"))
    counts = runner.step_load_invoices(stub_context(GROUND_TRUTH))
    assert called == ["json"] and counts["invoices_written"] == 0


def test_llm_mode_dispatches_to_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "load_invoices", lambda *args: pytest.fail("json loader ran"))
    with pytest.raises(AssertionError, match="an LLM client was built"):
        runner.step_load_invoices(stub_context(LLM))


class LoadedStub:
    invoices_written = lines_written = lines_removed = credit_notes_written = 0
    rejected: list[Any] = []


def test_no_other_code_builds_or_matches_extraction_issue_keys() -> None:
    """Nothing but the llm loader writes, reads or closes `extraction:` issues (decision 045)."""
    owner = ROOT / "pipelines/extract_invoices.py"
    sources = [*(ROOT / "pipelines").rglob("*.py"), *(ROOT / "backend/app").rglob("*.py")]
    sources += [*(ROOT / "orchestration").rglob("*.py"), *(ROOT / "mcp_server").rglob("*.py")]
    for path in sources:
        if path != owner:
            assert "extraction:" not in path.read_text(encoding="utf-8"), path


# --- With the database ---------------------------------------------------------------------

REFERENCE = {
    "clients": [
        {
            "code": "SAU",
            "name": "UAB Sau",
            "industry": "food",
            "billing_model": "markup",
            "markup_pct": "10.00",
            "mgmt_fee": None,
        }
    ],
    "platforms": [
        {"name": "google", "timezone": "Europe/Vilnius"},
        {"name": "meta", "timezone": "Europe/Vilnius"},
    ],
    "ad_accounts": [
        {
            "client_code": "SAU",
            "platform": "google",
            "external_account_id": "100000123456789",
            "timezone": "Europe/Vilnius",
        }
    ],
}
TABLES = ("core.invoices", "core.invoice_lines", "core.credit_notes", "app.issues")


def make_client(script: list[Scripted], run_cap: str = "1") -> tuple[LLMClient, FakeProvider]:
    fake = FakeProvider(script)
    client = LLMClient(
        provider=fake,
        limits=LLMLimits(model=MODEL, prices={MODEL: PRICE}),
        ledger=CostLedger(run_cap=Decimal(run_cap), monthly_cap=Decimal("10")),
        sleep=lambda _: None,
    )
    return client, fake


@pytest.fixture
def seeded(pipeline_db: PipelineDb, tmp_path: Path) -> PipelineDb:
    path = tmp_path / "reference.json"
    path.write_text(json.dumps(REFERENCE), encoding="utf-8")
    load_reference(uuid.uuid4(), path, pipeline_db.etl)
    with pipeline_db.owner.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO core.insertion_orders (client_id, platform_id, campaign_ref, "
                "start_date, end_date, budget, run_id) "
                "SELECT c.id, p.id, :ref, '2026-05-01', '2026-05-31', 1500.00, :run_id "
                "FROM core.clients c, core.platforms p WHERE c.code = 'SAU' AND p.name = 'google'"
            ),
            {"ref": GOOGLE_REF, "run_id": uuid.uuid4()},
        )
    return pipeline_db


def render(path: Path, **changes: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    invoice = replace(hand_built_invoice("google"), **changes)
    render_invoice(invoice, path, "LT100000000000")
    return path


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """One benign, one needs-review, one injected and one unreadable PDF."""
    root = tmp_path / "generated"
    render(root / "invoices/google/1234567890.pdf")
    render(root / "invoices/google/1234567891.pdf", number="1234567891")
    malicious = root / "inbox/1234567892.pdf"
    malicious.parent.mkdir(parents=True)
    variant = sorted(INJECTION_VARIANTS)[0]
    render_invoice(
        replace(hand_built_invoice("meta"), number="FBADS-456789-987654322"),
        malicious,
        "LT100000000000",
        INJECTION_VARIANTS[variant],
    )
    (root / "inbox/broken.pdf").write_bytes(b"not a pdf")
    return root


FIRST_SCRIPT: list[Scripted] = [
    as_json(GOOGLE_ANSWER),
    as_json(GOOGLE_ANSWER, number="1234567891", total="9999.99"),  # Not printed: review.
]


def load(db: PipelineDb, data_dir: Path, client: LLMClient) -> Any:
    return load_invoices_llm(uuid.uuid4(), data_dir, db.etl, db.app, client, CONFIG, KNOWN_VENDORS)


def counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in TABLES}


def extraction_issues(engine: Engine) -> list[Any]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT dedup_key, type, severity, explanation, entity_id, status, run_id "
                    "FROM app.issues WHERE dedup_key LIKE 'extraction:%' ORDER BY dedup_key"
                )
            )
        )


def test_only_extracted_documents_reach_core(seeded: PipelineDb, data_dir: Path) -> None:
    client, fake = make_client(list(FIRST_SCRIPT))
    result = load(seeded, data_dir, client)

    assert len(fake.requests) == 2  # The injected and the unreadable PDF never reach the model.
    assert dict(result.statuses) == {EXTRACTED: 1, NEEDS_REVIEW: 1, REJECTED: 1, FAILED: 1}
    with seeded.owner.connect() as conn:
        invoice = conn.execute(
            text("SELECT number, status, file_path, file_sha256 FROM core.invoices")
        ).one()
        confidence = conn.execute(
            text("SELECT extracted_confidence FROM core.invoice_lines")
        ).scalar_one()
    assert invoice.number == "1234567890" and invoice.status == "extracted"
    assert invoice.file_path == "invoices/google/1234567890.pdf"
    assert len(invoice.file_sha256) == 64
    assert confidence is not None
    assert {row.type for row in extraction_issues(seeded.owner)} == EXTRACTION_TYPES
    assert result.llm_calls == 2 and result.llm_cost_eur > 0 and result.llm_tokens > 0


def test_review_issues_hold_no_document_text(seeded: PipelineDb, data_dir: Path) -> None:
    client, _ = make_client(list(FIRST_SCRIPT))
    load(seeded, data_dir, client)
    for row in extraction_issues(seeded.owner):
        assert "9999.99" not in row.explanation and "1234567891" not in row.explanation
        assert row.status == "open"


def test_second_run_makes_no_model_call_and_keeps_rows(seeded: PipelineDb, data_dir: Path) -> None:
    client, _ = make_client(list(FIRST_SCRIPT))
    load(seeded, data_dir, client)
    before = counts(seeded.owner)

    client, fake = make_client([])
    result = load(seeded, data_dir, client)

    assert len(fake.requests) == 0
    assert result.skipped_unchanged == 4 and result.llm_calls == 0
    assert counts(seeded.owner) == before


def test_changed_file_is_extracted_again_and_upserted(seeded: PipelineDb, data_dir: Path) -> None:
    client, _ = make_client(list(FIRST_SCRIPT))
    load(seeded, data_dir, client)
    with seeded.owner.connect() as conn:
        old_sha = conn.execute(text("SELECT file_sha256 FROM core.invoices")).scalar_one()

    contact = VendorContact(iban=IBAN.replace(" ", ""), email="other@vendor.example", phone=PHONE)
    render(data_dir / "invoices/google/1234567890.pdf", contact=contact)
    client, fake = make_client([as_json(GOOGLE_ANSWER)])
    result = load(seeded, data_dir, client)

    assert len(fake.requests) == 1 and result.skipped_unchanged == 3
    with seeded.owner.connect() as conn:
        rows = conn.execute(text("SELECT file_sha256 FROM core.invoices")).scalars().all()
    assert len(rows) == 1 and rows[0] != old_sha


def test_cost_cap_skips_the_remaining_documents(seeded: PipelineDb, data_dir: Path) -> None:
    client, fake = make_client(list(FIRST_SCRIPT), run_cap="0.000001")
    result = load(seeded, data_dir, client)

    assert len(fake.requests) == 0
    assert result.skipped_cost_cap == 2
    assert counts(seeded.owner)["core.invoices"] == 0
    # Only the two documents that never needed the model have issues; the rest wait.
    types = {row.type for row in extraction_issues(seeded.owner)}
    assert types == {"extraction_rejected_suspicious", "extraction_failed"}


def test_llm_mode_never_opens_a_json(
    seeded: PipelineDb, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (data_dir / "invoices/google/1234567890.json").write_text("{}", encoding="utf-8")
    real_open = Path.open

    def guarded_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.suffix == ".json":
            raise AssertionError(f"opened {self.name}")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    client, _ = make_client(list(FIRST_SCRIPT))
    load(seeded, data_dir, client)


def test_duplicate_number_in_a_second_file_goes_to_review(
    seeded: PipelineDb, data_dir: Path
) -> None:
    client, _ = make_client([as_json(GOOGLE_ANSWER), as_json(GOOGLE_ANSWER)])
    (data_dir / "invoices/google/1234567891.pdf").unlink()
    contact = VendorContact(iban=IBAN.replace(" ", ""), email="copy@vendor.example", phone=PHONE)
    render(data_dir / "invoices/google/copy.pdf", contact=contact)
    result = load(seeded, data_dir, client)
    assert result.invoices_written == 1
    reasons = [item.reasons for item in result.review if item.status == NEEDS_REVIEW]
    assert reasons == [(DUPLICATE_NUMBER,)]


def test_run_record_gets_the_llm_cost_and_tokens(pipeline_db: PipelineDb) -> None:
    ctx = replace(stub_context(LLM), etl=pipeline_db.etl, app=pipeline_db.app)
    steps = (("load_invoices", lambda _: {"llm_cost_eur": "0.001234", "llm_tokens": 321}),)
    runner.execute(ctx, steps)
    with pipeline_db.owner.connect() as conn:
        row = conn.execute(
            text("SELECT cost_eur, tokens FROM app.pipeline_runs WHERE id = :id"),
            {"id": ctx.run_id},
        ).one()
    assert (row.cost_eur, row.tokens) == (Decimal("0.001234"), 321)


def test_later_steps_leave_extraction_issues_alone(seeded: PipelineDb, data_dir: Path) -> None:
    client, _ = make_client(list(FIRST_SCRIPT))
    load(seeded, data_dir, client)
    before = extraction_issues(seeded.owner)

    run_id = uuid.uuid4()
    write_findings(run_id, run_quality_checks(run_normalize(run_id, seeded.etl)), seeded.app)
    reconcile(run_id, seeded.etl, seeded.app, "Europe/Vilnius")
    pace(run_id, seeded.app, date(2026, 5, 31))

    assert extraction_issues(seeded.owner) == before
