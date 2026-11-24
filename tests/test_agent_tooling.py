"""The agent harness: generated docs are current, docs have no dead paths, the personal-data
scan works and never prints what it finds, the hooks block what they should, and the Claude
config is safe."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CLAUDE = ROOT / ".claude"


def run(*args: str, stdin: str | None = None, env: dict[str, str] | None = None):
    import os

    return subprocess.run(
        list(args),
        cwd=ROOT,
        input=stdin,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        check=False,
    )


def test_generated_docs_are_current() -> None:
    result = run(sys.executable, "scripts/gen_codemap.py", "--check")
    assert result.returncode == 0, result.stderr or result.stdout


def test_docs_and_agent_config_have_no_dead_paths() -> None:
    result = run(sys.executable, "scripts/check_docs.py")
    assert result.returncode == 0, result.stdout


# --- the personal-data scan ---


def test_scan_reports_location_and_kind_but_never_the_text(tmp_path: Path) -> None:
    leak = tmp_path / "leak.txt"
    address = "someone" + "@private-mail.test"  # built here: the scan must not flag this file
    leak.write_text(f"write to {address} about zebra-quartz-word\n", encoding="utf-8")
    patterns = tmp_path / "patterns"
    patterns.write_text("# comment\nzebra-quartz-word\n", encoding="utf-8")
    result = run("scripts/scan-personal.sh", str(leak), env={"PRE_PUBLISH_PATTERNS": str(patterns)})
    assert result.returncode == 1
    assert "[e-mail]" in result.stdout and "[private pattern]" in result.stdout
    for secret in ("someone", "private-mail", "zebra", "quartz"):
        assert secret not in result.stdout + result.stderr


def test_scan_passes_on_invented_values(tmp_path: Path) -> None:
    clean = tmp_path / "clean.txt"
    clean.write_text("mail me@example.com, IBAN SK31 1200 0000 1987 4263 7541\n", encoding="utf-8")
    result = run(
        "scripts/scan-personal.sh", str(clean), env={"PRE_PUBLISH_PATTERNS": "/nonexistent"}
    )
    assert result.returncode == 0, result.stdout


def test_scan_flags_a_forbidden_file(tmp_path: Path) -> None:
    (tmp_path / "orbit.db").write_bytes(b"x")
    result = run("scripts/scan-personal.sh", str(tmp_path), env={"PRE_PUBLISH_PATTERNS": "/none"})
    assert result.returncode == 1
    assert "[forbidden file]" in result.stdout


def test_the_repository_itself_is_clean() -> None:
    result = run("scripts/scan-personal.sh", env={"PRE_PUBLISH_PATTERNS": "/nonexistent"})
    assert result.returncode == 0, result.stdout


# --- hooks ---


def hook(name: str, path: str) -> subprocess.CompletedProcess[str]:
    payload = json.dumps({"tool_input": {"file_path": str(ROOT / path)}})
    return run(str(CLAUDE / "hooks" / name), stdin=payload, env={"CLAUDE_PROJECT_DIR": str(ROOT)})


@pytest.mark.parametrize(
    "path",
    [".env", ".env.production", "orbit.db", "tests/prompt_hashes.json", ".pre-publish-patterns"],
)
def test_guard_blocks_forbidden_edits(path: str) -> None:
    result = hook("guard-edits.sh", path)
    assert result.returncode == 2, result.stderr
    assert "Blocked" in result.stderr


@pytest.mark.parametrize(
    "path", [".env.example", "orbit/db.py", "migrations/versions/9999_new_revision.py"]
)
def test_guard_allows_normal_edits(path: str) -> None:
    assert hook("guard-edits.sh", path).returncode == 0


def test_guard_blocks_an_applied_migration() -> None:
    tracked = run("git", "cat-file", "-e", "HEAD:migrations/versions/0001_baseline.py")
    if tracked.returncode != 0:
        pytest.skip("not a git checkout with history")
    assert hook("guard-edits.sh", "migrations/versions/0001_baseline.py").returncode == 2


def test_format_hook_never_fails_on_missing_files() -> None:
    assert hook("format-python.sh", "orbit/does_not_exist.py").returncode == 0


# --- config ---


def load_settings() -> dict:
    return json.loads((CLAUDE / "settings.json").read_text(encoding="utf-8"))


def test_claude_settings_default_to_fake_claude_and_a_scratch_database() -> None:
    env = load_settings()["env"]
    assert env["ORBIT_CLAUDE_MODE"] == "fake"
    assert env["SCHEDULER_ENABLED"] == "false"
    assert env["DATABASE_URL"].endswith("agent-scratch.db")
    assert not any("KEY" in name or "TOKEN" in name or "PASSWORD" in name for name in env)


def test_claude_permissions_are_least_privilege() -> None:
    permissions = load_settings()["permissions"]
    allow = " ".join(permissions["allow"])
    for dangerous in ("git push", "git commit", "rm ", "curl", "docker", "--mode live"):
        assert dangerous not in allow
    deny = permissions["deny"]
    for needed in (
        "Read(./.env)",
        "Read(**/*.db)",
        "Bash(rm -rf:*)",
        "Bash(git push --force:*)",
        "Bash(docker volume rm:*)",
        "Bash(*--mode live*)",
    ):
        assert needed in deny, needed
    # .env.example must stay readable: no deny rule may cover it
    assert not any(rule.startswith("Read(./.env*") for rule in deny)


def test_gitignore_keeps_agent_config_and_ignores_local_files() -> None:
    kept = run("git", "check-ignore", ".claude/settings.json", ".claude/hooks/stop-gate.sh")
    assert kept.stdout.strip() == ""
    ignored = run("git", "check-ignore", ".claude/settings.local.json", ".pre-publish-patterns")
    assert set(ignored.stdout.split()) == {".claude/settings.local.json", ".pre-publish-patterns"}


def test_the_network_guard_refuses_outside_hosts() -> None:
    with pytest.raises(RuntimeError, match="must not use the network"):
        socket.socket().connect(("203.0.113.7", 9))


def test_doc_only_changes_select_no_tests() -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import select_tests
    finally:
        sys.path.remove(str(ROOT / "scripts"))
    assert (
        select_tests.select(["AGENTS.md", "STATUS.md", "Makefile", ".claude/settings.json"])
        == "NONE"
    )
