"""Pipeline step 1: store each platform's daily delivery in `raw.*`, exactly as received."""

import json
import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Self

import httpx
from app.db.models.raw import GoogleResponse, MetaResponse, TiktokResponse
from app.db.session import create_etl_engine
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Engine, Text, cast, delete, insert, literal
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DataError

REQUEST_TIMEOUT_SECONDS = 30.0
MAX_PAGE_SIZE = 1000
RATE_LIMITED = "rate_limited"
SERVER_ERROR = "server_error"

Body = dict[str, Any]
RawModel = type[MetaResponse] | type[GoogleResponse] | type[TiktokResponse]


class IngestSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INGEST_", env_file=".env", extra="ignore")

    base_url: str
    token: SecretStr = Field(validation_alias="MOCK_PLATFORMS_TOKEN", min_length=1)
    period_start: date
    period_end: date
    page_size: int = Field(default=100, ge=1, le=MAX_PAGE_SIZE)

    @model_validator(mode="after")
    def period_is_ordered(self) -> Self:
        if self.period_start > self.period_end:
            raise ValueError("period_start must not be after period_end")
        return self


@dataclass(frozen=True)
class RetryPolicy:
    base_delay: float = 1.0
    cap: float = 30.0
    max_attempts_429: int = 6
    max_attempts_5xx: int = 4

    def backoff(self, attempt: int) -> float:
        """Delay after the nth failed attempt of one kind."""
        return min(self.cap, self.base_delay * 2 ** (attempt - 1))

    def max_attempts(self, kind: str) -> int:
        return self.max_attempts_429 if kind == RATE_LIMITED else self.max_attempts_5xx


@dataclass(frozen=True)
class IngestFailure:
    platform: str
    account: str | None  # None when the platform's account list could not be fetched.
    reason: str


@dataclass
class IngestResult:
    run_id: uuid.UUID
    pages_fetched: int = 0
    rows_seen: int = 0
    retries: int = 0
    failures: list[IngestFailure] = field(default_factory=list)


@dataclass(frozen=True)
class PlatformApi:
    name: str
    model: RawModel
    accounts_path: str
    report_path: str
    start_param: str
    end_param: str
    size_param: str
    date_format: str
    accounts_of: Callable[[Body], list[Body]]
    rows_of: Callable[[Body], list[Body]]
    next_params: Callable[[Body], dict[str, Any] | None]


@dataclass(frozen=True)
class FetchedPage:
    body: bytes
    text: str
    fetched_at: datetime


@dataclass(frozen=True)
class IngestRun:
    run_id: uuid.UUID
    settings: IngestSettings
    client: httpx.Client
    engine: Engine
    sleep: Callable[[float], None]
    policy: RetryPolicy
    result: IngestResult


class FetchError(Exception):
    """This platform or account could not be read; the rest of the run continues."""


def meta_next_params(body: Body) -> dict[str, Any] | None:
    cursor = body["paging"]["cursors"].get("after")
    return {"after": cursor} if cursor else None


def google_next_params(body: Body) -> dict[str, Any] | None:
    token = body.get("nextPageToken")
    return {"page_token": token} if token else None


def tiktok_next_params(body: Body) -> dict[str, Any] | None:
    info = body["data"]["page_info"]
    return {"page": info["page"] + 1} if info["page"] < info["total_page"] else None


PLATFORMS = (
    PlatformApi(
        name="meta",
        model=MetaResponse,
        accounts_path="/meta/adaccounts",
        report_path="/meta/adaccounts/{account_id}/insights",
        start_param="since",
        end_param="until",
        size_param="limit",
        date_format="%Y-%m-%d",
        accounts_of=lambda body: body["data"],
        rows_of=lambda body: body["data"],
        next_params=meta_next_params,
    ),
    PlatformApi(
        name="google",
        model=GoogleResponse,
        accounts_path="/google/customers",
        report_path="/google/customers/{account_id}/report",
        start_param="start_date",
        end_param="end_date",
        size_param="page_size",
        date_format="%Y%m%d",
        accounts_of=lambda body: body["results"],
        rows_of=lambda body: body["results"],
        next_params=google_next_params,
    ),
    PlatformApi(
        name="tiktok",
        model=TiktokResponse,
        accounts_path="/tiktok/advertisers",
        report_path="/tiktok/advertisers/{account_id}/report",
        start_param="start_date",
        end_param="end_date",
        size_param="page_size",
        date_format="%Y-%m-%d %H:%M:%S",
        accounts_of=lambda body: body["data"]["list"],
        rows_of=lambda body: body["data"]["list"],
        next_params=tiktok_next_params,
    ),
)


def retry_after_seconds(response: httpx.Response) -> float:
    """The numeric Retry-After header; 0 when it is absent or an HTTP date."""
    value = response.headers.get("Retry-After", "")
    return float(value) if value.isdecimal() else 0.0


def retry_kind(response: httpx.Response | None) -> str | None:
    """Which retry budget a failed request uses; None means retrying cannot help."""
    if response is None or response.status_code >= 500:
        return SERVER_ERROR
    return RATE_LIMITED if response.status_code == 429 else None


def retry_delay(response: httpx.Response | None, attempt: int, policy: RetryPolicy) -> float:
    """The cap limits our own backoff; it never shortens a server's Retry-After."""
    backoff = policy.backoff(attempt)
    if retry_kind(response) == RATE_LIMITED and response is not None:
        return max(retry_after_seconds(response), backoff)
    return backoff


def send(run: IngestRun, path: str, params: dict[str, Any]) -> tuple[httpx.Response | None, str]:
    """One attempt: the response (None on a network failure) and a description of it."""
    headers = {"Authorization": f"Bearer {run.settings.token.get_secret_value()}"}
    try:
        response = run.client.get(path, params=params, headers=headers)
    except httpx.RequestError as error:
        return None, type(error).__name__
    return response, f"HTTP {response.status_code}"


def get_with_retries(run: IngestRun, path: str, params: dict[str, Any]) -> httpx.Response:
    attempts: Counter[str] = Counter()
    while True:
        response, outcome = send(run, path, params)
        if response is not None and response.status_code == 200:
            return response
        kind = retry_kind(response)
        if kind is None:
            raise FetchError(f"{outcome} from {path}")
        attempts[kind] += 1
        if attempts[kind] >= run.policy.max_attempts(kind):
            raise FetchError(f"{outcome} from {path} after {attempts[kind]} attempts")
        run.result.retries += 1
        run.sleep(retry_delay(response, attempts[kind], run.policy))


def parse_json(response: httpx.Response, path: str) -> tuple[str, Body]:
    try:
        text = response.content.decode("utf-8")
        body = json.loads(text)
    except ValueError as error:
        raise FetchError(f"Unparsable response from {path}") from error
    if not isinstance(body, dict):
        raise FetchError(f"Unexpected response shape from {path}")
    return text, body


def list_account_ids(run: IngestRun, platform: PlatformApi) -> list[str]:
    response = get_with_retries(run, platform.accounts_path, {})
    _, body = parse_json(response, platform.accounts_path)
    try:
        return [account["account_id"] for account in platform.accounts_of(body)]
    except (KeyError, TypeError, AttributeError) as error:
        raise FetchError(f"Unexpected response shape from {platform.accounts_path}") from error


def read_page_shape(
    platform: PlatformApi, body: Body, path: str
) -> tuple[int, dict[str, Any] | None]:
    """The page's row count and the params for the next page (None on the last page)."""
    try:
        return len(platform.rows_of(body)), platform.next_params(body)
    except (KeyError, TypeError, AttributeError) as error:
        raise FetchError(f"Unexpected response shape from {path}") from error


def report_params(run: IngestRun, platform: PlatformApi) -> dict[str, Any]:
    settings = run.settings
    return {
        platform.start_param: settings.period_start.strftime(platform.date_format),
        platform.end_param: settings.period_end.strftime(platform.date_format),
        platform.size_param: settings.page_size,
    }


def fetch_account_pages(
    run: IngestRun, platform: PlatformApi, account_id: str
) -> list[FetchedPage]:
    path = platform.report_path.format(account_id=account_id)
    params = report_params(run, platform)
    pages: list[FetchedPage] = []
    cursor: dict[str, Any] = {}
    seen_cursors = [cursor]
    while True:
        response = get_with_retries(run, path, params | cursor)
        text, body = parse_json(response, path)
        row_count, next_cursor = read_page_shape(platform, body, path)
        pages.append(FetchedPage(response.content, text, datetime.now(UTC)))
        run.result.pages_fetched += 1
        run.result.rows_seen += row_count
        if next_cursor is None:
            return pages
        if next_cursor in seen_cursors:
            raise FetchError(f"Pagination did not advance on {path}")
        seen_cursors.append(next_cursor)
        cursor = next_cursor


def store_snapshot(
    run: IngestRun, platform: PlatformApi, account_id: str, pages: list[FetchedPage]
) -> None:
    """Replace this account + period in one transaction, so a re-run cannot duplicate pages."""
    model = platform.model
    period = {
        "account_id": account_id,
        "period_start": run.settings.period_start,
        "period_end": run.settings.period_end,
    }
    with run.engine.begin() as conn:
        conn.execute(delete(model).filter_by(**period))
        for number, page in enumerate(pages, start=1):
            conn.execute(
                insert(model).values(
                    **period,
                    run_id=run.run_id,
                    page=number,
                    fetched_at=page.fetched_at,
                    body=page.body,
                    # Postgres parses the text itself, so numbers never pass through a float.
                    payload=cast(literal(page.text, Text), JSONB),
                )
            )


def record_failure(run: IngestRun, platform: PlatformApi, account: str | None, reason: str) -> None:
    run.result.failures.append(IngestFailure(platform.name, account, reason))


def ingest_account(run: IngestRun, platform: PlatformApi, account_id: str) -> None:
    """All pages or none: a partial account would look like missing delivery days downstream."""
    try:
        store_snapshot(run, platform, account_id, fetch_account_pages(run, platform, account_id))
    except FetchError as error:
        record_failure(run, platform, account_id, str(error))
    except DataError:
        record_failure(run, platform, account_id, "Response rejected by the database")


def ingest_platform(run: IngestRun, platform: PlatformApi) -> None:
    try:
        account_ids = list_account_ids(run, platform)
    except FetchError as error:
        record_failure(run, platform, None, str(error))
        return
    for account_id in account_ids:
        ingest_account(run, platform, account_id)


def run_ingest(
    run_id: uuid.UUID,
    settings: IngestSettings,
    client: httpx.Client,
    engine: Engine,
    sleep: Callable[[float], None] = time.sleep,
    policy: RetryPolicy | None = None,
) -> IngestResult:
    """Fetch every account of every platform; platform failures end up in the result."""
    result = IngestResult(run_id=run_id)
    run = IngestRun(run_id, settings, client, engine, sleep, policy or RetryPolicy(), result)
    for platform in PLATFORMS:
        ingest_platform(run, platform)
    return result


def ingest_from_env(run_id: uuid.UUID) -> IngestResult:
    settings = IngestSettings()
    engine = create_etl_engine()
    try:
        with httpx.Client(base_url=settings.base_url, timeout=REQUEST_TIMEOUT_SECONDS) as client:
            return run_ingest(run_id, settings, client, engine)
    finally:
        engine.dispose()
