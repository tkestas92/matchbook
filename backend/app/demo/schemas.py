"""Response models. Money is a decimal string with two places, never a float."""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, Field

MONEY_PATTERN = r"^-?\d+\.\d{2}$"
Money = Annotated[str, Field(pattern=MONEY_PATTERN, examples=["1234.50"])]


def money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):f}"


class Health(BaseModel):
    status: str


class Step(BaseModel):
    name: str
    status: str
    counts: dict[str, int] = Field(description="Integer counts only.")


class RunStatus(BaseModel):
    status: str
    started_at: datetime
    finished_at: datetime
    steps: list[Step]


class TypeSummary(BaseModel):
    type: str
    count: int
    amount_eur: Money


class Summary(BaseModel):
    total_count: int
    total_amount_eur: Money
    by_type: list[TypeSummary]


class Issue(BaseModel):
    type: str
    severity: str
    amount: Money | None
    explanation: str
    proposed_action: str | None
    period: date | None
    client_code: str | None
    platform: str | None


class IssuePage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[Issue]


class EvalRow(BaseModel):
    error_type: str
    planted: int
    found: int
    missed: int
    expected_missed: int
    false_alarms: int
    eur_planted: Money
    eur_found: Money


class EvalSummary(BaseModel):
    result: str
    by_type: list[EvalRow]
    overall: EvalRow
