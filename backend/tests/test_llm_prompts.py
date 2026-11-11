import hashlib
from pathlib import Path

import pytest
from app.llm.prompts import PROMPTS_DIR, PromptNotFound, load_prompt


def test_prompt_text_and_hash(tmp_path: Path) -> None:
    (tmp_path / "extract_invoice_v1.md").write_text("Extract.", encoding="utf-8")
    prompt = load_prompt("extract_invoice_v1", tmp_path)
    assert prompt.text == "Extract."
    assert prompt.sha256 == hashlib.sha256(b"Extract.").hexdigest()
    assert load_prompt("extract_invoice_v1", tmp_path) == prompt


def test_changed_prompt_changes_the_hash(tmp_path: Path) -> None:
    path = tmp_path / "x_v1.md"
    path.write_text("one", encoding="utf-8")
    first = load_prompt("x_v1", tmp_path).sha256
    path.write_text("two", encoding="utf-8")
    assert load_prompt("x_v1", tmp_path).sha256 != first


@pytest.mark.parametrize(
    "prompt_id", ["../secrets_v1", "extract", "Extract_v1", "a/b_v1", "x_v1.md", "x_v1\n", ""]
)
def test_malformed_ids_are_rejected(tmp_path: Path, prompt_id: str) -> None:
    with pytest.raises(PromptNotFound, match="invalid prompt id"):
        load_prompt(prompt_id, tmp_path)


def test_missing_prompt_file(tmp_path: Path) -> None:
    with pytest.raises(PromptNotFound, match="no prompt file"):
        load_prompt("absent_v1", tmp_path)


def test_default_prompts_dir_is_inside_the_llm_package() -> None:
    assert PROMPTS_DIR.parent.name == "llm" and PROMPTS_DIR.is_dir()
