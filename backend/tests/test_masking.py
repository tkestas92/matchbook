"""PII masking (task 2.3, threat model T2, decision 038).

The generated-document tests render every invoice and credit note in a tmp dir with the
generator's fixed seed; they never read data/generated and never write the answer key.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import pytest
from app.llm.masking import _mask_with_mapping, mask
from pypdf import PdfReader

from generator.documents.billing import build_documents
from generator.documents.config import DocumentsConfig, load_documents_config
from generator.documents.contacts import grouped_iban, make_iban
from generator.documents.injection import INJECTION_VARIANTS
from generator.documents.model import CreditNote, Invoice
from generator.documents.pdf import account_dashes, format_amount
from generator.documents.writer import credit_note_dir, inbox_dir, invoice_dir, write_outputs
from generator.errors import load_errors_config, plant_errors
from generator.world import World, build_world, load_config

LT_IBAN = make_iban("LT", "99999" + "12345678901")


def masked_values(text: str) -> list[str]:
    return sorted(_mask_with_mapping(text)[1].values())


# --- each type is masked ------------------------------------------------------------------


@pytest.mark.parametrize(
    "iban",
    [LT_IBAN, grouped_iban(LT_IBAN), "DE89 3704 0044 0532 0130 00", "GB82WEST12345698765432"],
)
def test_iban_is_masked(iban: str) -> None:
    assert mask(f"Pay to IBAN {iban} please") == "Pay to IBAN [IBAN_1] please"


def test_iban_with_a_bad_check_digit_is_kept() -> None:
    bad = LT_IBAN[:2] + f"{(int(LT_IBAN[2:4]) + 1) % 100:02d}" + LT_IBAN[4:]
    assert mask(f"IBAN {bad}") == f"IBAN {bad}"


def test_grouped_iban_followed_by_a_word_masks_only_the_iban() -> None:
    assert mask(f"{grouped_iban(LT_IBAN)} EUR") == "[IBAN_1] EUR"


@pytest.mark.parametrize(
    "email", ["billing@meta-platforms.example", "Jonas.Jonaitis+ads@agency.co.uk", "a_b@x.lt"]
)
def test_email_is_masked(email: str) -> None:
    assert mask(f"Contact: {email}.") == "Contact: [EMAIL_1]."


@pytest.mark.parametrize(
    "phone",
    [
        "+44 20 7946 0123",
        "+44 (0)20 7946 0123",
        "+370 612 34567",
        "+37061234567",
        "+370-612-34567",
        "00370 612 34567",
        "+370 (5) 212 3456",
        "8 612 34567",
        "861234567",
        "8-612-34567",
        "8 (612) 34567",
        "8 5 212 3456",
        "(8 5) 212 3456",
        "8 (5) 212 3456",
    ],
)
def test_phone_is_masked(phone: str) -> None:
    assert mask(f"Tel. {phone}, ask for Jonas") == "Tel. [PHONE_1], ask for Jonas"


@pytest.mark.parametrize(
    "card",
    [
        "4111111111111111",
        "4111 1111 1111 1111",
        "4012-8888-8888-1881",
        "5555555555554444",
        "2223003122003222",
        "378282246310005",
        "3782 822463 10005",
        "6011111111111117",
    ],
)
def test_card_number_is_masked(card: str) -> None:
    assert mask(f"Card {card} expires") == "Card [CARD_1] expires"


@pytest.mark.parametrize(
    "text",
    [
        "Card 4111111111111112",  # Luhn fails
        "Ref 1234567812345670",  # Luhn passes, no card network starts with 1
        "Account ID 4111111111111111",  # an ad-account ID, never a card
        "Advertiser ID: 4111111111111111111",
    ],
)
def test_non_card_digit_runs_are_kept(text: str) -> None:
    assert mask(text) == text


def test_card_followed_by_digits_masks_only_the_card() -> None:
    assert mask("4111 1111 1111 1111 2025") == "[CARD_1] 2025"


def test_personal_code_is_masked() -> None:
    assert mask("Asmens kodas 38703181745.") == "Asmens kodas [PERSONAL_CODE_1]."


def test_personal_code_with_a_bad_checksum_is_kept() -> None:
    assert mask("Asmens kodas 38703181746") == "Asmens kodas 38703181746"


def test_personal_code_second_round_checksum() -> None:
    # The first weighted sum mod 11 is 10, so the second weights decide the check digit (4).
    assert mask("kodas 33309240064") == "kodas [PERSONAL_CODE_1]"


# --- placeholders -------------------------------------------------------------------------


def test_same_value_gets_the_same_placeholder() -> None:
    text = "a@x.example, b@x.example, A@X.example, +370 612 34567, 861234567, +37061234567"
    assert mask(text) == "[EMAIL_1], [EMAIL_2], [EMAIL_1], [PHONE_1], [PHONE_2], [PHONE_1]"


def test_mapping_holds_the_originals_as_printed() -> None:
    _, mapping = _mask_with_mapping(f"IBAN {grouped_iban(LT_IBAN)}, mail a@x.example")
    assert mapping == {"[IBAN_1]": grouped_iban(LT_IBAN), "[EMAIL_1]": "a@x.example"}


ALL_TYPES = (
    f"IBAN {LT_IBAN}, a@x.example, +370 612 34567, 8 5 212 3456, "
    "card 4111 1111 1111 1111, code 38703181745"
)


def test_masking_twice_gives_the_same_result() -> None:
    once = mask(ALL_TYPES)
    assert once == (
        "IBAN [IBAN_1], [EMAIL_1], [PHONE_1], [PHONE_2], card [CARD_1], code [PERSONAL_CODE_1]"
    )
    assert mask(once) == once
    assert mask(ALL_TYPES) == once


def test_long_untrusted_input_is_handled() -> None:
    for text in ("1" * 50_000, "a" * 50_000, "1-" * 25_000, "a." * 25_000 + "@", "+" * 50_000):
        assert mask(text) == text


# --- what must never be touched -----------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "€1,234.56",
        "-1,234.56 EUR",
        "EUR 812345.67",
        "8,123.45 €",
        "812,345,678.90 EUR",
        "2025-01-31",
        "January 31, 2025",
        "2025-01-01 – 2025-01-31",
        "FBADS-123456-812345678",
        "8123456789",
        "BDUE20250112345678",
        "GCN-2501-123456",
        "FBCN-2501-812345",
        "INV-ACME-2501",
        "ACME-META-2501",
        "VAT code LT100000000000",
        "Customer ID 812-345-6789",
        "Account ID 378282246310005",
        "VAT 21%",
    ],
)
def test_business_values_are_kept(text: str) -> None:
    assert mask(text) == text


# --- every generated document -------------------------------------------------------------


@dataclass(frozen=True)
class GeneratedDocument:
    path: Path
    text: str
    document: Invoice | CreditNote


def pdf_text(path: Path) -> str:
    """Visible and hidden page text plus metadata: everything an extractor could pass on."""
    reader = PdfReader(path)
    pages = [page.extract_text() for page in reader.pages]
    metadata = [str(value) for value in (reader.metadata or {}).values()]
    return "\n".join(pages + metadata)


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> list[GeneratedDocument]:
    world: World = build_world(load_config())
    docs_config: DocumentsConfig = load_documents_config()
    planted = plant_errors(world, build_documents(world, docs_config), load_errors_config())
    inbox = [(m.invoice, INJECTION_VARIANTS[m.variant]) for m in planted.malicious]
    out = tmp_path_factory.mktemp("masking")
    write_outputs(world, planted.documents, docs_config, out, inbox=inbox)

    sources: dict[Path, Invoice | CreditNote] = {}
    for invoice in (*planted.documents.platform_invoices, *planted.documents.client_invoices):
        sources[out / invoice_dir(invoice) / f"{invoice.number}.pdf"] = invoice
    for invoice, _ in inbox:
        sources[out / inbox_dir(invoice) / f"{invoice.number}.pdf"] = invoice
    for note in planted.documents.credit_notes:
        sources[out / credit_note_dir(note) / f"{note.number}.pdf"] = note

    assert set(out.rglob("*.pdf")) == set(sources), "every rendered PDF is paired"
    return [GeneratedDocument(path, pdf_text(path), doc) for path, doc in sorted(sources.items())]


def contact_strings(document: Invoice | CreditNote) -> list[str]:
    if isinstance(document, CreditNote):
        return []
    contact = document.contact
    return [contact.email, contact.phone, grouped_iban(contact.iban)]


def business_strings(document: Invoice | CreditNote) -> list[str]:
    if isinstance(document, CreditNote):
        layout, refs, extra = document.platform_key, [document.campaign_ref], []
        extra.append(document.original_number)
    else:
        layout = document.layout
        refs = [line.campaign_ref for line in document.lines if line.campaign_ref]
        account = document.account_id
        extra = [account, account_dashes(account)] if account else []
    return [document.number, format_amount(layout, document.total), *refs, *extra]


def test_all_kinds_of_generated_documents_are_covered(generated: list[GeneratedDocument]) -> None:
    folders = {part for doc in generated for part in doc.path.parts}
    assert {"invoices", "inbox", "credit_notes"} <= folders
    assert any(isinstance(doc.document, CreditNote) for doc in generated)


def test_generated_documents_mask_exactly_their_contact_details(
    generated: list[GeneratedDocument],
) -> None:
    for doc in generated:
        assert masked_values(doc.text) == sorted(contact_strings(doc.document)), doc.path.name


def test_generated_business_values_are_untouched(generated: list[GeneratedDocument]) -> None:
    for doc in generated:
        masked = mask(doc.text)
        assert doc.document.number in doc.text, doc.path.name
        for value in business_strings(doc.document):
            if value in doc.text:
                assert value in masked, f"{doc.path.name}: {value}"


PII_TRACES = re.compile(r"@|\+44|\b7946\b|LT\d{2}(?: ?\d{4}){4}")


def test_masked_generated_documents_hold_no_pii(generated: list[GeneratedDocument]) -> None:
    for doc in generated:
        masked = mask(doc.text)
        assert not PII_TRACES.search(masked), doc.path.name
        if isinstance(doc.document, Invoice):
            contact = doc.document.contact
            for value in (contact.iban, grouped_iban(contact.iban), contact.email, contact.phone):
                assert value not in masked, doc.path.name


def test_masking_generated_documents_twice_gives_the_same_result(
    generated: list[GeneratedDocument],
) -> None:
    for doc in generated:
        once = mask(doc.text)
        assert mask(once) == once, doc.path.name
