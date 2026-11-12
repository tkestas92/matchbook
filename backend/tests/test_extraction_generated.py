"""Extraction guards against every generated document (task 2.5, threat model T1).

Like test_masking.py, this builds the world, documents and planted errors in memory and
renders every PDF into a tmp dir with the generator. It never reads data/generated, never
writes answer_key.json and never touches `planted.answer_key`: only `planted.documents` and
`planted.malicious` are used.

Benign documents must not trip any rule (a false rejection hides a real invoice from
reconciliation); every inbox copy must be rejected before the model is called.
"""

from dataclasses import dataclass
from pathlib import Path

import pytest
from app.extraction.extract import extract_invoice
from app.extraction.guards import scan
from app.extraction.pdf_text import read_pdf
from tests.extraction_samples import CONFIG, KNOWN_VENDORS, make_client

from generator.documents.billing import build_documents
from generator.documents.config import DocumentsConfig, load_documents_config
from generator.documents.injection import INJECTION_VARIANTS
from generator.documents.model import CreditNote, Invoice
from generator.documents.pdf import format_amount
from generator.documents.writer import credit_note_dir, inbox_dir, invoice_dir, write_outputs
from generator.errors import load_errors_config, plant_errors
from generator.world import World, build_world, load_config


@dataclass(frozen=True)
class RenderedDocument:
    path: Path
    document: Invoice | CreditNote
    variant: str | None  # injection variant for inbox copies, None for benign documents

    @property
    def layout(self) -> str:
        if isinstance(self.document, CreditNote):
            return f"credit_note/{self.document.platform_key}"
        return self.document.layout

    def label(self) -> str:
        return f"{self.layout} {self.path.name}"


@pytest.fixture(scope="module")
def rendered(tmp_path_factory: pytest.TempPathFactory) -> list[RenderedDocument]:
    world: World = build_world(load_config())
    docs_config: DocumentsConfig = load_documents_config()
    planted = plant_errors(world, build_documents(world, docs_config), load_errors_config())
    inbox = [(m.invoice, INJECTION_VARIANTS[m.variant]) for m in planted.malicious]
    out = tmp_path_factory.mktemp("extraction")
    write_outputs(world, planted.documents, docs_config, out, inbox=inbox)

    documents: list[RenderedDocument] = []
    for invoice in (*planted.documents.platform_invoices, *planted.documents.client_invoices):
        documents.append(
            RenderedDocument(out / invoice_dir(invoice) / f"{invoice.number}.pdf", invoice, None)
        )
    for malicious in planted.malicious:
        path = out / inbox_dir(malicious.invoice) / f"{malicious.invoice.number}.pdf"
        documents.append(RenderedDocument(path, malicious.invoice, malicious.variant))
    for note in planted.documents.credit_notes:
        documents.append(
            RenderedDocument(out / credit_note_dir(note) / f"{note.number}.pdf", note, None)
        )

    assert {doc.path for doc in documents} == set(out.rglob("*.pdf")), "every PDF is paired"
    return sorted(documents, key=lambda doc: doc.path)


def benign(rendered: list[RenderedDocument]) -> list[RenderedDocument]:
    return [doc for doc in rendered if doc.variant is None]


def malicious(rendered: list[RenderedDocument]) -> list[RenderedDocument]:
    return [doc for doc in rendered if doc.variant is not None]


def printed_total(doc: RenderedDocument) -> str:
    document = doc.document
    layout = document.platform_key if isinstance(document, CreditNote) else document.layout
    return format_amount(layout, document.total)


def test_every_kind_of_document_and_every_variant_is_covered(
    rendered: list[RenderedDocument],
) -> None:
    layouts = {doc.layout for doc in benign(rendered)}
    assert {"meta", "google", "tiktok", "agency"} <= layouts
    assert any(layout.startswith("credit_note/") for layout in layouts)
    assert {doc.variant for doc in malicious(rendered)} == set(INJECTION_VARIANTS)


def test_benign_documents_have_no_findings_and_keep_their_total(
    rendered: list[RenderedDocument],
) -> None:
    false_rejections = []
    for doc in benign(rendered):
        pdf_text = read_pdf(doc.path.read_bytes(), CONFIG.max_pdf_bytes, CONFIG.max_pages)
        findings = pdf_text.findings + scan(pdf_text.text)
        false_rejections += [f"{doc.label()}: {finding.code}" for finding in findings]
        assert printed_total(doc) in pdf_text.text, f"{doc.label()}: total not in the text"
    assert false_rejections == []


def test_every_inbox_copy_is_rejected_before_the_model_is_called(
    rendered: list[RenderedDocument],
) -> None:
    for doc in malicious(rendered):
        client, fake = make_client([])
        result = extract_invoice(doc.path.read_bytes(), client, CONFIG, known_vendors=KNOWN_VENDORS)
        assert result.status == "rejected", f"{doc.variant} {doc.label()}: {result.reasons}"
        assert result.document is None
        assert len(fake.requests) == 0, f"{doc.variant} {doc.label()}"
