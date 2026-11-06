"""Render documents from the true world into data/generated/.

Run: uv run python -m generator.documents
"""

from generator.documents.config import load_documents_config
from generator.documents.writer import DEFAULT_OUTPUT_DIR as OUTPUT_DIR
from generator.documents.writer import generate_documents
from generator.world import build_world, load_config


def main() -> None:
    world = build_world(load_config())
    documents = generate_documents(world, load_documents_config(), OUTPUT_DIR)
    print(
        f"plans={len(world.clients)} platform_invoices={len(documents.platform_invoices)} "
        f"client_invoices={len(documents.client_invoices)} "
        f"credit_notes={len(documents.credit_notes)} -> {OUTPUT_DIR}"
    )


if __name__ == "__main__":
    main()
