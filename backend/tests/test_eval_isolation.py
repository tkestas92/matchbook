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


# --- the public demo reads only its two views and one committed results file (decision 035) ---

DEMO_DIR = ROOT / "backend/app/demo"
DEMO_RELATIONS = {"demo.v_issues", "demo.v_pipeline_runs"}
DEMO_FORBIDDEN = {
    "raw/core/eval relation": re.compile(r"\b(raw|core|eval)\.[a-z_]+"),
    "app table": re.compile(r"\bapp\.(issues|pipeline_runs|users|user_client_scope|audit_log)\b"),
    "generated data": re.compile(r"data/generated|generated_dir|answer_key"),
    "ORM models": re.compile(r"app\.db\.models"),
    "pipeline code": re.compile(r"\b(pipelines|orchestration|mock_platforms|generator)\b"),
    "owner or other role login": re.compile(
        r"\b(get_engine|create_etl_engine|create_app_engine)\b"
    ),
}
DEMO_RELATION = re.compile(r"\bdemo\.v_[a-z_]+")
SCHEMA_ARGUMENT = re.compile(r"schema=\"([a-z_]+)\"")


def demo_sources() -> list[Path]:
    return sorted(DEMO_DIR.rglob("*.py"))


def test_demo_module_has_sources() -> None:
    assert {path.name for path in demo_sources()} >= {"app.py", "queries.py", "db.py"}


@pytest.mark.parametrize("path", demo_sources(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_demo_never_names_other_data(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    hits = [name for name, pattern in DEMO_FORBIDDEN.items() if pattern.search(source)]
    assert not hits, f"{path.relative_to(ROOT)} mentions {', '.join(hits)}"
    assert set(DEMO_RELATION.findall(source)) <= DEMO_RELATIONS
    assert set(SCHEMA_ARGUMENT.findall(source)) <= {"demo"}


def test_demo_tables_are_the_two_views() -> None:
    from app.demo import queries

    relations = {f"{t.schema}.{t.name}" for t in (queries.ISSUES, queries.PIPELINE_RUNS)}
    assert relations == {"demo.v_issues", "demo.v_pipeline_runs"}


def test_demo_reads_only_the_committed_results_file() -> None:
    from app.demo.eval_results import DEFAULT_RESULTS_PATH

    assert DEFAULT_RESULTS_PATH == ROOT / "docs" / "eval-results.md"
    file_reads = re.compile(r"\b(open|read_text|read_bytes)\(")
    readers = [
        path.name for path in demo_sources() if file_reads.search(path.read_text(encoding="utf-8"))
    ]
    assert readers == ["eval_results.py"]
