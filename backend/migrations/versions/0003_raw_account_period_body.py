"""Raw responses: per-account snapshot key and the exact response bytes.

Design: decision 027.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RAW_TABLES = ("meta_responses", "google_responses", "tiktok_responses")
SNAPSHOT_KEY = ["account_id", "period_start", "period_end", "page"]


def snapshot_constraint(table: str) -> str:
    return f"uq_{table}_{'_'.join(SNAPSHOT_KEY)}"


def upgrade() -> None:
    for table in RAW_TABLES:
        op.add_column(table, sa.Column("account_id", sa.Text(), nullable=False), schema="raw")
        op.add_column(table, sa.Column("period_start", sa.Date(), nullable=False), schema="raw")
        op.add_column(table, sa.Column("period_end", sa.Date(), nullable=False), schema="raw")
        op.add_column(table, sa.Column("body", sa.LargeBinary(), nullable=False), schema="raw")
        op.drop_constraint(f"uq_{table}_run_id_page", table, schema="raw", type_="unique")
        op.create_unique_constraint(snapshot_constraint(table), table, SNAPSHOT_KEY, schema="raw")


def downgrade() -> None:
    for table in RAW_TABLES:
        op.drop_constraint(snapshot_constraint(table), table, schema="raw", type_="unique")
        op.create_unique_constraint(
            f"uq_{table}_run_id_page", table, ["run_id", "page"], schema="raw"
        )
        op.drop_column(table, "body", schema="raw")
        op.drop_column(table, "period_end", schema="raw")
        op.drop_column(table, "period_start", schema="raw")
        op.drop_column(table, "account_id", schema="raw")
