"""Cost caps for LLM calls (threat model T5, decisions 014 and 039).

Before every call the client asks the ledger whether the worst case (estimated input plus the full
output limit) still fits under the per-run and the monthly cap; if not, the call is refused before
anything is sent. After the call the real cost from the provider's token counts is recorded.
"""

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.config import ModelPrice
from app.db.models.app import PipelineRun
from app.llm.provider import LLMError
from sqlalchemy import Engine, func, select

MILLION = Decimal(1_000_000)


class CostCapExceeded(LLMError):
    """The call could push spending over a cap, so it was not made."""


class UnknownModelPrice(LLMError):
    """No price is configured for the model; a call without a price is never assumed free."""


def price_for(prices: dict[str, ModelPrice], model: str) -> ModelPrice:
    if model not in prices:
        raise UnknownModelPrice(f"no price configured for model {model!r}")
    return prices[model]


def cost_of(price: ModelPrice, input_tokens: int, output_tokens: int) -> Decimal:
    return (
        price.input_eur_per_mtok * input_tokens + price.output_eur_per_mtok * output_tokens
    ) / MILLION


def estimate_input_tokens(text: str) -> int:
    """Upper bound without a network call: real tokenizers average more than 2 bytes per token."""
    return math.ceil(len(text.encode("utf-8")) / 2)


def worst_case_cost(price: ModelPrice, text: str, max_output_tokens: int) -> Decimal:
    return cost_of(price, estimate_input_tokens(text), max_output_tokens)


@dataclass
class CostLedger:
    run_cap: Decimal
    monthly_cap: Decimal
    spent_this_month_before_run: Decimal = Decimal(0)
    total: Decimal = Decimal(0)
    tokens: int = 0
    calls: int = 0

    def check(self, estimate: Decimal) -> None:
        if self.total + estimate > self.run_cap:
            raise CostCapExceeded(
                f"per-run LLM cap EUR {self.run_cap} would be exceeded "
                f"(spent {self.total}, next call up to {estimate})"
            )
        month_total = self.spent_this_month_before_run + self.total + estimate
        if month_total > self.monthly_cap:
            raise CostCapExceeded(
                f"monthly LLM cap EUR {self.monthly_cap} would be exceeded "
                f"(this month {self.spent_this_month_before_run + self.total}, "
                f"next call up to {estimate})"
            )

    def record(self, cost: Decimal, tokens: int) -> None:
        self.total += cost
        self.tokens += tokens
        self.calls += 1


def month_start_utc(now: datetime, timezone: str) -> datetime:
    """Midnight on the 1st of `now`'s month in the agency timezone, as a UTC instant."""
    local = now.astimezone(ZoneInfo(timezone))
    first = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return first.astimezone(UTC)


def monthly_spend(
    engine: Engine, now: datetime, timezone: str, exclude_run_id: uuid.UUID | None = None
) -> Decimal:
    """Sum of `app.pipeline_runs.cost_eur` for runs started this month, without the current run."""
    query = select(func.coalesce(func.sum(PipelineRun.cost_eur), 0)).where(
        PipelineRun.started_at >= month_start_utc(now, timezone)
    )
    if exclude_run_id is not None:
        query = query.where(PipelineRun.id != exclude_run_id)
    with engine.connect() as conn:
        return Decimal(conn.execute(query).scalar_one())
