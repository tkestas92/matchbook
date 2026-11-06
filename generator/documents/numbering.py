"""Deterministic document numbers and dates.

Each number comes from its own RNG stream (decision 023), so adding a client or a month
never renumbers other documents.
"""

import random
from collections.abc import Callable
from datetime import date

from generator.world import next_month, rng_for

# Day of the following month on which each document type is issued.
PLATFORM_ISSUE_DAY = {"meta": 1, "google": 2, "tiktok": 3}
CREDIT_NOTE_ISSUE_DAY = 4
AGENCY_ISSUE_DAY = 5


def digits(rng: random.Random, count: int) -> str:
    return str(rng.randrange(10 ** (count - 1), 10**count))


def _meta_number(rng: random.Random, account_id: str, month: date) -> str:
    return f"FBADS-{account_id[-6:]}-{digits(rng, 9)}"


def _google_number(rng: random.Random, account_id: str, month: date) -> str:
    return digits(rng, 10)


def _tiktok_number(rng: random.Random, account_id: str, month: date) -> str:
    return f"BDUE{month:%Y%m}{digits(rng, 8)}"


PLATFORM_NUMBER_FORMATS: dict[str, Callable[[random.Random, str, date], str]] = {
    "meta": _meta_number,
    "google": _google_number,
    "tiktok": _tiktok_number,
}
CREDIT_NOTE_PREFIXES = {"meta": "FBCN", "google": "GCN", "tiktok": "BDCN"}


def platform_invoice_number(seed: int, platform_key: str, account_id: str, month: date) -> str:
    rng = rng_for(seed, "invoice-number", platform_key, account_id, f"{month:%Y-%m}")
    return PLATFORM_NUMBER_FORMATS[platform_key](rng, account_id, month)


def credit_note_number(seed: int, platform_key: str, original_number: str, month: date) -> str:
    rng = rng_for(seed, "credit-note-number", platform_key, original_number)
    return f"{CREDIT_NOTE_PREFIXES[platform_key]}-{month:%y%m}-{digits(rng, 6)}"


def agency_invoice_number(prefix: str, client_code: str, month: date) -> str:
    return f"{prefix}-{client_code}-{month:%y%m}"


def platform_issue_date(platform_key: str, month: date) -> date:
    return next_month(month).replace(day=PLATFORM_ISSUE_DAY[platform_key])


def credit_note_issue_date(month: date) -> date:
    return next_month(month).replace(day=CREDIT_NOTE_ISSUE_DAY)


def agency_issue_date(month: date) -> date:
    return next_month(month).replace(day=AGENCY_ISSUE_DAY)
