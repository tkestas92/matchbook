"""HTTP helpers shared by the platform routers."""

import secrets
from collections.abc import Callable
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from mock_platforms.store import Account

bearer = HTTPBearer(auto_error=False)


def require_token(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> None:
    expected = request.app.state.settings.token.get_secret_value().encode()
    supplied = credentials.credentials.encode() if credentials else b""
    if not secrets.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def inject_fault(platform: str) -> Callable[[Request], None]:
    def dependency(request: Request) -> None:
        state = request.app.state
        status = state.faults.next_status(platform)
        if status == 429:
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded",
                headers={"Retry-After": str(state.settings.retry_after_seconds)},
            )
        if status == 500:
            raise HTTPException(status_code=500, detail="Internal server error")

    return dependency


def platform_router(platform: str) -> APIRouter:
    """Router whose routes authenticate first, so a rejected call never consumes a fault slot."""
    return APIRouter(
        prefix=f"/{platform}",
        dependencies=[Depends(require_token), Depends(inject_fault(platform))],
    )


def platform_accounts(request: Request, platform: str) -> list[Account]:
    return list(request.app.state.store[platform].values())


def platform_account(request: Request, platform: str, account_id: str) -> Account:
    account = request.app.state.store[platform].get(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Unknown ad account")
    return account


def parse_date(raw: str, date_format: str) -> date:
    """Parse strictly: strptime alone accepts unpadded values such as `2026-1-5`."""
    try:
        parsed = datetime.strptime(raw, date_format)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=f"Invalid date: {raw}") from error
    if parsed.strftime(date_format) != raw:
        raise HTTPException(status_code=400, detail=f"Invalid date: {raw}")
    return parsed.date()


def parse_date_range(raw_start: str, raw_end: str, date_format: str) -> tuple[date, date]:
    start, end = parse_date(raw_start, date_format), parse_date(raw_end, date_format)
    if start > end:
        raise HTTPException(status_code=400, detail="Date range start is after its end")
    return start, end
