from math import ceil
from typing import Annotated, Any

from fastapi import Query, Request

from mock_platforms.deps import (
    parse_date_range,
    platform_account,
    platform_accounts,
    platform_router,
)
from mock_platforms.pagination import MAX_PAGE_SIZE
from mock_platforms.store import DeliveryRow

PLATFORM = "tiktok"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_PAGE_SIZE = 50

router = platform_router(PLATFORM)


def tiktok_row(row: DeliveryRow) -> dict[str, Any]:
    return {**row.shared_fields(DATE_FORMAT), "cost": str(row.spend)}


def success(data: dict[str, Any]) -> dict[str, Any]:
    return {"code": 0, "message": "OK", "data": data}


@router.get("/advertisers")
def list_advertisers(request: Request) -> dict[str, Any]:
    return success(
        {"list": [account.summary() for account in platform_accounts(request, PLATFORM)]}
    )


@router.get("/advertisers/{account_id}/report")
def report(
    request: Request,
    account_id: str,
    start_date: str,
    end_date: str,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    account = platform_account(request, PLATFORM, account_id)
    start, end = parse_date_range(start_date, end_date, DATE_FORMAT)
    rows = account.rows_between(start, end)
    return success(
        {
            "list": [tiktok_row(row) for row in rows[(page - 1) * page_size : page * page_size]],
            "page_info": {
                "page": page,
                "page_size": page_size,
                "total_number": len(rows),
                "total_page": ceil(len(rows) / page_size),
            },
        }
    )
