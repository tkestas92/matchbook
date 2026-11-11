"""The demo deploy files (decision 036), read statically: no Docker needed.

Checks that the public demo API binds 127.0.0.1 only and gets no DB password but `demo_ro`'s,
that the demo Postgres publishes no port, and that the demo image cannot contain `.env`,
the answer key, evals or tests.
"""

import fnmatch
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
SERVICES: dict[str, Any] = COMPOSE["services"]
DEMO_SERVICES = ("demo-api", "demo-init", "demo-postgres")
DEMO_API = SERVICES["demo-api"]
DOCKERFILE = ROOT / DEMO_API["build"]["dockerfile"]
DOCKERIGNORE = DOCKERFILE.with_name(DOCKERFILE.name + ".dockerignore")

DEMO_API_COPIES = {"pyproject.toml", "uv.lock", "backend/app", "docs/eval-results.md"}
FORBIDDEN_IN_CONTEXT = (
    ".env",
    "backend/.env",
    "data/generated/answer_key.json",
    "answer_key.json",
    "generator/answer_key.json",
    "evals/stage1.py",
    "backend/tests/test_demo_api.py",
    "backend/app/__pycache__/config.cpython-312.pyc",
)
REQUIRED_IN_CONTEXT = (
    "pyproject.toml",
    "uv.lock",
    "backend/app/demo/app.py",
    "docs/eval-results.md",
    "deploy/demo/init.sh",
)
FORBIDDEN_COPY_SOURCE = re.compile(r"\.env|answer_key|(^|/)(data|evals|tests)(/|$)")


def environment(service: dict[str, Any]) -> dict[str, str]:
    env = service.get("environment", {})
    if isinstance(env, list):
        return dict(item.split("=", 1) for item in env)
    return env


def published_ports(service: dict[str, Any]) -> list[str]:
    return [str(port) for port in service.get("ports", [])]


def test_demo_services_are_in_the_demo_profile_only() -> None:
    for name in DEMO_SERVICES:
        assert SERVICES[name]["profiles"] == ["demo"], name


def test_demo_api_binds_localhost_only() -> None:
    assert published_ports(DEMO_API) == ["127.0.0.1:${DEMO_API_PORT:-8200}:8200"]


def test_demo_postgres_publishes_no_port() -> None:
    assert "ports" not in SERVICES["demo-postgres"]


def test_every_published_port_in_compose_is_localhost() -> None:
    for name, service in SERVICES.items():
        for port in published_ports(service):
            assert port.startswith("127.0.0.1:"), f"{name} publishes {port}"


def test_demo_api_gets_only_the_demo_ro_password() -> None:
    env = environment(DEMO_API)
    assert {key for key in env if "PASSWORD" in key} == {"DEMO_RO_DB_PASSWORD"}
    assert "@" not in env["DATABASE_URL"], "DATABASE_URL must carry no credentials"
    assert "env_file" not in DEMO_API


def test_demo_api_is_read_only_with_a_tmp_tmpfs() -> None:
    assert DEMO_API["read_only"] is True
    assert "/tmp" in DEMO_API["tmpfs"]


def test_demo_init_data_tmpfs_is_writable_by_the_image_user() -> None:
    data_mounts = [m for m in SERVICES["demo-init"]["tmpfs"] if m.split(":")[0] == "/app/data"]
    assert len(data_mounts) == 1
    assert "uid=10001" in data_mounts[0]


def test_demo_api_waits_for_a_healthy_database() -> None:
    assert DEMO_API["depends_on"]["demo-postgres"]["condition"] == "service_healthy"
    assert SERVICES["demo-init"]["depends_on"]["demo-postgres"]["condition"] == "service_healthy"
    assert "healthcheck" in DEMO_API


def dockerignore_rules() -> list[tuple[bool, str]]:
    """(excluded, pattern) pairs in file order; `!` re-includes."""
    rules = []
    for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rules.append((False, line[1:]) if line.startswith("!") else (True, line))
    return rules


def rule_matches(pattern: str, path: str) -> bool:
    """A pattern matches a path or any of its parent directories (Docker semantics).

    A leading `**/` also matches at the context root.
    """
    parts = path.split("/")
    candidates = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    patterns = [pattern, pattern.removeprefix("**/")]
    return any(fnmatch.fnmatchcase(c, p) for c in candidates for p in patterns)


def excluded_from_context(path: str) -> bool:
    """Last matching rule wins, as in Docker."""
    excluded = False
    for exclude, pattern in dockerignore_rules():
        if rule_matches(pattern, path):
            excluded = exclude
    return excluded


def test_dockerignore_sits_next_to_the_compose_dockerfile() -> None:
    assert DOCKERFILE.is_file()
    assert DOCKERIGNORE.is_file()


def test_dockerignore_denies_everything_by_default() -> None:
    assert dockerignore_rules()[0] == (True, "*")


@pytest.mark.parametrize("path", FORBIDDEN_IN_CONTEXT)
def test_secrets_and_eval_data_never_enter_the_build_context(path: str) -> None:
    assert excluded_from_context(path)


@pytest.mark.parametrize("path", REQUIRED_IN_CONTEXT)
def test_what_the_images_need_is_in_the_build_context(path: str) -> None:
    assert not excluded_from_context(path)


def dockerfile_stages() -> dict[str, list[str]]:
    """Instructions per stage name, continuation lines joined."""
    text = DOCKERFILE.read_text(encoding="utf-8").replace("\\\n", " ")
    stages: dict[str, list[str]] = {}
    current: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if match := re.match(r"FROM\s+\S+\s+AS\s+(\S+)", line, re.I):
            current = stages.setdefault(match.group(1), [])
        current.append(line)
    return stages


def copy_sources(instructions: list[str]) -> list[str]:
    sources = []
    for line in instructions:
        words = line.split()
        if words[0].upper() == "COPY" and not any(w.startswith("--from") for w in words):
            sources.extend(words[1:-1])
    return sources


def test_dockerfile_never_copies_the_whole_context_or_eval_data() -> None:
    for name, instructions in dockerfile_stages().items():
        assert not any(line.split()[0].upper() == "ADD" for line in instructions), name
        for source in copy_sources(instructions):
            assert source not in {".", "./", "*"}, f"{name} copies the whole context"
            assert not FORBIDDEN_COPY_SOURCE.search(source), f"{name} copies {source}"


def test_demo_api_image_holds_only_backend_app_and_eval_results() -> None:
    stages = dockerfile_stages()
    copied = set(copy_sources(stages["base"])) | set(copy_sources(stages["demo-api"]))
    assert copied == DEMO_API_COPIES


@pytest.mark.parametrize("stage", ["demo-api", "demo-init"])
def test_final_stages_run_as_a_non_root_user(stage: str) -> None:
    users = [line.split()[1] for line in dockerfile_stages()[stage] if line.startswith("USER ")]
    assert users and users[-1] not in {"root", "0"}


def test_compose_targets_exist_in_the_dockerfile() -> None:
    stages = dockerfile_stages()
    for name in ("demo-api", "demo-init"):
        assert SERVICES[name]["build"]["target"] in stages
