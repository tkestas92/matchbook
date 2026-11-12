"""Suspicious-content rules for invoice text (threat model T1, decision 042).

Plain code, no AI. Any finding rejects the document before an LLM sees it. The text is
normalised first (NFKC, format characters such as zero-width spaces removed, casefolded,
whitespace collapsed), so spacing, case and invisible characters do not hide a phrase.

A `Finding` holds rule codes only, never the matched text: document text is untrusted and is
never copied into reasons, logs or issues.

Every pattern is bounded (fixed words, `{0,n}` word gaps), so untrusted text cannot cause
catastrophic backtracking.
"""

import re
import unicodedata
from dataclasses import dataclass

CONTENT = "content"
HIDDEN_TEXT = "hidden_text"
WORD_GAP = r"(?: [\w-]+){0,%d}"

RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ignore_instructions",
        re.compile(
            r"\b(?:ignore|disregard|forget|override)\b"
            + WORD_GAP % 3
            + r" (?:instructions?|prompts?|rules)\b"
        ),
    ),
    ("system_prompt", re.compile(r"\bsystem (?:instructions?|prompts?|messages?)\b")),
    (
        "addresses_ai",
        re.compile(
            r"\b(?:note|message|instructions?) (?:to|for) (?:the |an |any )?"
            r"(?:ai|llm|assistant|model|language model)\b"
            r"|\bai assistant\b|\blanguage model\b"
        ),
    ),
    (
        "role_play",
        re.compile(r"\byou are (?:now )?(?:an?|the) (?:ai|assistant|model|extractor|system)\b"),
    ),
    (
        "skip_checks",
        re.compile(
            r"\b(?:skip|bypass|disable)\b"
            + WORD_GAP % 2
            + r" (?:checks?|validations?|verifications?|reviews?)\b"
        ),
    ),
    (
        "approval",
        re.compile(
            r"\bpre-?approved\b|\bapproved for payment\b"
            r"|\b(?:mark|set|flag)\b" + WORD_GAP % 5 + r" (?:as|to) (?:approved|paid|verified)\b"
        ),
    ),
    (
        "change_value",
        re.compile(
            r"\b(?:change|report|set|replace|update)\b"
            + WORD_GAP % 3
            + r" (?:total|amount|vat|subtotal)\b"
        ),
    ),
    ("fake_json", re.compile(r"\"[^\"]{1,64}\" ?:")),
    ("url", re.compile(r"\bhttps?:|\bwww\.|\w\.[a-z]{2,24}/\S")),
    ("delimiter_spoof", re.compile(r"</? ?document\b")),
)


@dataclass(frozen=True)
class Finding:
    kind: str  # CONTENT or HIDDEN_TEXT
    rule: str
    source: str  # "text", "metadata" or "page N"

    @property
    def code(self) -> str:
        return f"{self.kind}:{self.rule}:{self.source}"


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = "".join(char for char in text if unicodedata.category(char) != "Cf")
    return " ".join(text.casefold().split())


def scan(text: str, source: str = "text") -> tuple[Finding, ...]:
    """One finding per rule that matches anywhere in `text`."""
    normalized = normalize(text)
    return tuple(
        Finding(CONTENT, rule, source) for rule, pattern in RULES if pattern.search(normalized)
    )
