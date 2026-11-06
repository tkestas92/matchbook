from datetime import datetime
from typing import Any

from app.db.base import Base, CreatedAtMixin, IntPk, Json, RunIdMixin
from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import Mapped, declared_attr


class RawResponseMixin(RunIdMixin, CreatedAtMixin):
    """One API response page, stored exactly as received."""

    id: Mapped[IntPk]
    page: Mapped[int]
    fetched_at: Mapped[datetime]
    payload: Mapped[Json]

    @declared_attr.directive
    def __table_args__(cls) -> tuple[Any, ...]:
        return (UniqueConstraint("run_id", "page"), {"schema": "raw"})


class MetaResponse(RawResponseMixin, Base):
    __tablename__ = "meta_responses"


class GoogleResponse(RawResponseMixin, Base):
    __tablename__ = "google_responses"


class TiktokResponse(RawResponseMixin, Base):
    __tablename__ = "tiktok_responses"
