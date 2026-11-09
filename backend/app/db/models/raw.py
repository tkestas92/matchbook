from datetime import date, datetime
from typing import Any

from app.db.base import Base, CreatedAtMixin, IntPk, Json, RunIdMixin
from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import Mapped, declared_attr


class RawResponseMixin(RunIdMixin, CreatedAtMixin):
    """One API response page, stored exactly as received."""

    id: Mapped[IntPk]
    account_id: Mapped[str]
    period_start: Mapped[date]
    period_end: Mapped[date]
    page: Mapped[int]
    fetched_at: Mapped[datetime]
    payload: Mapped[Json]
    # JSONB normalises key order and whitespace; these are the bytes as received.
    body: Mapped[bytes]

    @declared_attr.directive
    def __table_args__(cls) -> tuple[Any, ...]:
        return (
            UniqueConstraint("account_id", "period_start", "period_end", "page"),
            {"schema": "raw"},
        )


class MetaResponse(RawResponseMixin, Base):
    __tablename__ = "meta_responses"


class GoogleResponse(RawResponseMixin, Base):
    __tablename__ = "google_responses"


class TiktokResponse(RawResponseMixin, Base):
    __tablename__ = "tiktok_responses"
