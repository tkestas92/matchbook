"""Printed vendor name -> canonical vendor name, in code (decision 044).

The model copies whatever heading the PDF prints (`Meta Ads`, `TikTok For Business`); the
canonical name comes from a known-vendors list the caller builds and passes in. The extractor
never reads the database. A name that does not resolve is never extracted automatically.

Normalisation: NFKC, casefold, every non-alphanumeric character becomes a space, whitespace is
collapsed, then trailing suffix words (`ads`, `advertising`, `for business`) are stripped,
never down to nothing. Legal forms (`UAB`, `Ltd`) are kept: they are part of canonical names.
"""

import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

SUFFIXES: tuple[tuple[str, ...], ...] = (("ads",), ("advertising",), ("for", "business"))


def words_of(name: str) -> list[str]:
    text = unicodedata.normalize("NFKC", name).casefold()
    return "".join(char if char.isalnum() else " " for char in text).split()


def strip_suffixes(words: list[str]) -> list[str]:
    stripped = True
    while stripped:
        stripped = False
        for suffix in SUFFIXES:
            if len(words) > len(suffix) and tuple(words[-len(suffix) :]) == suffix:
                words = words[: -len(suffix)]
                stripped = True
    return words


def vendor_key(name: str) -> str:
    return " ".join(strip_suffixes(words_of(name)))


@dataclass(frozen=True)
class KnownVendors:
    by_key: Mapping[str, str]  # normalised key -> canonical name

    @classmethod
    def of(cls, names: Iterable[str]) -> "KnownVendors":
        by_key: dict[str, str] = {}
        for name in names:
            key = vendor_key(name)
            if not key:
                raise ValueError("a known vendor name is empty after normalisation")
            if by_key.get(key, name) != name:
                raise ValueError("two known vendor names normalise to the same key")
            by_key[key] = name
        return cls(by_key)

    @property
    def canonical_names(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.by_key.values())))

    def resolve(self, printed: str) -> str | None:
        key = vendor_key(printed)
        return self.by_key.get(key) if key else None
