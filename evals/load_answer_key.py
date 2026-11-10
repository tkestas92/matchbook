"""Load data/generated/answer_key.json into eval.answer_key (`make eval-load-key`).

Run by the owner: only the database owner (`DATABASE_URL`) may write `eval`; `eval_runner` reads
it and no other role can see it (decisions 004, 025, 033). The table is replaced in one
transaction, so a re-run never duplicates rows and a bad file leaves the old key in place.
Prints counts only, so the planted entities never reach a log.
"""

import argparse
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from app.config import get_settings
from app.db.models.eval import AnswerKey
from sqlalchemy import Engine, create_engine, delete, insert

from evals.answer_key import AnswerKeyError, AnswerKeyFile, read_answer_key, to_row

DEFAULT_PATH = Path("data/generated/answer_key.json")


def load_answer_key(engine: Engine, key: AnswerKeyFile) -> int:
    rows = [to_row(entry, key.seed) for entry in key.errors]
    with engine.begin() as conn:
        conn.execute(delete(AnswerKey))
        if rows:
            conn.execute(insert(AnswerKey), rows)
    return len(rows)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load the answer key into eval.answer_key.")
    parser.add_argument("--path", type=Path, default=DEFAULT_PATH)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        key = read_answer_key(args.path)
    except (OSError, AnswerKeyError) as exc:
        print(f"cannot load the answer key: {exc}", file=sys.stderr)
        return 2
    engine = create_engine(get_settings().database_url)
    try:
        loaded = load_answer_key(engine, key)
    finally:
        engine.dispose()
    for error_type, count in sorted(Counter(e.error_type for e in key.errors).items()):
        print(f"{error_type:<24} {count:>4}")
    print(f"loaded {loaded} rows (seed {key.seed}) into eval.answer_key")
    return 0


if __name__ == "__main__":
    sys.exit(main())
