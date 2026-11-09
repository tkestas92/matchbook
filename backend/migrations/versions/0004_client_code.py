"""Clients: the short code plans and invoices use to identify them.

Existing rows are matched by name to the agency registry (`reference.json`); an empty table
never reads that file. Design: decision 029.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-09
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import context, op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_REFERENCE_PATH = Path(__file__).resolve().parents[3] / "data/generated/reference.json"


def reference_codes(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise RuntimeError(
            f"core.clients has rows but {path} is missing; run `make generate` to backfill codes"
        )
    clients = json.loads(path.read_text(encoding="utf-8"))["clients"]
    return {client["name"]: client["code"] for client in clients}


def backfill_codes() -> None:
    conn = op.get_bind()
    names = list(conn.scalars(sa.text("SELECT name FROM core.clients ORDER BY name")))
    if not names:
        return
    path = Path(context.config.attributes.get("reference_path", DEFAULT_REFERENCE_PATH))
    codes = reference_codes(path)
    unmatched = [name for name in names if name not in codes]
    if unmatched:
        raise RuntimeError(f"No client code in {path} for: {', '.join(unmatched)}")
    conn.execute(
        sa.text("UPDATE core.clients SET code = :code WHERE name = :name"),
        [{"name": name, "code": codes[name]} for name in names],
    )


def upgrade() -> None:
    op.add_column("clients", sa.Column("code", sa.Text(), nullable=True), schema="core")
    backfill_codes()
    op.alter_column("clients", "code", nullable=False, schema="core")
    op.create_unique_constraint("uq_clients_code", "clients", ["code"], schema="core")


def downgrade() -> None:
    op.drop_constraint("uq_clients_code", "clients", schema="core", type_="unique")
    op.drop_column("clients", "code", schema="core")
