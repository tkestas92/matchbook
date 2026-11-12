"""PDF bytes -> page text plus hidden-text findings (threat model T1 and T5, decision 042).

Takes bytes only, never a path: where a file came from says nothing about what is inside it.
Size and page count are checked before any page content is parsed.

Hidden text is found without decoding it. pypdf's content-stream visitor reports every
operator with the current transformation and text matrices; `StateTracker` follows the
graphics state (fill and stroke colour, render mode, font size, fill alpha) through `q`/`Q`
and flags each text-showing operator with a non-empty string that would not be visible to a
reader: invisible render mode, white text, an effective size below 2 pt, a fully transparent
fill, or an origin outside the media box. Metadata values go through the content guards.

Any finding rejects the document, so when there is none every extracted character was
visible and `PdfText.text` is the whole page text.
"""

import io
import math
from dataclasses import dataclass, field
from typing import Any

from app.extraction.guards import HIDDEN_TEXT, Finding, scan
from pypdf import PdfReader
from pypdf.generic import ArrayObject, NameObject

MIN_FONT_SIZE = 2.0  # points, after all scaling
WHITE = 0.98  # every RGB/grey component at or above this (CMYK at or below 1 - WHITE) is white
MIN_ALPHA = 0.1
PAGE_TOLERANCE = 1.0  # points
INVISIBLE_MODES = {3, 7}
STROKE_MODES = {1, 5}
FILL_AND_STROKE_MODES = {2, 6}
TEXT_SHOWING = {b"Tj", b"TJ", b"'", b'"'}
IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

Matrix = tuple[float, float, float, float, float, float]


class PdfUnreadable(Exception):
    """The bytes are over a limit, encrypted or not a PDF pypdf can parse."""


@dataclass(frozen=True)
class PdfText:
    text: str
    findings: tuple[Finding, ...]
    page_count: int


@dataclass
class GraphicsState:
    fill: tuple[float, ...] = (0.0,)
    stroke: tuple[float, ...] = (0.0,)
    render_mode: int = 0
    font_size: float | None = None
    fill_alpha: float = 1.0


@dataclass
class StateTracker:
    """Follows one page's graphics state and records which hidden-text rules fired."""

    media_box: tuple[float, float, float, float]
    alphas: dict[str, float]  # ExtGState name -> fill alpha (/ca), page resources only
    state: GraphicsState = field(default_factory=GraphicsState)
    stack: list[GraphicsState] = field(default_factory=list)
    forms: list[tuple[Matrix, int]] = field(default_factory=list)  # parent CTM, stack depth
    rules: set[str] = field(default_factory=set)

    def before(self, operator: bytes, operands: list[Any], cm: Any, tm: Any) -> None:
        if operator == b"Do":
            self.forms.append((self.page_matrix(cm), len(self.stack)))
        elif operator == b"q":
            self.stack.append(GraphicsState(**vars(self.state)))
        elif operator == b"Q":
            if self.stack:
                self.state = self.stack.pop()
        elif operator in TEXT_SHOWING:
            if shows_text(operator, operands):
                self.check_text(multiply(as_matrix(tm), self.page_matrix(cm)))
        else:
            self.update_state(operator, operands)

    def after(self, operator: bytes, operands: list[Any], cm: Any, tm: Any) -> None:
        """A form XObject has finished: drop anything it left on the stack."""
        if operator == b"Do" and self.forms:
            _, depth = self.forms.pop()
            if len(self.stack) > depth:
                self.state = self.stack[depth]
                del self.stack[depth:]

    def page_matrix(self, cm: Any) -> Matrix:
        """pypdf restarts the CTM at identity inside a form, so compose it with the parent's."""
        local = as_matrix(cm)
        return multiply(local, self.forms[-1][0]) if self.forms else local

    def update_state(self, operator: bytes, operands: list[Any]) -> None:
        state = self.state
        numbers = [float(value) for value in operands if isinstance(value, int | float)]
        if operator in (b"g", b"rg", b"k", b"sc", b"scn"):
            state.fill = tuple(numbers)
        elif operator in (b"G", b"RG", b"K", b"SC", b"SCN"):
            state.stroke = tuple(numbers)
        elif operator == b"cs":
            state.fill = (0.0,)
        elif operator == b"CS":
            state.stroke = (0.0,)
        elif operator == b"Tr" and numbers:
            state.render_mode = int(numbers[0])
        elif operator == b"Tf" and numbers:
            state.font_size = numbers[-1]
        elif operator == b"gs" and operands:
            state.fill_alpha = self.alphas.get(str(operands[0]), state.fill_alpha)

    def check_text(self, matrix: Matrix) -> None:
        state = self.state
        if state.render_mode in INVISIBLE_MODES:
            self.rules.add("invisible_render_mode")
        elif is_white_text(state):
            self.rules.add("white_text")
        if state.fill_alpha < MIN_ALPHA:
            self.rules.add("transparent_text")
        vertical_scale = math.hypot(matrix[2], matrix[3])
        if state.font_size is not None and abs(state.font_size) * vertical_scale < MIN_FONT_SIZE:
            self.rules.add("tiny_font")
        if not self.inside_page(matrix[4], matrix[5]):
            self.rules.add("outside_page")

    def inside_page(self, x: float, y: float) -> bool:
        x0, y0, x1, y1 = self.media_box
        tolerance = PAGE_TOLERANCE
        return x0 - tolerance <= x <= x1 + tolerance and y0 - tolerance <= y <= y1 + tolerance


def is_white(colour: tuple[float, ...]) -> bool:
    if len(colour) == 4:  # CMYK
        return all(value <= 1 - WHITE for value in colour)
    return len(colour) in (1, 3) and all(value >= WHITE for value in colour)


def is_white_text(state: GraphicsState) -> bool:
    if state.render_mode in STROKE_MODES:
        return is_white(state.stroke)
    if state.render_mode in FILL_AND_STROKE_MODES:
        return is_white(state.fill) and is_white(state.stroke)
    return is_white(state.fill)


def shows_text(operator: bytes, operands: list[Any]) -> bool:
    """True when the operator would draw at least one non-blank character."""
    strings = operands[2:] if operator == b'"' else operands
    for value in strings:
        if isinstance(value, ArrayObject):
            if any(is_nonblank(item) for item in value):
                return True
        elif is_nonblank(value):
            return True
    return False


def is_nonblank(value: Any) -> bool:
    """Undecoded bytes count as text: a font can map any byte, even 0x20, to a visible glyph."""
    if isinstance(value, NameObject):
        return False
    if isinstance(value, bytes):
        return bool(value)
    return isinstance(value, str) and bool(value.strip())


def as_matrix(values: Any) -> Matrix:
    a, b, c, d, e, f = (float(value) for value in values)
    return (a, b, c, d, e, f)


def multiply(first: Matrix, second: Matrix) -> Matrix:
    """`first` then `second`, in PDF row-vector order."""
    a1, b1, c1, d1, e1, f1 = first
    a2, b2, c2, d2, e2, f2 = second
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def fill_alphas(page: Any) -> dict[str, float]:
    resources = page.get("/Resources")
    states = resources.get_object().get("/ExtGState") if resources is not None else None
    if states is None:
        return {}
    alphas: dict[str, float] = {}
    for name, value in states.get_object().items():
        alpha = value.get_object().get("/ca")
        if alpha is not None:
            alphas[str(name)] = float(alpha)
    return alphas


def open_reader(data: bytes, max_bytes: int, max_pages: int) -> PdfReader:
    if len(data) > max_bytes:
        raise PdfUnreadable(f"PDF has {len(data)} bytes, the limit is {max_bytes}")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise PdfUnreadable("PDF is encrypted")
        page_count = len(reader.pages)
    except PdfUnreadable:
        raise
    except Exception as exc:
        raise PdfUnreadable(f"PDF cannot be parsed ({type(exc).__name__})") from exc
    if page_count == 0 or page_count > max_pages:
        raise PdfUnreadable(f"PDF has {page_count} pages, the limit is {max_pages}")
    return reader


def metadata_findings(reader: PdfReader) -> tuple[Finding, ...]:
    values = [str(value) for value in (reader.metadata or {}).values()]
    return scan("\n".join(values), "metadata")


def read_pdf(data: bytes, max_bytes: int, max_pages: int) -> PdfText:
    reader = open_reader(data, max_bytes, max_pages)
    texts: list[str] = []
    findings: list[Finding] = []
    try:
        for number, page in enumerate(reader.pages, start=1):
            box = page.mediabox
            tracker = StateTracker(
                media_box=(float(box.left), float(box.bottom), float(box.right), float(box.top)),
                alphas=fill_alphas(page),
            )
            texts.append(
                page.extract_text(
                    visitor_operand_before=tracker.before, visitor_operand_after=tracker.after
                )
            )
            findings.extend(
                Finding(HIDDEN_TEXT, rule, f"page {number}") for rule in sorted(tracker.rules)
            )
        findings.extend(metadata_findings(reader))
    except Exception as exc:
        raise PdfUnreadable(f"PDF cannot be parsed ({type(exc).__name__})") from exc
    return PdfText(text="\n".join(texts), findings=tuple(findings), page_count=len(reader.pages))
