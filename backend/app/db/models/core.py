from datetime import date
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
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

BILLING_MODELS = ("markup", "mgmt_fee", "client_owned")
RATE_TYPES = ("cpm", "cpc", "flat")
ALIAS_SOURCES = ("rule", "ai", "human")
INVOICE_KINDS = ("platform", "client")
INVOICE_STATUSES = ("received", "extracted", "needs_review", "approved")
RECONCILIATION_STATUSES = ("ok", "issue", "incomplete")

Percent = Numeric(5, 2)
Confidence = Numeric(4, 3)


class Client(CreatedAtMixin, UpdatedAtMixin, Base):
    __tablename__ = "clients"
    __table_args__ = (
        one_of("billing_model", BILLING_MODELS),
        CheckConstraint(
            "billing_model <> 'markup' OR markup_pct IS NOT NULL",
            name="markup_requires_pct",
        ),
        CheckConstraint(
            "billing_model <> 'mgmt_fee' OR mgmt_fee IS NOT NULL",
            name="mgmt_fee_requires_amount",
        ),
        {"schema": "core"},
    )

    id: Mapped[IntPk]
    name: Mapped[str] = mapped_column(unique=True)
    industry: Mapped[str]
    billing_model: Mapped[str]
    markup_pct: Mapped[Decimal | None] = mapped_column(Percent)
    mgmt_fee: Mapped[Decimal | None]


class Platform(CreatedAtMixin, UpdatedAtMixin, Base):
    __tablename__ = "platforms"
    __table_args__ = ({"schema": "core"},)

    id: Mapped[IntPk]
    name: Mapped[str] = mapped_column(unique=True)
    timezone: Mapped[str]
    metric_map: Mapped[Json]


class AdAccount(RunIdMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    __tablename__ = "ad_accounts"
    __table_args__ = (
        UniqueConstraint("platform_id", "external_account_id"),
        {"schema": "core"},
    )

    id: Mapped[IntPk]
    client_id: Mapped[int] = mapped_column(ForeignKey("core.clients.id"))
    platform_id: Mapped[int] = mapped_column(ForeignKey("core.platforms.id"))
    external_account_id: Mapped[str]
    timezone: Mapped[str]


class InsertionOrder(RunIdMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    __tablename__ = "insertion_orders"
    __table_args__ = (
        UniqueConstraint("client_id", "platform_id", "campaign_ref", "start_date"),
        one_of("rate_type", RATE_TYPES),
        {"schema": "core"},
    )

    id: Mapped[IntPk]
    client_id: Mapped[int] = mapped_column(ForeignKey("core.clients.id"))
    platform_id: Mapped[int] = mapped_column(ForeignKey("core.platforms.id"))
    campaign_ref: Mapped[str]
    start_date: Mapped[date]
    end_date: Mapped[date]
    budget: Mapped[Decimal]
    rate_type: Mapped[str | None]


class Campaign(RunIdMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        UniqueConstraint("platform_id", "platform_campaign_id"),
        {"schema": "core"},
    )

    id: Mapped[IntPk]
    platform_id: Mapped[int] = mapped_column(ForeignKey("core.platforms.id"))
    ad_account_id: Mapped[int] = mapped_column(ForeignKey("core.ad_accounts.id"))
    platform_campaign_id: Mapped[str]
    name: Mapped[str]
    io_id: Mapped[int | None] = mapped_column(ForeignKey("core.insertion_orders.id"))


class CampaignAlias(RunIdMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    __tablename__ = "campaign_aliases"
    __table_args__ = (one_of("source", ALIAS_SOURCES), {"schema": "core"})

    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("core.campaigns.id"), primary_key=True
    )
    alias: Mapped[str] = mapped_column(primary_key=True)
    source: Mapped[str]
    confidence: Mapped[Decimal | None] = mapped_column(Confidence)


class DailyDelivery(RunIdMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    """Delivery per campaign per day; `date` is as reported by the platform.

    No non-negative CHECKs: bad values must reach the quality checks, not crash ingest.
    """

    __tablename__ = "daily_delivery"
    __table_args__ = (
        UniqueConstraint("campaign_id", "date"),
        Index(None, "date"),
        {"schema": "core"},
    )

    id: Mapped[IntPk]
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("core.campaigns.id", ondelete="RESTRICT")
    )
    date: Mapped[date]
    spend: Mapped[Decimal]
    impressions: Mapped[int] = mapped_column(BigInteger)
    clicks: Mapped[int] = mapped_column(BigInteger)
    conversions: Mapped[int] = mapped_column(BigInteger)


class Invoice(RunIdMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    """Platform → agency (`platform`) or agency → client (`client`) invoice."""

    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("vendor", "number"),
        Index(None, "period_start", "period_end"),
        one_of("kind", INVOICE_KINDS),
        one_of("status", INVOICE_STATUSES),
        CheckConstraint(
            "(kind = 'platform' AND platform_id IS NOT NULL)"
            " OR (kind = 'client' AND client_id IS NOT NULL)",
            name="kind_requires_party",
        ),
        {"schema": "core"},
    )

    id: Mapped[IntPk]
    kind: Mapped[str]
    vendor: Mapped[str]
    number: Mapped[str]
    client_id: Mapped[int | None] = mapped_column(
        ForeignKey("core.clients.id", ondelete="RESTRICT")
    )
    platform_id: Mapped[int | None] = mapped_column(
        ForeignKey("core.platforms.id", ondelete="RESTRICT")
    )
    issue_date: Mapped[date | None]
    period_start: Mapped[date]
    period_end: Mapped[date]
    subtotal: Mapped[Decimal | None]
    vat: Mapped[Decimal | None]
    total: Mapped[Decimal]
    file_path: Mapped[str | None]
    status: Mapped[str] = mapped_column(server_default="received")


class InvoiceLine(RunIdMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    __tablename__ = "invoice_lines"
    __table_args__ = (UniqueConstraint("invoice_id", "line_no"), {"schema": "core"})

    id: Mapped[IntPk]
    invoice_id: Mapped[int] = mapped_column(
        ForeignKey("core.invoices.id", ondelete="RESTRICT")
    )
    line_no: Mapped[int]
    campaign_ref: Mapped[str | None]
    campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("core.campaigns.id", ondelete="RESTRICT")
    )
    description: Mapped[str | None]
    amount: Mapped[Decimal]
    extracted_confidence: Mapped[Decimal | None] = mapped_column(Confidence)


class CreditNote(RunIdMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    __tablename__ = "credit_notes"
    __table_args__ = (UniqueConstraint("vendor", "number"), {"schema": "core"})

    id: Mapped[IntPk]
    original_invoice_id: Mapped[int] = mapped_column(
        ForeignKey("core.invoices.id", ondelete="RESTRICT")
    )
    vendor: Mapped[str]
    number: Mapped[str]
    issue_date: Mapped[date | None]
    amount: Mapped[Decimal]
    reason: Mapped[str | None]
    file_path: Mapped[str | None]


class ReconciliationResult(RunIdMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    """One row per client × platform × month; NULL amount = source missing."""

    __tablename__ = "reconciliation_results"
    __table_args__ = (
        UniqueConstraint("period", "client_id", "platform_id"),
        one_of("status", RECONCILIATION_STATUSES),
        {"schema": "core"},
    )

    id: Mapped[IntPk]
    period: Mapped[date]
    client_id: Mapped[int] = mapped_column(ForeignKey("core.clients.id"))
    platform_id: Mapped[int] = mapped_column(ForeignKey("core.platforms.id"))
    planned: Mapped[Decimal | None]
    delivered: Mapped[Decimal | None]
    invoiced: Mapped[Decimal | None]
    client_invoiced: Mapped[Decimal | None]
    expected_client_amount: Mapped[Decimal | None]
    variance: Mapped[Decimal | None]
    status: Mapped[str]
