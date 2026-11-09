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

PLATFORM = "meta"
DATE_FORMAT = "%Y-%m-%d"
DEFAULT_LIMIT = 25

router = platform_router(PLATFORM)


def meta_row(row: DeliveryRow) -> dict[str, Any]:
    return {**row.shared_fields(DATE_FORMAT), "spend": str(row.spend)}


@router.get("/adaccounts")
def list_ad_accounts(request: Request) -> dict[str, Any]:
    return {"data": [account.summary() for account in platform_accounts(request, PLATFORM)]}


@router.get("/adaccounts/{account_id}/insights")
def insights(
    request: Request,
    account_id: str,
    since: str,
    until: str,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_LIMIT,
    after: str | None = None,
) -> dict[str, Any]:
    account = platform_account(request, PLATFORM, account_id)
    start, end = parse_date_range(since, until, DATE_FORMAT)
    page, next_cursor = token_page(account.rows_between(start, end), after, limit)
    return {
        "data": [meta_row(row) for row in page],
        "paging": {"cursors": {"after": next_cursor} if next_cursor else {}},
    }
