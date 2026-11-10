"""Only evals/ may know about the answer key or the `eval` schema (AGENTS.md rule 8).

Application, pipeline and mock-platform code must not read, name or import it, so no module can
"find" planted errors by looking at the answers (decisions 003, 025, 033).
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GUARDED_DIRS = ("pipelines", "backend/app", "mock_platforms", "orchestration")

# The ORM definition of eval.answer_key: Alembic needs it on Base.metadata. Nothing else.
ALLOWED = {
    "backend/app/db/models/eval.py",
    "backend/app/db/models/__init__.py",
}

FORBIDDEN = {
    "answer key name": re.compile(r"answer_key|AnswerKey"),
    "eval schema": re.compile(r"\beval\.|[\"']eval[\"']"),
    "evals package": re.compile(r"\bevals\b"),
    "eval models import": re.compile(r"app\.db\.models\.eval\b|models\s+import\s+.*\beval\b"),
}


def guarded_sources() -> list[Path]:
    return sorted(
        path
        for directory in GUARDED_DIRS
        for path in (ROOT / directory).rglob("*.py")
        if path.relative_to(ROOT).as_posix() not in ALLOWED
    )


def test_guarded_directories_have_sources() -> None:
    names = {path.relative_to(ROOT).parts[0] for path in guarded_sources()}
    assert names == {"pipelines", "backend", "mock_platforms", "orchestration"}


@pytest.mark.parametrize("path", guarded_sources(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_source_never_mentions_the_answer_key_or_eval(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    hits = [name for name, pattern in FORBIDDEN.items() if pattern.search(source)]
    assert not hits, f"{path.relative_to(ROOT)} mentions {', '.join(hits)}"


def test_allowed_files_only_define_the_table() -> None:
    model = (ROOT / "backend/app/db/models/eval.py").read_text(encoding="utf-8")
    assert "select(" not in model and "execute(" not in model
