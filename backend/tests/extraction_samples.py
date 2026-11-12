"""Hand-written invoice texts and matching fake model answers for the extraction tests.

The texts mimic the four generated layouts and the credit note (decision 024) as pypdf reads
them. Contact details are synthetic (RFC 2606 domains, Ofcom drama phone range, a textbook
IBAN). Nothing here is read from data/generated.
"""

import json
from datetime import date
from decimal import Decimal
from typing import Any

from app.config import ModelPrice
from app.extraction.extract import ExtractionConfig
from app.extraction.vendors import KnownVendors
from app.llm.client import LLMClient, LLMLimits
from app.llm.cost import CostLedger
from app.llm.fake import FakeProvider, Scripted

from generator.documents.contacts import VendorContact
from generator.documents.model import Invoice, InvoiceLine

MODEL = "test-model"
PRICE = ModelPrice(input_eur_per_mtok=Decimal("1"), output_eur_per_mtok=Decimal("2"))
CONFIG = ExtractionConfig()
KNOWN_VENDORS = KnownVendors.of(("Meta", "Google", "TikTok", "UAB Matchbook Media"))

IBAN = "LT12 1000 0111 0100 1000"
EMAIL = "billing@vendor.example"
PHONE = "+44 20 7946 0123"
CONTACT = f"{EMAIL} · {PHONE}"
FOOTER = f"Pay to IBAN {IBAN}\nSynthetic test document — Matchbook generator. Not a real invoice."

META_TEXT = f"""Meta Ads Receipt / Invoice
{CONTACT}
Invoice # FBADS-456789-987654321
Invoice date June 1, 2026
Payment due July 1, 2026
Account ID 100000123456789
Billed to UAB Matchbook Media
Billing period May 1, 2026 – May 31, 2026
Campaign Reference Amount
Facebook/Instagram ads – SAU-META-2605 SAU-META-2605 €1,000.00
Subtotal €1,000.00
VAT (21%) €210.00
Total paid €1,210.00
All amounts in EUR.
{FOOTER}"""

GOOGLE_TEXT = f"""Invoice Google
{CONTACT}
Total amount due 1,493.82 EUR
Due 02 Jul 2026
Summary for 01 May 2026 - 31 May 2026
Invoice number 1234567890
Issue date 02 Jun 2026
Customer ID 123-456-7890
Bill to UAB Matchbook Media
Description Quantity Amount (EUR)
Google Ads campaign GIN-GOOGLE-2605 1 1,234.56 EUR
Subtotal in EUR 1,234.56 EUR
VAT (21%) 259.26 EUR
Total in EUR 1,493.82 EUR
{FOOTER}"""

TIKTOK_TEXT = f"""TAX INVOICE
TikTok For Business
{CONTACT}
Invoice No.: BDUE20260512345678
Invoice Date: 2026-06-03
Due Date: 2026-07-03
Advertiser ID: 7000000000000000001
Customer: UAB Matchbook Media
Service Period: 2026-05-01 ~ 2026-05-31
Currency: EUR
Amount Item Ref
EUR 500.00 TikTok For Business advertising service (BAL-TIKTOK-2605) BAL-TIKTOK-2605
Net Amount: EUR 500.00
Tax 21%: EUR 105.00
Amount Due: EUR 605.00
{FOOTER}"""

AGENCY_TEXT = f"""UAB Matchbook Media VAT INVOICE
VAT code LT100000000000
{CONTACT}
Invoice no. MBM-SAU-2605
Date 2026-06-05
Due date 2026-07-05
Client Saulė Foods
Period 2026-05-01 – 2026-05-31
No. Description Amount
1 Media – Meta – SAU-META-2605 1,000.00 €
2 Credit note FBCN-2606-123456 (Meta) -100.00 €
3 Agency commission 10% on media 90.00 €
Subtotal 990.00 €
VAT 21% 207.90 €
Total due 1,197.90 €
{FOOTER}"""

CREDIT_NOTE_TEXT = """Meta CREDIT NOTE
Credit note no. FBCN-2606-123456
Issue date 2026-06-04
Original invoice FBADS-456789-987654321
Customer UAB Matchbook Media
Period 2026-05-01 – 2026-05-31
Campaign SAU-META-2605
Reason Invalid traffic adjustment
Credit amount €100.00
VAT 21% €21.00
Total credit €121.00
Synthetic test document — Matchbook generator. Not a real invoice."""


def invoice_answer(**fields: Any) -> dict[str, Any]:
    answer: dict[str, Any] = {
        "document_type": "invoice",
        "account_id": None,
        "original_number": None,
        "campaign_ref": None,
        "reason": None,
        "currency": "EUR",
        "vat_rate": "0.21",
        "bill_to": "UAB Matchbook Media",
        "period_start": "2026-05-01",
        "period_end": "2026-05-31",
        "model_confidence": "0.95",
        "contains_instructions": False,
    }
    answer.update(fields)
    return answer


def line(ref: str | None, description: str, amount: str) -> dict[str, Any]:
    return {"campaign_ref": ref, "description": description, "amount": amount}


META_ANSWER = invoice_answer(
    vendor="Meta",
    number="FBADS-456789-987654321",
    account_id="100000123456789",
    issue_date="2026-06-01",
    due_date="2026-07-01",
    lines=[line("SAU-META-2605", "Facebook/Instagram ads – SAU-META-2605", "1000.00")],
    subtotal="1000.00",
    vat="210.00",
    total="1210.00",
)
GOOGLE_ANSWER = invoice_answer(
    vendor="Google",
    number="1234567890",
    account_id="123-456-7890",
    issue_date="2026-06-02",
    due_date="2026-07-02",
    lines=[line("GIN-GOOGLE-2605", "Google Ads campaign GIN-GOOGLE-2605", "1234.56")],
    subtotal="1234.56",
    vat="259.26",
    total="1493.82",
)
TIKTOK_ANSWER = invoice_answer(
    vendor="TikTok",
    number="BDUE20260512345678",
    account_id="7000000000000000001",
    issue_date="2026-06-03",
    due_date="2026-07-03",
    lines=[
        line(
            "BAL-TIKTOK-2605",
            "TikTok For Business advertising service (BAL-TIKTOK-2605)",
            "500.00",
        )
    ],
    subtotal="500.00",
    vat="105.00",
    total="605.00",
)
AGENCY_ANSWER = invoice_answer(
    vendor="UAB Matchbook Media",
    number="MBM-SAU-2605",
    bill_to="Saulė Foods",
    issue_date="2026-06-05",
    due_date="2026-07-05",
    lines=[
        line("SAU-META-2605", "Media – Meta – SAU-META-2605", "1000.00"),
        line(None, "Credit note FBCN-2606-123456 (Meta)", "-100.00"),
        line(None, "Agency commission 10% on media", "90.00"),
    ],
    subtotal="990.00",
    vat="207.90",
    total="1197.90",
)
CREDIT_NOTE_ANSWER = invoice_answer(
    document_type="credit_note",
    vendor="Meta",
    number="FBCN-2606-123456",
    issue_date="2026-06-04",
    due_date=None,
    original_number="FBADS-456789-987654321",
    campaign_ref="SAU-META-2605",
    reason="Invalid traffic adjustment",
    lines=[],
    subtotal="100.00",
    vat="21.00",
    total="121.00",
)

SAMPLES: dict[str, tuple[str, dict[str, Any]]] = {
    "meta": (META_TEXT, META_ANSWER),
    "google": (GOOGLE_TEXT, GOOGLE_ANSWER),
    "tiktok": (TIKTOK_TEXT, TIKTOK_ANSWER),
    "agency": (AGENCY_TEXT, AGENCY_ANSWER),
    "credit_note": (CREDIT_NOTE_TEXT, CREDIT_NOTE_ANSWER),
}


def as_json(answer: dict[str, Any], **changes: Any) -> str:
    return json.dumps({**answer, **changes}, ensure_ascii=False)


def hand_built_invoice(layout: str) -> Invoice:
    """One invoice to render with the generator's own layout code, no world needed."""
    platform = None if layout == "agency" else layout
    ref = f"SAU-{(platform or 'meta').upper()}-2605"
    return Invoice(
        kind="client" if layout == "agency" else "platform",
        layout=layout,
        vendor="UAB Matchbook Media" if layout == "agency" else layout.capitalize(),
        number={
            "meta": "FBADS-456789-987654321",
            "google": "1234567890",
            "tiktok": "BDUE20260512345678",
            "agency": "MBM-SAU-2605",
        }[layout],
        client_code="SAU",
        platform_key=platform,
        bill_to="UAB Matchbook Media",
        account_id=None if layout == "agency" else "100000123456789",
        issue_date=date(2026, 6, 1),
        due_date=date(2026, 7, 1),
        period_start=date(2026, 5, 1),
        period_end=date(2026, 5, 31),
        lines=(InvoiceLine(1, ref, f"Campaign {ref}", Decimal("1234.56")),),
        subtotal=Decimal("1234.56"),
        vat_rate=Decimal("0.21"),
        vat=Decimal("259.26"),
        total=Decimal("1493.82"),
        contact=VendorContact(iban=IBAN.replace(" ", ""), email=EMAIL, phone=PHONE),
    )


def make_client(script: list[Scripted]) -> tuple[LLMClient, FakeProvider]:
    """A client with the real extraction prompt and a scripted model."""
    fake = FakeProvider(script)
    client = LLMClient(
        provider=fake,
        limits=LLMLimits(model=MODEL, prices={MODEL: PRICE}),
        ledger=CostLedger(run_cap=Decimal("1"), monthly_cap=Decimal("10")),
        sleep=lambda _: None,
    )
    return client, fake
