from datetime import date
from decimal import Decimal

from app.db.base import Base, CreatedAtMixin, IntPk, Json
from sqlalchemy.orm import Mapped


class AnswerKey(CreatedAtMixin, Base):
    """Planted errors. Evals only; no FKs so `eval` stays isolated from core/app."""

    __tablename__ = "answer_key"
    __table_args__ = ({"schema": "eval"},)

    id: Mapped[IntPk]
    error_type: Mapped[str]
    entity_ref: Mapped[str]
    period_start: Mapped[date]
    period_end: Mapped[date]
    magnitude: Mapped[Decimal | None]
    expected_detector: Mapped[str]
    details: Mapped[Json | None]
