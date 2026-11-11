"""PII masking for everything sent to an LLM or a trace (threat model T2, decision 038).

Plain code, no AI. IBANs (mod-97), emails, phone numbers (international and Lithuanian
national), card numbers (network prefix + Luhn) and Lithuanian personal codes (checksum) are
replaced with typed placeholders like `[IBAN_1]`. The same value gets the same placeholder
within one call.

`mask(text)` is the only public function. The placeholder mapping lives in a local variable
for one call and is never logged or returned: model output never needs the originals back,
because no extracted field holds PII.

Every pattern is linear on untrusted text: fixed-size or separator-delimited groups, bounded
repeats, and a lookbehind so a match can only start at a token boundary.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field

SEPARATORS = " -."
ID_LABEL = re.compile(r"\bID:?\s*$")
ID_LABEL_WINDOW = 16


@dataclass(frozen=True)
class Detector:
    kind: str
    pattern: re.Pattern[str]
    is_valid: Callable[[str], bool]
    never_after: re.Pattern[str] | None = None  # text right before a match that rules it out


@dataclass
class Placeholders:
    mapping: dict[str, str] = field(default_factory=dict)  # placeholder -> original text
    by_key: dict[tuple[str, str], str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    def for_value(self, kind: str, value: str) -> str:
        key = (kind, normalize(value))
        if key not in self.by_key:
            self.counts[kind] = self.counts.get(kind, 0) + 1
            placeholder = f"[{kind}_{self.counts[kind]}]"
            self.by_key[key] = placeholder
            self.mapping[placeholder] = value
        return self.by_key[key]


def normalize(value: str) -> str:
    return re.sub(r"[\s().-]", "", value).lower()


def digits_of(value: str) -> str:
    return re.sub(r"\D", "", value)


# --- validators ---------------------------------------------------------------------------


def iban_checksum_ok(value: str) -> bool:
    """ISO 13616: move the first four characters to the end, letters become 10-35, mod 97 == 1."""
    compact = value.replace(" ", "")
    if not 15 <= len(compact) <= 34 or not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]+", compact):
        return False
    rearranged = compact[4:] + compact[:4]
    return int("".join(str(int(char, 36)) for char in rearranged)) % 97 == 1


def luhn_ok(number: str) -> bool:
    total = 0
    for position, char in enumerate(reversed(number)):
        digit = int(char)
        if position % 2 == 1:
            digit = digit * 2 - 9 if digit > 4 else digit * 2
        total += digit
    return total % 10 == 0


def card_network_ok(number: str) -> bool:
    """The issuer prefix and length match a real card network (Visa, Mastercard, Amex, Discover)."""
    length = len(number)
    if number.startswith("4"):
        return length in (13, 16, 19)
    if 51 <= int(number[:2]) <= 55 or 2221 <= int(number[:4]) <= 2720:
        return length == 16
    if number[:2] in ("34", "37"):
        return length == 15
    if number.startswith(("6011", "65")):
        return length in (16, 19)
    return False


def card_ok(value: str) -> bool:
    number = digits_of(value)
    return 13 <= len(number) <= 19 and card_network_ok(number) and luhn_ok(number)


def personal_code_ok(value: str) -> bool:
    """Lithuanian asmens kodas: weights 1-9,1 mod 11; if 10, weights 3-9,1-3 mod 11; if 10, 0."""
    digits = [int(char) for char in value]
    first = sum(d * w for d, w in zip(digits, (1, 2, 3, 4, 5, 6, 7, 8, 9, 1), strict=False)) % 11
    if first != 10:
        return first == digits[10]
    second = sum(d * w for d, w in zip(digits, (3, 4, 5, 6, 7, 8, 9, 1, 2, 3), strict=False)) % 11
    return (0 if second == 10 else second) == digits[10]


def international_phone_ok(value: str) -> bool:
    """E.164 allows at most 15 digits; 8 is the shortest real subscriber number with a code."""
    number = digits_of(value)
    if value.startswith("00"):
        number = number[2:]
    return 8 <= len(number) <= 15


def lt_national_phone_ok(value: str) -> bool:
    """`8` + area or mobile code + subscriber number: always 9 digits."""
    return len(digits_of(value)) == 9


def always(_: str) -> bool:
    return True


# --- patterns -----------------------------------------------------------------------------

EMAIL = re.compile(
    r"(?<![\w.%+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,8}"
    r"\.[A-Za-z]{2,24}(?![\w-])"
)
IBAN = re.compile(
    r"(?<![A-Za-z0-9])[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?(?![A-Za-z0-9])"
)
CARD = re.compile(
    r"(?<![\w-])(?:\d{4}(?:[ -]\d{4}){2,3}(?:[ -]\d{1,3})?|\d{4}[ -]\d{6}[ -]\d{5}|\d{13,19})(?![\w-])"
)
PERSONAL_CODE = re.compile(
    r"(?<![\w-])[1-6]\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{4}(?![\w-])"
)
INTERNATIONAL_PHONE = re.compile(
    r"(?<![\w+])(?:\+|00)[1-9]\d{0,14}(?:[ .-]?\(\d{1,4}\)|[ .-]\d{1,14}|(?<=\))\d{1,14}){0,8}(?!\w)"
)
LT_NATIONAL_PHONE = re.compile(
    r"(?<![\w.,/+-])(?:\(8[ -]?\d{1,3}\)|8[ -]?\(\d{1,3}\)|8)(?:[ -]?\d){5,8}(?![\w/-]|[.,]\d)"
)

# Order matters: emails first (their local part may hold digits), phones last (the loosest).
DETECTORS = (
    Detector("EMAIL", EMAIL, always),
    Detector("IBAN", IBAN, iban_checksum_ok),
    Detector("CARD", CARD, card_ok, never_after=ID_LABEL),
    Detector("PERSONAL_CODE", PERSONAL_CODE, personal_code_ok),
    Detector("PHONE", INTERNATIONAL_PHONE, international_phone_ok),
    Detector("PHONE", LT_NATIONAL_PHONE, lt_national_phone_ok),
)


# --- masking ------------------------------------------------------------------------------


def mask(text: str) -> str:
    """Replace every IBAN, email, phone, card number and personal code with a placeholder."""
    masked, _ = _mask_with_mapping(text)
    return masked


def _mask_with_mapping(text: str) -> tuple[str, dict[str, str]]:
    """Masked text plus placeholder -> original. Private: for this module's tests only."""
    placeholders = Placeholders()
    for detector in DETECTORS:
        text = apply_detector(text, detector, placeholders)
    return text, placeholders.mapping


def apply_detector(text: str, detector: Detector, placeholders: Placeholders) -> str:
    """Replace valid matches. A match that fails validation is retried cut at each separator,
    longest first, so a trailing token (a date, an amount) never hides the value before it."""
    parts: list[str] = []
    done = 0
    position = 0
    while (match := detector.pattern.search(text, position)) is not None:
        start = match.start()
        end = valid_end(match.group(), detector) if not ruled_out(text, start, detector) else None
        if end is None:
            position = start + 1
            continue
        parts.append(text[done:start])
        parts.append(placeholders.for_value(detector.kind, match.group()[:end]))
        done = position = start + end
    parts.append(text[done:])
    return "".join(parts)


def valid_end(candidate: str, detector: Detector) -> int | None:
    cuts = [len(candidate)] + [
        index for index in range(len(candidate) - 1, 0, -1) if candidate[index] in SEPARATORS
    ]
    for cut in cuts:
        if detector.is_valid(candidate[:cut]):
            return cut
    return None


def ruled_out(text: str, start: int, detector: Detector) -> bool:
    if detector.never_after is None:
        return False
    return detector.never_after.search(text[max(0, start - ID_LABEL_WINDOW) : start]) is not None
