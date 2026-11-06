"""PDF renderers: three platform layouts, the agency invoice and the credit note.

Layouts differ on purpose (headers, column order, number formats, wording) so that later
extraction has to cope with variety. `invariant=1` makes reportlab drop timestamps and
random document IDs, so the same document always renders to the same bytes.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

from generator.documents.model import CreditNote, Invoice

FONTS_DIR = Path(__file__).resolve().parent.parent / "fonts"
FONT, FONT_BOLD = "DejaVuSans", "DejaVuSans-Bold"
PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN = 50
FOOTER = "Synthetic test document — Matchbook generator. Not a real invoice."
ROW_HEIGHT = 16


def register_fonts() -> None:
    if FONT not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(FONT, str(FONTS_DIR / "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont(FONT_BOLD, str(FONTS_DIR / "DejaVuSans-Bold.ttf")))


# --- number formats -----------------------------------------------------------------------


def grouped(amount: Decimal) -> str:
    return f"{abs(amount):,.2f}"


def sign(amount: Decimal) -> str:
    return "-" if amount < 0 else ""


def format_meta(amount: Decimal) -> str:
    return f"{sign(amount)}€{grouped(amount)}"


def format_google(amount: Decimal) -> str:
    return f"{sign(amount)}{grouped(amount)} EUR"


def format_tiktok(amount: Decimal) -> str:
    return f"EUR {sign(amount)}{abs(amount):.2f}"


def format_agency(amount: Decimal) -> str:
    return f"{sign(amount)}{grouped(amount)} €"


AMOUNT_FORMATS: dict[str, Callable[[Decimal], str]] = {
    "meta": format_meta,
    "google": format_google,
    "tiktok": format_tiktok,
    "agency": format_agency,
}


def format_amount(layout: str, amount: Decimal) -> str:
    """The amount exactly as the given layout prints it."""
    return AMOUNT_FORMATS[layout](amount)


# --- drawing helpers ----------------------------------------------------------------------


@dataclass(frozen=True)
class Column:
    title: str
    x: float
    align: str = "left"  # left / right


class Page:
    def __init__(self, path: Path, title: str) -> None:
        register_fonts()
        self.canvas = Canvas(str(path), pagesize=A4, invariant=1)
        self.canvas.setTitle(title)
        self.canvas.setAuthor("Matchbook generator")
        self.y = PAGE_HEIGHT - MARGIN

    def text(self, value: str, x: float = MARGIN, size: int = 9, bold: bool = False) -> None:
        self.canvas.setFont(FONT_BOLD if bold else FONT, size)
        self.canvas.drawString(x, self.y, value)

    def right(self, value: str, x: float, size: int = 9, bold: bool = False) -> None:
        self.canvas.setFont(FONT_BOLD if bold else FONT, size)
        self.canvas.drawRightString(x, self.y, value)

    def line_break(self, height: float = ROW_HEIGHT) -> None:
        self.y -= height

    def rule(self) -> None:
        self.canvas.line(MARGIN, self.y + 4, PAGE_WIDTH - MARGIN, self.y + 4)
        self.line_break(8)

    def pairs(
        self, rows: list[tuple[str, str]], x: float = MARGIN, label_width: float = 120
    ) -> None:
        for label, value in rows:
            self.text(label, x, bold=True)
            self.text(value, x + label_width)
            self.line_break()

    def table(self, columns: list[Column], rows: list[list[str]]) -> None:
        for column in columns:
            self.cell(column, column.title, bold=True)
        self.line_break()
        self.rule()
        for row in rows:
            for column, value in zip(columns, row, strict=True):
                self.cell(column, value)
            self.line_break()
        self.rule()

    def cell(self, column: Column, value: str, bold: bool = False) -> None:
        if column.align == "right":
            self.right(value, column.x, bold=bold)
        else:
            self.text(value, column.x, bold=bold)

    def save(self) -> None:
        self.canvas.setFont(FONT, 7)
        self.canvas.drawString(MARGIN, MARGIN / 2, FOOTER)
        self.canvas.showPage()
        self.canvas.save()


RIGHT_EDGE = PAGE_WIDTH - MARGIN


def long_date(value: date) -> str:
    return f"{value:%B} {value.day}, {value.year}"


# --- platform layouts ---------------------------------------------------------------------


def render_meta(invoice: Invoice, path: Path) -> None:
    fmt = format_meta
    page = Page(path, f"Receipt {invoice.number}")
    page.text(f"{invoice.vendor} Ads", size=16, bold=True)
    page.right("Receipt / Invoice", RIGHT_EDGE, size=14, bold=True)
    page.line_break(30)
    page.pairs(
        [
            ("Invoice #", invoice.number),
            ("Invoice date", long_date(invoice.issue_date)),
            ("Payment due", long_date(invoice.due_date)),
            ("Account ID", invoice.account_id or ""),
            ("Billed to", invoice.bill_to),
            (
                "Billing period",
                f"{long_date(invoice.period_start)} – {long_date(invoice.period_end)}",
            ),
        ]
    )
    page.line_break()
    page.table(
        [
            Column("Campaign", MARGIN),
            Column("Reference", 330),
            Column("Amount", RIGHT_EDGE, "right"),
        ],
        [[line.description, line.campaign_ref or "", fmt(line.amount)] for line in invoice.lines],
    )
    for label, value, bold in (
        ("Subtotal", invoice.subtotal, False),
        (f"VAT ({vat_pct(invoice.vat_rate)}%)", invoice.vat, False),
        ("Total paid", invoice.total, True),
    ):
        page.right(label, 420, bold=bold)
        page.right(fmt(value), RIGHT_EDGE, bold=bold)
        page.line_break()
    page.line_break()
    page.text(f"All amounts in {invoice.currency}.", size=8)
    page.save()


def render_google(invoice: Invoice, path: Path) -> None:
    fmt = format_google
    page = Page(path, f"Invoice {invoice.number}")
    page.text("Invoice", size=18, bold=True)
    page.right(invoice.vendor, RIGHT_EDGE, size=12, bold=True)
    page.line_break(28)
    # Summary box first, details after: Google-style.
    page.canvas.rect(MARGIN, page.y - 52, PAGE_WIDTH - 2 * MARGIN, 66)
    page.text("Total amount due", MARGIN + 10, size=10, bold=True)
    page.right(fmt(invoice.total), RIGHT_EDGE - 10, size=14, bold=True)
    page.line_break(18)
    page.text(f"Due {invoice.due_date:%d %b %Y}", MARGIN + 10)
    page.line_break()
    page.text(
        f"Summary for {invoice.period_start:%d %b %Y} - {invoice.period_end:%d %b %Y}", MARGIN + 10
    )
    page.line_break(36)
    page.pairs(
        [
            ("Invoice number", invoice.number),
            ("Issue date", f"{invoice.issue_date:%d %b %Y}"),
            ("Customer ID", account_dashes(invoice.account_id or "")),
            ("Bill to", invoice.bill_to),
        ]
    )
    page.line_break()
    page.table(
        [
            Column("Description", MARGIN),
            Column("Quantity", 380, "right"),
            Column("Amount (EUR)", RIGHT_EDGE, "right"),
        ],
        [[line.description, "1", fmt(line.amount)] for line in invoice.lines],
    )
    for label, value in (
        ("Subtotal in EUR", invoice.subtotal),
        (f"VAT ({vat_pct(invoice.vat_rate)}%)", invoice.vat),
        ("Total in EUR", invoice.total),
    ):
        page.right(label, 420, bold=label.startswith("Total"))
        page.right(fmt(value), RIGHT_EDGE, bold=label.startswith("Total"))
        page.line_break()
    page.save()


def render_tiktok(invoice: Invoice, path: Path) -> None:
    fmt = format_tiktok
    page = Page(path, f"Tax invoice {invoice.number}")
    page.text("TAX INVOICE", size=16, bold=True)
    page.line_break(14)
    page.text(f"{invoice.vendor} For Business", size=10)
    page.line_break(28)
    page.pairs(
        [
            ("Invoice No.:", invoice.number),
            ("Invoice Date:", f"{invoice.issue_date:%Y-%m-%d}"),
            ("Due Date:", f"{invoice.due_date:%Y-%m-%d}"),
            ("Advertiser ID:", invoice.account_id or ""),
            ("Customer:", invoice.bill_to),
            ("Service Period:", f"{invoice.period_start:%Y-%m-%d} ~ {invoice.period_end:%Y-%m-%d}"),
            ("Currency:", invoice.currency),
        ],
        label_width=100,
    )
    page.line_break()
    page.table(
        [Column("Amount", 120, "right"), Column("Item", 140), Column("Ref", 400)],
        [[fmt(line.amount), line.description, line.campaign_ref or ""] for line in invoice.lines],
    )
    for label, value in (
        ("Net Amount:", invoice.subtotal),
        (f"Tax {vat_pct(invoice.vat_rate)}%:", invoice.vat),
        ("Amount Due:", invoice.total),
    ):
        page.text(label, MARGIN, bold=label == "Amount Due:")
        page.right(fmt(value), 220, bold=label == "Amount Due:")
        page.line_break()
    page.save()


# --- agency documents ---------------------------------------------------------------------


def render_agency(invoice: Invoice, path: Path, agency_vat_code: str) -> None:
    fmt = format_agency
    page = Page(path, f"Invoice {invoice.number}")
    page.text(invoice.vendor, size=14, bold=True)
    page.right("VAT INVOICE", RIGHT_EDGE, size=14, bold=True)
    page.line_break(14)
    page.text(f"VAT code {agency_vat_code}", size=8)
    page.line_break(28)
    page.pairs(
        [
            ("Invoice no.", invoice.number),
            ("Date", invoice.issue_date.isoformat()),
            ("Due date", invoice.due_date.isoformat()),
            ("Client", invoice.bill_to),
            ("Period", f"{invoice.period_start.isoformat()} – {invoice.period_end.isoformat()}"),
        ]
    )
    page.line_break()
    page.table(
        [Column("No.", MARGIN), Column("Description", 80), Column("Amount", RIGHT_EDGE, "right")],
        [[str(line.line_no), line.description, fmt(line.amount)] for line in invoice.lines],
    )
    for label, value in (
        ("Subtotal", invoice.subtotal),
        (f"VAT {vat_pct(invoice.vat_rate)}%", invoice.vat),
        ("Total due", invoice.total),
    ):
        page.right(label, 420, bold=label == "Total due")
        page.right(fmt(value), RIGHT_EDGE, bold=label == "Total due")
        page.line_break()
    page.save()


def render_credit_note(note: CreditNote, path: Path) -> None:
    fmt = AMOUNT_FORMATS[note.platform_key]
    page = Page(path, f"Credit note {note.number}")
    page.text(f"{note.vendor}", size=14, bold=True)
    page.right("CREDIT NOTE", RIGHT_EDGE, size=14, bold=True)
    page.line_break(30)
    page.pairs(
        [
            ("Credit note no.", note.number),
            ("Issue date", note.issue_date.isoformat()),
            ("Original invoice", note.original_number),
            ("Customer", note.bill_to),
            ("Period", f"{note.period_start.isoformat()} – {note.period_end.isoformat()}"),
            ("Campaign", note.campaign_ref),
            ("Reason", note.reason),
        ]
    )
    page.line_break()
    for label, value in (
        ("Credit amount", note.subtotal),
        (f"VAT {vat_pct(note.vat_rate)}%", note.vat),
        ("Total credit", note.total),
    ):
        page.right(label, 420, bold=label == "Total credit")
        page.right(fmt(value), RIGHT_EDGE, bold=label == "Total credit")
        page.line_break()
    page.save()


def vat_pct(rate: Decimal) -> str:
    return f"{(rate * 100).normalize():f}"


def account_dashes(account_id: str) -> str:
    if len(account_id) != 10:
        return account_id
    return f"{account_id[:3]}-{account_id[3:6]}-{account_id[6:]}"


PLATFORM_RENDERERS: dict[str, Callable[[Invoice, Path], None]] = {
    "meta": render_meta,
    "google": render_google,
    "tiktok": render_tiktok,
}


def render_invoice(invoice: Invoice, path: Path, agency_vat_code: str) -> None:
    if invoice.layout == "agency":
        render_agency(invoice, path, agency_vat_code)
    else:
        PLATFORM_RENDERERS[invoice.layout](invoice, path)
