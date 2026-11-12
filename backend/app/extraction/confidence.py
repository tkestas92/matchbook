"""Per-field confidence computed in code (threat model T3, decision 042).

Every field gets a set of pass/fail checks:
- grounded: the value is printed in the visible text (money as a parsed amount, dates in any
  printed format, strings as normalised substrings, IDs without dashes);
- arithmetic: lines sum to the subtotal, VAT = subtotal × rate (half up to cents), total =
  subtotal + VAT, rate is 21%;
- format: dates parse and are in order, currency is EUR, the number matches a known vendor
  format, campaign refs match `CLIENT-PLATFORM-YYMM` and, when given, a known ref;
- unknown: the printed vendor resolves to a known canonical vendor (decision 044); the document
  carries the canonical name, and an unresolved vendor sends the document to review.

A field's confidence is 0 if any of its checks failed, otherwise the model's own score. The
model's score is one input: it can only lower a field, never raise it. Fields the code derives
(type, kind, platform, client_code, line_no) never come from the model.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.extraction.schema import (
    ExtractedDocument,
    ExtractedLine,
    InvoiceExtraction,
    LineExtraction,
)
from app.extraction.vendors import KnownVendors

CURRENCY = "EUR"
VAT_RATES = frozenset({Decimal("0.21")})
CENT = Decimal("0.01")
MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")

INVOICE = "invoice"
CREDIT_NOTE = "credit_note"
PLATFORM = "platform"
CLIENT = "client"


@dataclass(frozen=True)
class NumberFormat:
    pattern: re.Pattern[str]
    type: str
    kind: str | None
    platform: str | None


# Vendor number formats (decision 024's generator formats). Credit notes first: their numbers
# would otherwise look like an agency number.
NUMBER_FORMATS = (
    NumberFormat(re.compile(r"FBCN-\d{4}-\d{6}"), CREDIT_NOTE, None, "meta"),
    NumberFormat(re.compile(r"GCN-\d{4}-\d{6}"), CREDIT_NOTE, None, "google"),
    NumberFormat(re.compile(r"BDCN-\d{4}-\d{6}"), CREDIT_NOTE, None, "tiktok"),
    NumberFormat(re.compile(r"FBADS-\d{6}-\d{9}"), INVOICE, PLATFORM, "meta"),
    NumberFormat(re.compile(r"\d{10}"), INVOICE, PLATFORM, "google"),
    NumberFormat(re.compile(r"BDUE\d{14}"), INVOICE, PLATFORM, "tiktok"),
    NumberFormat(
        re.compile(r"[A-Z]{2,10}-(?P<client>[A-Z0-9]{2,12})-\d{4}"), INVOICE, CLIENT, None
    ),
)
CAMPAIGN_REF = re.compile(r"(?P<client>[A-Z0-9]{2,12})-(?P<platform>[A-Z]{2,12})-(?P<yymm>\d{4})")
AMOUNT = re.compile(
    r"(?<![\w.,])(?P<sign>-)?(?:€|EUR ?)?(?P<sign2>-)?"
    r"(?P<number>\d{1,3}(?:,\d{3})+\.\d{2}|\d+\.\d{2})(?![\d.,]\d)"
)
DIGIT_RUN = re.compile(r"\d[\d-]*\d")


@dataclass(frozen=True)
class Evaluation:
    document: ExtractedDocument
    field_confidence: dict[str, Decimal]
    reasons: tuple[str, ...]  # "<check>:<field>", fixed codes only

    @property
    def confidence(self) -> Decimal:
        return min(self.field_confidence.values(), default=Decimal(0))


@dataclass
class Scorecard:
    failures: dict[str, list[str]] = field(default_factory=dict)

    def check(self, name: str, passed: bool, reason: str) -> None:
        self.failures.setdefault(name, [])
        if not passed:
            self.failures[name].append(reason)

    def confidence(self, model_score: Decimal) -> dict[str, Decimal]:
        return {
            name: Decimal(0) if failed else model_score for name, failed in self.failures.items()
        }

    def reasons(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {f"{reason}:{name}" for name, failed in self.failures.items() for reason in failed}
            )
        )


# --- reading the visible text ------------------------------------------------------------


def clean(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(DASHES)
    return " ".join(text.split())


def normalize(text: str) -> str:
    return clean(text).casefold()


def amounts_in(text: str) -> set[Decimal]:
    """Every amount printed in one of the layouts' formats, with its sign."""
    amounts = set()
    for match in AMOUNT.finditer(clean(text)):
        value = Decimal(match["number"].replace(",", ""))
        amounts.add(-value if match["sign"] or match["sign2"] else value)
    return amounts


def date_forms(value: date) -> tuple[str, ...]:
    month = MONTHS[value.month - 1]
    return (
        value.isoformat(),
        f"{value.day:02d} {month[:3]} {value.year}",
        f"{month} {value.day}, {value.year}",
    )


def digit_runs(text: str) -> set[str]:
    return {run.replace("-", "") for run in DIGIT_RUN.findall(text)}


@dataclass(frozen=True)
class VisibleText:
    normalized: str
    amounts: frozenset[Decimal]
    digits: frozenset[str]

    @classmethod
    def of(cls, text: str) -> "VisibleText":
        return cls(normalize(text), frozenset(amounts_in(text)), frozenset(digit_runs(text)))

    def has_string(self, value: str) -> bool:
        return normalize(value) in self.normalized

    def has_amount(self, value: Decimal) -> bool:
        return value in self.amounts

    def has_date(self, value: date) -> bool:
        return any(normalize(form) in self.normalized for form in date_forms(value))

    def has_digits(self, value: str) -> bool:
        return re.sub(r"[ -]", "", value) in self.digits


# --- parsing and derived fields ------------------------------------------------------------


def parse_date(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def number_format(number: str) -> NumberFormat | None:
    return next((f for f in NUMBER_FORMATS if f.pattern.fullmatch(number)), None)


def ref_parts(ref: str | None) -> re.Match[str] | None:
    return CAMPAIGN_REF.fullmatch(ref) if ref is not None else None


def derive_client_code(fmt: NumberFormat | None, number: str, refs: list[str | None]) -> str | None:
    """The agency number's client part, else the one client prefix all refs agree on."""
    codes = {parts["client"] for ref in refs if (parts := ref_parts(ref)) is not None}
    if fmt is not None and fmt.kind == CLIENT and (match := fmt.pattern.fullmatch(number)):
        codes.add(match["client"])
    return codes.pop() if len(codes) == 1 else None


def vat_of(subtotal: Decimal, rate: Decimal) -> Decimal:
    return (subtotal * rate).quantize(CENT, rounding=ROUND_HALF_UP)


# --- checks --------------------------------------------------------------------------------


def check_ref(
    card: Scorecard,
    name: str,
    ref: str | None,
    text: VisibleText,
    platform: str | None,
    period_start: date | None,
    known_refs: frozenset[str] | None,
) -> None:
    if ref is None:
        return
    parts = ref_parts(ref)
    card.check(name, parts is not None, "format")
    card.check(name, text.has_string(ref), "grounding")
    if parts is not None and platform is not None:
        card.check(name, parts["platform"] == platform.upper(), "format")
    if parts is not None and period_start is not None:
        card.check(name, parts["yymm"] == f"{period_start:%y%m}", "format")
    if known_refs is not None:
        card.check(name, ref in known_refs, "unknown_ref")


def check_line(
    card: Scorecard,
    index: int,
    line: LineExtraction,
    text: VisibleText,
    platform: str | None,
    period_start: date | None,
    known_refs: frozenset[str] | None,
) -> ExtractedLine:
    prefix = f"lines[{index}]"
    amount = Decimal(line.amount)
    card.check(f"{prefix}.amount", text.has_amount(amount), "grounding")
    card.check(f"{prefix}.description", text.has_string(line.description), "grounding")
    check_ref(
        card, f"{prefix}.campaign_ref", line.campaign_ref, text, platform, period_start, known_refs
    )
    return ExtractedLine(
        line_no=index + 1,
        campaign_ref=line.campaign_ref,
        description=line.description,
        amount=amount,
    )


def check_dates(
    card: Scorecard, raw: InvoiceExtraction, text: VisibleText
) -> dict[str, date | None]:
    values = {
        "issue_date": raw.issue_date,
        "due_date": raw.due_date,
        "period_start": raw.period_start,
        "period_end": raw.period_end,
    }
    parsed: dict[str, date | None] = {}
    for name, value in values.items():
        parsed[name] = day = parse_date(value)
        if value is None:
            continue
        card.check(name, day is not None, "format")
        if day is not None:
            card.check(name, text.has_date(day), "grounding")
    for first, second in (("issue_date", "due_date"), ("period_start", "period_end")):
        a, b = parsed[first], parsed[second]
        if a is not None and b is not None and a > b:
            card.check(first, False, "format")
            card.check(second, False, "format")
    return parsed


def check_totals(
    card: Scorecard,
    raw: InvoiceExtraction,
    text: VisibleText,
    lines: tuple[ExtractedLine, ...],
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    subtotal, rate, vat, total = (
        Decimal(raw.subtotal),
        Decimal(raw.vat_rate),
        Decimal(raw.vat),
        Decimal(raw.total),
    )
    for name, value in (("subtotal", subtotal), ("vat", vat), ("total", total)):
        card.check(name, text.has_amount(value), "grounding")
    card.check("vat_rate", rate in VAT_RATES, "format")
    card.check("vat_rate", text.has_string(f"{(rate * 100).normalize():f}%"), "grounding")
    if lines:
        sums_up = sum((line.amount for line in lines), Decimal(0)) == subtotal
        card.check("subtotal", sums_up, "arithmetic")
        for index in range(len(lines)):
            card.check(f"lines[{index}].amount", sums_up, "arithmetic")
    card.check("vat", vat == vat_of(subtotal, rate), "arithmetic")
    card.check("total", total == subtotal + vat, "arithmetic")
    return subtotal, rate, vat, total


def check_required(
    card: Scorecard, raw: InvoiceExtraction, doc_type: str, kind: str | None
) -> None:
    if doc_type == CREDIT_NOTE:
        required = {
            "original_number": raw.original_number,
            "campaign_ref": raw.campaign_ref,
            "reason": raw.reason,
        }
        card.check("lines", not raw.lines, "format")
    else:
        required = {"due_date": raw.due_date, "lines": raw.lines or None}
        if kind == PLATFORM:
            required["account_id"] = raw.account_id
    for name, value in required.items():
        card.check(name, value is not None, "missing")


def evaluate(
    raw: InvoiceExtraction,
    visible_text: str,
    known_refs: frozenset[str] | None = None,
    *,
    known_vendors: KnownVendors,
) -> Evaluation:
    text = VisibleText.of(visible_text)
    card = Scorecard()

    fmt = number_format(raw.number)
    card.check("number", fmt is not None, "format")
    card.check("number", text.has_string(raw.number), "grounding")
    doc_type = fmt.type if fmt is not None else raw.document_type
    card.check("type", doc_type == raw.document_type, "format")
    kind = fmt.kind if fmt is not None else None
    platform = fmt.platform if fmt is not None else None
    if doc_type == INVOICE:
        card.check("kind", kind is not None, "format")
    if fmt is None or fmt.kind != CLIENT:
        card.check("platform", platform is not None, "format")
    check_required(card, raw, doc_type, kind)

    card.check("vendor", text.has_string(raw.vendor), "grounding")
    vendor = known_vendors.resolve(raw.vendor)
    card.check("vendor", vendor is not None, "unknown")
    card.check("bill_to", text.has_string(raw.bill_to), "grounding")
    if raw.account_id is not None:
        card.check("account_id", text.has_digits(raw.account_id), "grounding")
    card.check("currency", raw.currency == CURRENCY, "format")
    dates = check_dates(card, raw, text)

    period_start = dates["period_start"]
    lines = tuple(
        check_line(card, index, line, text, platform, period_start, known_refs)
        for index, line in enumerate(raw.lines)
    )
    check_ref(card, "campaign_ref", raw.campaign_ref, text, platform, period_start, known_refs)
    if raw.original_number is not None:
        original = number_format(raw.original_number)
        card.check("original_number", original is not None and original.type == INVOICE, "format")
        card.check("original_number", text.has_string(raw.original_number), "grounding")
    if raw.reason is not None:
        card.check("reason", text.has_string(raw.reason), "grounding")
    subtotal, rate, vat, total = check_totals(card, raw, text, lines)

    refs = [line.campaign_ref for line in raw.lines] + [raw.campaign_ref]
    client_code = derive_client_code(fmt, raw.number, refs)
    card.check("client_code", client_code is not None, "derivation")

    document = ExtractedDocument(
        type=doc_type,
        kind=kind,
        vendor=vendor if vendor is not None else raw.vendor,
        number=raw.number,
        client_code=client_code,
        platform=platform,
        bill_to=raw.bill_to,
        account_id=re.sub(r"[ -]", "", raw.account_id) if raw.account_id is not None else None,
        issue_date=dates["issue_date"],
        due_date=dates["due_date"],
        period_start=period_start,
        period_end=dates["period_end"],
        currency=raw.currency,
        lines=lines,
        subtotal=subtotal,
        vat_rate=rate,
        vat=vat,
        total=total,
        original_number=raw.original_number,
        campaign_ref=raw.campaign_ref,
        reason=raw.reason,
    )
    field_confidence = card.confidence(Decimal(raw.model_confidence))
    return Evaluation(document, field_confidence, card.reasons())
