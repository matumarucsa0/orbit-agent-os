"""Prompt versions: one `name@N` per Claude prompt, stored on every call and every proposal.

The prompt text lives in `orbit/integrations/claude.py`. When the text of a prompt changes, bump
its number here, add a line to docs/PROMPTS.md and regenerate the snapshot:
`uv run python -m orbit.cli prompts-snapshot`. A test (tests/test_prompt_versions.py) fails when a
prompt's text hash differs from `tests/prompt_hashes.json` without a version bump.

This module must not import `claude.py` at module level (claude.py imports it).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class PromptInfo:
    purpose: str  # the `purpose` recorded on ClaudeCall
    constant: str  # the name of the text constant in orbit.integrations.claude
    version: int


PROMPTS: tuple[PromptInfo, ...] = (
    PromptInfo("capture", "CAPTURE_SYSTEM", 1),
    PromptInfo("classification", "CLASSIFY_SYSTEM", 1),
    PromptInfo("meeting_link", "LINK_SYSTEM", 1),
    PromptInfo("brief", "BRIEF_SYSTEM", 1),
    PromptInfo("draft", "DRAFT_SYSTEM", 1),
    PromptInfo("weekly_review", "REVIEW_SYSTEM", 1),
    PromptInfo("report", "REPORT_SYSTEM", 1),
    PromptInfo("school_read", "SCHOOL_READ_SYSTEM", 1),
)

_BY_PURPOSE = {p.purpose: p for p in PROMPTS}
# Job-queue kinds that are named differently from the purpose of their call.
_ALIASES = {"review": "weekly_review"}


def prompt_version(purpose: str) -> str | None:
    """`capture@3` for a known purpose, else None (the worker's job kinds map to the same names)."""
    info = _BY_PURPOSE.get(_ALIASES.get(purpose, purpose))
    return f"{info.purpose}@{info.version}" if info else None


def prompt_text(info: PromptInfo) -> str:
    from orbit.integrations import claude

    text: str = getattr(claude, info.constant)
    return text


def prompt_hash(info: PromptInfo) -> str:
    return hashlib.sha256(prompt_text(info).encode()).hexdigest()


def current_snapshot() -> dict[str, dict[str, object]]:
    return {p.purpose: {"version": p.version, "sha256": prompt_hash(p)} for p in PROMPTS}
