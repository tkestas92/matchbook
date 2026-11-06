"""Generate the synthetic data: true world -> clean documents -> planted errors -> files.

Run: uv run python -m generator   (or `make generate`)
"""

from collections import Counter

from generator.documents.billing import build_documents
from generator.documents.config import load_documents_config
from generator.documents.writer import DEFAULT_OUTPUT_DIR
from generator.errors import generate_with_errors, load_errors_config
from generator.world import build_world, load_config


def main() -> None:
    world = build_world(load_config())
    docs_config = load_documents_config()
    planted = generate_with_errors(
        world,
        build_documents(world, docs_config),
        docs_config,
        load_errors_config(),
        DEFAULT_OUTPUT_DIR,
    )
    counts = Counter(entry.error_type for entry in planted.answer_key)
    print(
        f"platform_invoices={len(planted.documents.platform_invoices)} "
        f"client_invoices={len(planted.documents.client_invoices)} "
        f"credit_notes={len(planted.documents.credit_notes)} "
        f"delivery_rows={len(planted.delivery)} -> {DEFAULT_OUTPUT_DIR}"
    )
    print(
        f"planted errors={len(planted.answer_key)}: "
        + ", ".join(f"{slug}={count}" for slug, count in sorted(counts.items()))
    )


if __name__ == "__main__":
    main()
