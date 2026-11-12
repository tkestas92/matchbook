"""Invoices and credit notes: SHA-256 of the PDF they were extracted from.

The LLM invoice source skips a file whose bytes are already loaded, so the same PDF is never
extracted or paid for twice. Ground-truth loading leaves the column NULL. Roles, grants and
RLS are unchanged: grants are per table, so the new columns are covered. Design: decision 045.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("invoices", "credit_notes")


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("file_sha256", sa.String(64), nullable=True), schema="core")
        op.create_index(f"ix_{table}_file_sha256", table, ["file_sha256"], schema="core")


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_index(f"ix_{table}_file_sha256", table_name=table, schema="core")
        op.drop_column(table, "file_sha256", schema="core")
