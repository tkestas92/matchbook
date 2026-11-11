"""Versioned prompt files. Each result carries the SHA-256 of the prompt it was made with.

A prompt id is `<name>_v<N>` and maps to `prompts/<id>.md`. The id pattern keeps lookups inside
the prompts directory. Prompts hold no secrets and no examples from generated data (T6, T7).
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from app.llm.provider import LLMError

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
PROMPT_ID = re.compile(r"^[a-z][a-z0-9_]*_v\d+$")


class PromptNotFound(LLMError):
    """The prompt id is malformed or has no file."""


@dataclass(frozen=True)
class Prompt:
    id: str
    text: str
    sha256: str


def load_prompt(prompt_id: str, directory: Path = PROMPTS_DIR) -> Prompt:
    if not PROMPT_ID.fullmatch(prompt_id):
        raise PromptNotFound(f"invalid prompt id {prompt_id!r}")
    path = directory / f"{prompt_id}.md"
    if not path.is_file():
        raise PromptNotFound(f"no prompt file for {prompt_id!r}")
    text = path.read_text(encoding="utf-8")
    return Prompt(id=prompt_id, text=text, sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())
