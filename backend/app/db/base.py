import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, ClassVar

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Identity,
    MetaData,
    Numeric,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

MONEY = Numeric(12, 2)

IntPk = Annotated[int, mapped_column(BigInteger, Identity(always=True), primary_key=True)]
Json = dict[str, Any]


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        str: Text,
        Decimal: MONEY,
        datetime: DateTime(timezone=True),
        Json: JSONB,
    }


def one_of(column: str, values: tuple[str, ...]) -> CheckConstraint:
    """Enum-like TEXT column: a CHECK is cheaper to evolve than a PG ENUM."""
    allowed = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=column)


class CreatedAtMixin:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class UpdatedAtMixin:
    """Maintained by the `core.set_updated_at()` trigger, so upserts update it too."""

    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class RunIdMixin:
    """Pipeline run that last wrote the row. No FK: `core` must not depend on `app`."""

    run_id: Mapped[uuid.UUID]
