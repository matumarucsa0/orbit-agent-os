"""OWNER_NAME: the prompts name the owner from the setting, with a neutral default."""

from __future__ import annotations

import re
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from orbit.integrations.claude import (
    BRIEF_SYSTEM,
    CAPTURE_SYSTEM,
    CLASSIFY_SYSTEM,
    DEFAULT_OWNER,
    DRAFT_SYSTEM,
    LINK_SYSTEM,
    REPORT_SYSTEM,
    REVIEW_SYSTEM,
    SCHOOL_READ_SYSTEM,
    BriefRequest,
    CandidateCourse,
    CandidateGoal,
    CandidatePerson,
    CaptureExtraction,
    CaptureRequest,
    ClaudeClient,
    DraftRequest,
    MeetingLinkRequest,
    ReportRequest,
    ReviewRequest,
    SchoolReadRequest,
    brief_spec,
    build_capture_prompt,
    build_draft_prompt,
    capture_spec,
    classification_spec,
    draft_spec,
    link_spec,
    render_prompt,
    report_system,
    review_system,
    school_read_spec,
    with_owner,
)
from orbit.settings import Settings

TODAY = date(2026, 9, 23)
SYSTEMS = [
    CAPTURE_SYSTEM,
    CLASSIFY_SYSTEM,
    LINK_SYSTEM,
    BRIEF_SYSTEM,
    DRAFT_SYSTEM,
    REVIEW_SYSTEM,
    REPORT_SYSTEM,
    SCHOOL_READ_SYSTEM,
]


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, password_hash: str) -> pytest.MonkeyPatch:
    monkeypatch.setenv("ORBIT_API_TOKEN", "a" * 40)
    monkeypatch.setenv("ORBIT_WEB_PASSWORD_HASH", password_hash)
    monkeypatch.setenv("ORBIT_SECRET_KEY", "b" * 40)
    monkeypatch.setenv("ICS_TOKEN", "c" * 40)
    monkeypatch.delenv("OWNER_NAME", raising=False)
    return monkeypatch


def test_the_setting_defaults_to_a_neutral_name(env: pytest.MonkeyPatch) -> None:
    assert Settings(_env_file=None).owner_name == "the owner"  # type: ignore[call-arg]
    env.setenv("OWNER_NAME", "Alex")
    assert Settings(_env_file=None).owner_name == "Alex"  # type: ignore[call-arg]
    env.setenv("OWNER_NAME", "")  # empty means unset
    assert Settings(_env_file=None).owner_name == "the owner"  # type: ignore[call-arg]


def test_render_fills_both_spellings_and_falls_back() -> None:
    text = "{Owner} (the user) asked. Notes of {owner}."
    assert render_prompt(text, "alex") == "Alex (the user) asked. Notes of alex."
    assert render_prompt(text, "  ") == "The owner (the user) asked. Notes of the owner."
    assert DEFAULT_OWNER == "the owner"


@pytest.mark.parametrize("system", SYSTEMS)
def test_every_system_prompt_names_the_owner_by_placeholder(system: str) -> None:
    assert "{owner}" in system or "{Owner}" in system
    rendered = render_prompt(
        system.replace("{currency}", "EUR").replace("{shared_min}", "15"), "Alex"
    )
    assert "{owner}" not in rendered and "{Owner}" not in rendered and "Alex" in rendered


def capture(owner: str | None = None) -> CaptureRequest:
    request = CaptureRequest(
        "kávu s Paťom",
        TODAY,
        (CandidatePerson(1, "Pat Example", ()),),
        "EUR",
        goals=(CandidateGoal(1, 1, "Run", None),),
        courses=(CandidateCourse("NMAT101", "Linear Algebra 1"),),
        corrections=("use first names",),
    )
    return request if owner is None else with_owner(request, owner)


def test_the_capture_prompt_and_system_use_the_name() -> None:
    plain = build_capture_prompt(capture())
    assert "The owner's active goals" in plain and "Corrections the owner made" in plain
    named = capture("Alex")
    assert "Alex's active goals" in build_capture_prompt(named)
    assert "Alex's school courses" in build_capture_prompt(named)
    assert "from Alex (the user" in capture_spec(named).system
    assert "from the owner (the user" in capture_spec(capture()).system
    assert "{owner}" not in capture_spec(named).system


def test_the_other_specs_use_the_name() -> None:
    specs = [
        link_spec(MeetingLinkRequest("Kafe", None, (), owner="Alex")),
        brief_spec(BriefRequest("Kafe", TODAY, (), owner="Alex")),
        draft_spec(DraftRequest("birthday", "Pat", "sk", TODAY, TODAY, owner="Alex")),
        school_read_spec(SchoolReadRequest(TODAY, "text", False, "Page", owner="Alex")),
        classification_spec([], "15", "Alex"),
    ]
    for spec in specs:
        assert "Alex" in spec.system and "{owner}" not in spec.system


def test_the_draft_prompt_names_the_owner_in_the_style_line() -> None:
    request = DraftRequest("birthday", "Pat", "sk", TODAY, TODAY, style="short", owner="Alex")
    assert "Style wishes from Alex: short" in build_draft_prompt(request)


def test_review_and_report_systems() -> None:
    review = ReviewRequest(TODAY, (TODAY, TODAY), (TODAY, TODAY), "{}", "{}", owner="Alex")
    report = ReportRequest(TODAY, TODAY, TODAY, "{}", owner="Alex")
    assert "weekly review for Alex" in review_system(review)
    assert "report for Alex" in report_system(report)
    assert "weekly review for the owner" in review_system(with_owner(review, DEFAULT_OWNER))


@pytest.mark.parametrize("system", SYSTEMS)
def test_system_prompts_do_not_assume_a_gender(system: str) -> None:
    assert not re.search(r"\b(he|his|him|himself)\b", system, re.IGNORECASE)


def test_the_api_client_puts_its_owner_into_what_it_sends() -> None:
    calls: list[dict[str, Any]] = []

    def parse(**kwargs: Any) -> Any:
        calls.append(kwargs)
        empty = CaptureExtraction(
            notes=[], interactions=[], debts=[], promises=[], unknown_people=[], confidence=0.9
        )
        return SimpleNamespace(parsed_output=empty, stop_reason="end_turn")

    sdk = SimpleNamespace(messages=SimpleNamespace(parse=parse), close=lambda: None)
    client = ClaudeClient("key", "m", client=sdk, owner_name="Alex")  # type: ignore[arg-type]
    client.extract_capture(capture())
    assert "from Alex (the user" in str(calls[0]["system"])
    assert "Alex's active goals" in calls[0]["messages"][0]["content"]
