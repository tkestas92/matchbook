import uuid
from datetime import date, datetime
from decimal import Decimal

from app.db.base import (
    Base,
    CreatedAtMixin,
    IntPk,
    Json,
    RunIdMixin,
    UpdatedAtMixin,
    one_of,
)
from sqlalchemy import BigInteger, ForeignKey, Index, Numeric, func, text
from sqlalchemy.orm import Mapped, mapped_column

USER_ROLES = ("admin", "planner", "finance")
ISSUE_SEVERITIES = ("low", "medium", "high")
ISSUE_STATUSES = ("open", "approved", "edited", "rejected")
RUN_STATUSES = ("running", "succeeded", "failed", "partial")


class User(CreatedAtMixin, Base):
    __tablename__ = "users"
    __table_args__ = (one_of("role", USER_ROLES), {"schema": "app"})

    id: Mapped[IntPk]
    email: Mapped[str] = mapped_column(unique=True)
    role: Mapped[str]


class UserClientScope(CreatedAtMixin, Base):
    """Clients a user may see; no rows = no client access."""

    __tablename__ = "user_client_scope"
    __table_args__ = ({"schema": "app"},)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("app.users.id", ondelete="CASCADE"), primary_key=True
    )
    client_id: Mapped[int] = mapped_column(
        ForeignKey("core.clients.id", ondelete="CASCADE"), primary_key=True
    )


class Issue(RunIdMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    """Approval-queue card. `dedup_key` makes re-runs upsert instead of duplicate."""

    __tablename__ = "issues"
    __table_args__ = (
        Index(None, "status"),
        one_of("severity", ISSUE_SEVERITIES),
        one_of("status", ISSUE_STATUSES),
        {"schema": "app"},
    )

    id: Mapped[IntPk]
    dedup_key: Mapped[str] = mapped_column(unique=True)
    type: Mapped[str]
    severity: Mapped[str]
    client_id: Mapped[int | None] = mapped_column(ForeignKey("core.clients.id"))
    platform_id: Mapped[int | None] = mapped_column(ForeignKey("core.platforms.id"))
    period: Mapped[date | None]
    entity_type: Mapped[str | None]
    entity_id: Mapped[str | None]
    amount: Mapped[Decimal | None]
    explanation: Mapped[str]
    proposed_action: Mapped[str | None]
    status: Mapped[str] = mapped_column(server_default="open")
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("app.users.id"))
    decided_at: Mapped[datetime | None]
    decision_note: Mapped[str | None]


class AuditLog(CreatedAtMixin, Base):
    """Insert-only: a DB trigger rejects UPDATE, DELETE and TRUNCATE."""

    __tablename__ = "audit_log"
    __table_args__ = ({"schema": "app"},)

    id: Mapped[IntPk]
    actor: Mapped[str]
    action: Mapped[str]
    entity: Mapped[str]
    entity_id: Mapped[str | None]
    payload: Mapped[Json | None]


class PipelineRun(CreatedAtMixin, Base):
    __tablename__ = "pipeline_runs"
    __table_args__ = (one_of("status", RUN_STATUSES), {"schema": "app"})

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(server_default="running")
    # LLM costs are fractions of a cent, so this is the one non-(12,2) EUR column.
    cost_eur: Mapped[Decimal] = mapped_column(Numeric(12, 6), server_default="0")
    tokens: Mapped[int] = mapped_column(BigInteger, server_default="0")
    details: Mapped[Json | None]
