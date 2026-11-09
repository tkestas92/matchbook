from typing import Annotated, Any

from fastapi import Query, Request

from mock_platforms.deps import (
    parse_date_range,
    platform_account,
    platform_accounts,
    platform_router,
)
from mock_platforms.pagination import MAX_PAGE_SIZE, token_page
from mock_platforms.store import DeliveryRow

PLATFORM = "google"
DATE_FORMAT = "%Y%m%d"
DEFAULT_PAGE_SIZE = 100
MICROS_PER_EUR = 1_000_000

router = platform_router(PLATFORM)


def google_row(row: DeliveryRow) -> dict[str, Any]:
    return {**row.shared_fields(DATE_FORMAT), "cost_micros": int(row.spend * MICROS_PER_EUR)}


@router.get("/customers")
def list_customers(request: Request) -> dict[str, Any]:
    return {"results": [account.summary() for account in platform_accounts(request, PLATFORM)]}


@router.get("/customers/{account_id}/report")
def report(
    request: Request,
    account_id: str,
    start_date: str,
    end_date: str,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    page_token: str | None = None,
) -> dict[str, Any]:
    account = platform_account(request, PLATFORM, account_id)
    start, end = parse_date_range(start_date, end_date, DATE_FORMAT)
    page, next_token = token_page(account.rows_between(start, end), page_token, page_size)
    body: dict[str, Any] = {"results": [google_row(row) for row in page]}
    if next_token:
        body["nextPageToken"] = next_token
    return body
