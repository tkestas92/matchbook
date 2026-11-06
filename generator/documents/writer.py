"""Write all documents for a world to disk, with a manifest of content hashes."""

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from generator.documents.billing import Documents, build_documents
from generator.documents.config import DocumentsConfig
from generator.documents.excel import write_media_plan
from generator.documents.model import (
    CreditNote,
    Invoice,
    credit_note_ground_truth,
    invoice_ground_truth,
)
from generator.documents.pdf import render_credit_note, render_invoice
from generator.world import World

MANIFEST_NAME = "manifest.json"


def invoice_dir(invoice: Invoice) -> Path:
    month = f"{invoice.period_start:%Y-%m}"
    if invoice.kind == "platform":
        return Path("invoices", "platform", invoice.platform_key or "", month)
    return Path("invoices", "client", invoice.client_code, month)


def credit_note_dir(note: CreditNote) -> Path:
    return Path("credit_notes", note.platform_key, f"{note.period_start:%Y-%m}")


def plan_path(client_code: str) -> Path:
    return Path("plans", f"{client_code}_media_plan.xlsx")


def generate_documents(world: World, config: DocumentsConfig, out_dir: Path) -> Documents:
    """Replace `out_dir` with freshly rendered documents and return what was written."""
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    documents = build_documents(world, config)
    for client in world.clients:
        write_media_plan(world, client, ensure_parent(out_dir / plan_path(client.code)))
    for invoice in (*documents.platform_invoices, *documents.client_invoices):
        base = ensure_parent(out_dir / invoice_dir(invoice) / invoice.number)
        render_invoice(invoice, base.with_suffix(".pdf"), config.agency.vat_code)
        write_json(base.with_suffix(".json"), invoice_ground_truth(invoice))
    for note in documents.credit_notes:
        base = ensure_parent(out_dir / credit_note_dir(note) / note.number)
        render_credit_note(note, base.with_suffix(".pdf"))
        write_json(base.with_suffix(".json"), credit_note_ground_truth(note))
    write_json(out_dir / MANIFEST_NAME, {"seed": world.seed, "files": file_hashes(out_dir)})
    return documents


def ensure_parent(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False)
    path.write_text(text + "\n", encoding="utf-8")


def file_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != MANIFEST_NAME
    }
