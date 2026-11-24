"""A prompt's text may only change together with its version (W7).

`tests/prompt_hashes.json` holds the hash and version of every prompt as of the last snapshot. If
you changed a prompt in orbit/integrations/claude.py:

1. raise its number in orbit/integrations/prompts_registry.py,
2. add a line to the changelog in docs/PROMPTS.md,
3. run `uv run python -m orbit.cli prompts-snapshot`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from orbit.integrations import prompts_registry as registry
from orbit.integrations.prompts_registry import PROMPTS, PromptInfo, prompt_hash, prompt_version

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = json.loads((ROOT / "tests" / "prompt_hashes.json").read_text())
HOW = (
    "Bump the version in prompts_registry.py, add a docs/PROMPTS.md line, "
    "then run: uv run python -m orbit.cli prompts-snapshot"
)


@pytest.mark.parametrize("info", PROMPTS, ids=lambda p: p.purpose)
def test_prompt_text_matches_the_snapshot_or_the_version_moved(info: PromptInfo) -> None:
    recorded = SNAPSHOT.get(info.purpose)
    assert recorded is not None, f"{info.purpose} is not in the snapshot. {HOW}"
    if prompt_hash(info) != recorded["sha256"]:
        assert info.version != recorded["version"], (
            f"The text of {info.purpose} changed but its version is still {info.version}. {HOW}"
        )
        pytest.fail(f"{info.purpose}@{info.version}: the snapshot is out of date. {HOW}")
    assert info.version == recorded["version"], (
        f"{info.purpose}: the version moved without a text change or without a snapshot. {HOW}"
    )


def test_snapshot_has_no_stale_entries() -> None:
    assert set(SNAPSHOT) == {p.purpose for p in PROMPTS}


def test_every_system_prompt_constant_is_registered() -> None:
    from orbit.integrations import claude

    constants = {n for n in dir(claude) if n.endswith("_SYSTEM") and n.isupper()}
    assert constants == {p.constant for p in PROMPTS}


def test_changelog_has_a_line_for_every_current_version() -> None:
    changelog = (ROOT / "docs" / "PROMPTS.md").read_text()
    for info in PROMPTS:
        assert f"`{info.purpose}@{info.version}`" in changelog, (
            f"docs/PROMPTS.md has no line for {info.purpose}@{info.version}"
        )


def test_version_labels() -> None:
    assert prompt_version("capture") == f"capture@{_version('capture')}"
    assert prompt_version("review") == prompt_version("weekly_review")  # job-queue kind
    assert prompt_version("nope") is None
    assert all(re.fullmatch(r"[a-z_]+@\d+", prompt_version(p.purpose) or "") for p in PROMPTS)


def _version(purpose: str) -> int:
    return next(p.version for p in PROMPTS if p.purpose == purpose)


def test_hash_is_of_the_text_before_rendering() -> None:
    assert len(prompt_hash(PROMPTS[0])) == 64
    text = registry.prompt_text(PROMPTS[0])
    assert "{owner}" in text or "{Owner}" in text  # the placeholder, not a rendered name


def test_the_test_catches_an_unversioned_change(monkeypatch: pytest.MonkeyPatch) -> None:
    from orbit.integrations import claude

    info = PROMPTS[0]
    monkeypatch.setattr(claude, info.constant, getattr(claude, info.constant) + " Also be rude.")
    assert prompt_hash(info) != SNAPSHOT[info.purpose]["sha256"]
    assert info.version == SNAPSHOT[info.purpose]["version"]  # no bump: the test above would fail


def test_calls_and_proposals_carry_the_version(db) -> None:  # type: ignore[no-untyped-def]
    from datetime import date

    from orbit.integrations.fake_claude import FakeClaude
    from orbit.services import capture

    log: list[object] = []
    fake = FakeClaude(on_usage=log.append)
    result = capture.capture(
        db,
        fake,
        "Lent Pato 10 euro",
        today=date(2026, 9, 23),
        default_currency="EUR",
    )
    assert {p.prompt_version for p in result.proposals} == {prompt_version("capture")}
    assert log and log[0].prompt_version == prompt_version("capture")
