"""Opaque cursors over an immutable row sequence, used by the token-paged platforms."""

from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Sequence

from fastapi import HTTPException

from mock_platforms.store import DeliveryRow

MAX_PAGE_SIZE = 1000
OFFSET_PREFIX = "offset"


def encode_offset(offset: int) -> str:
    return urlsafe_b64encode(f"{OFFSET_PREFIX}:{offset}".encode()).decode()


def decode_offset(token: str) -> int:
    try:
        prefix, _, digits = urlsafe_b64decode(token).decode().partition(":")
        offset = int(digits)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid page token") from error
    if prefix != OFFSET_PREFIX or offset < 0:
        raise HTTPException(status_code=400, detail="Invalid page token")
    return offset


def token_page(
    rows: Sequence[DeliveryRow], token: str | None, size: int
) -> tuple[Sequence[DeliveryRow], str | None]:
    """Return one page and the token for the next one (None on the last page)."""
    offset = decode_offset(token) if token is not None else 0
    next_offset = offset + size
    return rows[offset:next_offset], encode_offset(next_offset) if next_offset < len(rows) else None
