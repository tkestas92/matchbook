"""Printed vendor -> canonical vendor (decision 044). Hand-built names only."""

import pytest
from app.extraction.vendors import KnownVendors, vendor_key

KNOWN = KnownVendors.of(("Meta", "Google", "TikTok", "UAB Matchbook Media"))


@pytest.mark.parametrize(
    ("printed", "canonical"),
    [
        ("Meta", "Meta"),
        ("Meta Ads", "Meta"),
        ("TikTok Ads", "TikTok"),
        ("TikTok For Business", "TikTok"),
        ("META ADS.", "Meta"),
        ("meta-ads", "Meta"),
        ("  Meta   Ads  ", "Meta"),
        ("Google Advertising", "Google"),
        ("Ｍｅｔａ Ads", "Meta"),  # full-width letters, NFKC
        ("UAB Matchbook Media", "UAB Matchbook Media"),
        ("uab matchbook media", "UAB Matchbook Media"),
    ],
)
def test_printed_names_resolve_to_the_canonical_name(printed: str, canonical: str) -> None:
    assert KNOWN.resolve(printed) == canonical


@pytest.mark.parametrize(
    "printed",
    ["Acme Media", "Meta Platforms Ireland Limited", "Matchbook Media", "Ads", "", "  ", "..."],
)
def test_unknown_names_do_not_resolve(printed: str) -> None:
    assert KNOWN.resolve(printed) is None


def test_suffixes_are_never_stripped_to_nothing() -> None:
    assert vendor_key("Ads") == "ads"
    assert vendor_key("For Business") == "for business"
    assert vendor_key("Meta Ads Ads") == "meta"


def test_suffix_must_be_a_whole_word() -> None:
    assert vendor_key("Metaads") == "metaads"
    assert KNOWN.resolve("Metaads") is None


def test_empty_known_list_resolves_nothing() -> None:
    empty = KnownVendors.of(())
    assert empty.resolve("Meta") is None
    assert empty.canonical_names == ()


def test_canonical_names_are_sorted() -> None:
    assert KNOWN.canonical_names == ("Google", "Meta", "TikTok", "UAB Matchbook Media")


def test_a_name_that_normalises_to_nothing_is_rejected() -> None:
    with pytest.raises(ValueError):
        KnownVendors.of(("Meta", "--"))


def test_two_names_with_the_same_key_are_rejected() -> None:
    with pytest.raises(ValueError):
        KnownVendors.of(("Meta", "Meta Ads"))


def test_the_same_name_twice_is_fine() -> None:
    assert KnownVendors.of(("Meta", "Meta")).canonical_names == ("Meta",)
