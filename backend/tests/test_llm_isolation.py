"""Every LLM call goes through backend/app/llm/ (AGENTS.md rule 6, threat model T7/T8).

No other code may import a provider or tracing SDK or name a provider endpoint.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCANNED_DIRS = (
    "backend/app",
    "pipelines",
    "mock_platforms",
    "orchestration",
    "generator",
    "evals",
    "mcp_server",
)
LLM_DIR = ROOT / "backend/app/llm"
# Settings hold the provider's default base URL; nothing in it calls the provider.
ENDPOINT_ALLOWED = {"backend/app/config.py"}

SDK_IMPORT = re.compile(
    r"^\s*(?:import|from)\s+(?:openai|anthropic|langfuse|google\.genai|google\.generativeai"
    r"|google\s+import\s+genai)\b",
    re.MULTILINE,
)
PROVIDER_ENDPOINT = re.compile(
    r"generativelanguage\.googleapis\.com|api\.openai\.com|api\.anthropic"
)


def scanned_sources() -> list[Path]:
    return sorted(
        path
        for directory in SCANNED_DIRS
        if (ROOT / directory).is_dir()
        for path in (ROOT / directory).rglob("*.py")
        if LLM_DIR not in path.parents
    )


def test_scan_covers_the_code_base() -> None:
    assert len(scanned_sources()) > 20


@pytest.mark.parametrize("path", scanned_sources(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_no_provider_access_outside_the_llm_layer(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    relative = path.relative_to(ROOT).as_posix()
    assert not SDK_IMPORT.search(source), f"{relative} imports an LLM SDK"
    if relative not in ENDPOINT_ALLOWED:
        assert not PROVIDER_ENDPOINT.search(source), f"{relative} names a provider URL"


@pytest.mark.parametrize(
    "line",
    [
        "import openai",
        "from anthropic import Anthropic",
        "from google import genai",
        "import google.generativeai as genai",
        "from langfuse import Langfuse",
    ],
)
def test_sdk_pattern_catches_imports(line: str) -> None:
    assert SDK_IMPORT.search(line)
