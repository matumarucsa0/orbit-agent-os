"""The weekly review made by an external Claude session (SPEC §2.18): prepare, submit, fail."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from orbit.app import create_app
from orbit.domain.enums import ProposalKind, ReviewStatus
from orbit.domain.inbox import Notification, Proposal
from orbit.domain.review import WeeklyReview
from orbit.integrations.claude import ReviewOutput
from tests.conftest import AUTH, TEST_API_TOKEN, reset_schema
from tests.test_api_ledger import person
from tests.test_review import empty_output, proposals_of

REVIEW_TOKEN = "review-token-" + "r" * 32
REVIEW = {"Authorization": f"Bearer {REVIEW_TOKEN}"}
# A Sunday evening in Prague, and the Thursday after it.
SUNDAY = "2026-10-04T17:30:00+00:00"


@pytest.fixture
def agent_app(make_settings: Any) -> FastAPI:
    app = create_app(make_settings(orbit_review_token=REVIEW_TOKEN))
    reset_schema(app.state.engine)
    return app


@pytest.fixture
def agent(agent_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(agent_app) as client:
        client.headers.update(REVIEW)
        yield client


def freeze(monkeypatch: pytest.MonkeyPatch, when: str) -> None:
    moment = datetime.fromisoformat(when)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return moment if tz is None else moment.astimezone(tz)

    monkeypatch.setattr("orbit.api.review_agent.datetime", Frozen)
    assert datetime.now(UTC) is not None


def seed(agent: TestClient) -> int:
    agent.headers.update(AUTH)
    pato = person(agent, "Patrik Veselý")
    agent.post("/api/v1/goals", json={"title": "Get fit"})
    agent.post(
        "/api/v1/promises",
        json={"person_id": pato, "direction": "i_promised", "text": "send the plan"},
    )
    agent.headers.clear()
    agent.headers.update(REVIEW)
    return pato


def valid_body(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "output": empty_output().model_dump(mode="json"),
        "model": "claude-test",
        "input_tokens": 700,
        "output_tokens": 300,
    }
    body.update(changes)
    return body


# --- Tokens ---


def test_endpoints_need_a_token(client: TestClient) -> None:
    for path in ("/prepare", "/1/submit", "/1/fail"):
        assert client.post(f"/api/v1/review-agent{path}", json={}).status_code == 401
    bad = client.post("/api/v1/review-agent/prepare", headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401


def test_the_review_token_opens_nothing_else(agent: TestClient) -> None:
    for method, path in (
        ("get", "/api/v1/goals"),
        ("get", "/api/v1/people"),
        ("get", "/api/v1/reviews"),
        ("post", "/api/v1/reviews/run"),
        ("get", "/api/v1/export"),
        ("get", "/api/v1/context"),
    ):
        assert getattr(agent, method)(path).status_code == 401, path


def test_the_main_token_works_here_too(agent_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    freeze(monkeypatch, "2026-10-08T12:00:00+00:00")  # Thursday: nothing due
    with TestClient(agent_app, headers=AUTH) as client:
        assert client.post("/api/v1/review-agent/prepare").status_code == 204  # nothing due


def test_a_blank_review_token_means_none(make_settings: Any) -> None:
    assert make_settings(orbit_review_token="").orbit_review_token is None
    with pytest.raises(ValueError, match="at least"):
        make_settings(orbit_review_token="short")


def test_without_a_review_token_only_the_main_one_works(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze(monkeypatch, "2026-10-08T12:00:00+00:00")
    assert (
        client.post(
            "/api/v1/review-agent/prepare", headers={"Authorization": f"Bearer {REVIEW_TOKEN}"}
        ).status_code
        == 401
    )
    client.headers.update({"Authorization": f"Bearer {TEST_API_TOKEN}"})
    assert client.post("/api/v1/review-agent/prepare").status_code == 204


# --- Prepare ---


def test_prepare_does_nothing_outside_the_window(
    agent: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze(monkeypatch, "2026-10-08T12:00:00+00:00")  # a Thursday
    assert agent.post("/api/v1/review-agent/prepare").status_code == 204


def test_prepare_claims_the_week_and_hands_out_the_input(
    agent: TestClient, agent_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(agent)
    freeze(monkeypatch, SUNDAY)
    response = agent.post("/api/v1/review-agent/prepare")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["plan_start"] == "2026-10-05"
    assert "weekly review" in data["system"] and "the owner" in data["system"]
    assert "Get fit" in data["prompt"] and "send the plan" in data["prompt"]
    assert "Planned week: 2026-10-05 to 2026-10-11." in data["prompt"]
    assert data["schema"]["properties"]["summary"]["type"] == "string"
    assert set(data["schema"]["required"]) >= {"summary", "conflicts", "priorities", "proposals"}
    with agent_app.state.sessionmaker() as db:
        row = db.get(WeeklyReview, data["review_id"])
        assert row is not None
        assert (row.status, row.attempts) == (ReviewStatus.PENDING, 1)
        assert row.input_json is not None and row.input_chars


def test_prepare_twice_is_a_no_op(agent: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    freeze(monkeypatch, SUNDAY)
    assert agent.post("/api/v1/review-agent/prepare").status_code == 200
    assert agent.post("/api/v1/review-agent/prepare").status_code == 204  # being made right now


def test_force_makes_this_weeks_review_on_any_day(
    agent: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze(monkeypatch, "2026-10-08T12:00:00+00:00")
    forced = agent.post("/api/v1/review-agent/prepare?force=1")
    assert forced.status_code == 200
    assert forced.json()["plan_start"] == "2026-10-12"  # the week after the one in progress


# --- Submit ---


def test_submit_stores_the_review_and_makes_proposals(
    agent: TestClient, agent_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    pato = seed(agent)
    freeze(monkeypatch, SUNDAY)
    rid = agent.post("/api/v1/review-agent/prepare").json()["review_id"]
    ideas = proposals_of(
        follow_ups=[
            {
                "person_id": pato,
                "text": "Ask about the plan",
                "due": None,
                "goal_id": None,
                "reason": "r",
            },
            {"person_id": 999, "text": "ghost", "due": None, "goal_id": None, "reason": "r"},
        ]
    )
    body = valid_body(output=empty_output(proposals=ideas).model_dump(mode="json"))
    response = agent.post(f"/api/v1/review-agent/{rid}/submit", json=body)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "ready"
    assert (data["model"], data["input_tokens"], data["output_tokens"]) == ("claude-test", 700, 300)
    assert len(data["proposal_ids"]) == 1  # the ghost was dropped: not in what Claude was shown
    with agent_app.state.sessionmaker() as db:
        proposal = db.scalars(select(Proposal)).one()
        assert proposal.kind is ProposalKind.FOLLOW_UP and proposal.source_id == str(rid)
        pushes = db.scalars(select(Notification).where(Notification.kind == "weekly_review")).all()
        assert [n.dedupe_key for n in pushes] == ["weekly_review:2026-10-05"]


def test_submit_is_checked_like_an_api_answer(
    agent: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(agent)
    freeze(monkeypatch, SUNDAY)
    rid = agent.post("/api/v1/review-agent/prepare").json()["review_id"]
    bad = valid_body()
    del bad["output"]["summary"]
    refused = agent.post(f"/api/v1/review-agent/{rid}/submit", json=bad)
    assert refused.status_code == 422 and "summary" in refused.text
    assert agent.post(f"/api/v1/review-agent/{rid}/submit", json={"output": {}}).status_code == 422
    assert (
        agent.post(
            f"/api/v1/review-agent/{rid}/submit", json=valid_body(input_tokens=-1)
        ).status_code
        == 422
    )
    # the week is still waiting, so a corrected answer goes through
    assert agent.post(f"/api/v1/review-agent/{rid}/submit", json=valid_body()).status_code == 200


def test_submit_only_works_on_a_pending_review(
    agent: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze(monkeypatch, SUNDAY)
    rid = agent.post("/api/v1/review-agent/prepare").json()["review_id"]
    assert agent.post(f"/api/v1/review-agent/{rid}/submit", json=valid_body()).status_code == 200
    again = agent.post(f"/api/v1/review-agent/{rid}/submit", json=valid_body())
    assert again.status_code == 409 and "ready" in again.text
    assert agent.post("/api/v1/review-agent/999/submit", json=valid_body()).status_code == 404


def test_a_second_submit_cannot_duplicate_proposals_or_the_push(
    agent: TestClient, agent_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    pato = seed(agent)
    freeze(monkeypatch, SUNDAY)
    rid = agent.post("/api/v1/review-agent/prepare").json()["review_id"]
    follow = {"person_id": pato, "text": "Ask", "due": None, "goal_id": None, "reason": "r"}
    body = valid_body(
        output=empty_output(proposals=proposals_of(follow_ups=[follow])).model_dump(mode="json")
    )
    agent.post(f"/api/v1/review-agent/{rid}/submit", json=body)
    agent.post(f"/api/v1/review-agent/{rid}/submit", json=body)
    with agent_app.state.sessionmaker() as db:
        assert db.scalar(select(func.count(Proposal.id))) == 1
        assert db.scalar(select(func.count(Notification.id))) == 1


# --- Fail ---


def test_fail_marks_the_review_failed_and_the_timer_can_retry(
    agent: TestClient, agent_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze(monkeypatch, SUNDAY)
    rid = agent.post("/api/v1/review-agent/prepare").json()["review_id"]
    failed = agent.post(f"/api/v1/review-agent/{rid}/fail", json={"error": "Claude Code timed out"})
    assert failed.status_code == 200
    assert (failed.json()["status"], failed.json()["error"]) == ("failed", "Claude Code timed out")
    assert (
        agent.post(f"/api/v1/review-agent/{rid}/fail", json={"error": "again"}).status_code == 409
    )
    assert agent.post(f"/api/v1/review-agent/{rid}/fail", json={}).status_code in (409, 422)
    retry = agent.post("/api/v1/review-agent/prepare")  # the next hourly run
    assert retry.status_code == 200 and retry.json()["review_id"] == rid
    with agent_app.state.sessionmaker() as db:
        row = db.get(WeeklyReview, rid)
        assert row is not None and (row.status, row.attempts, row.error) == (
            ReviewStatus.PENDING,
            2,
            None,
        )


def test_the_timer_gives_up_after_the_attempt_limit(
    agent: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze(monkeypatch, SUNDAY)
    for _ in range(3):
        rid = agent.post("/api/v1/review-agent/prepare").json()["review_id"]
        agent.post(f"/api/v1/review-agent/{rid}/fail", json={"error": "x"})
    assert agent.post("/api/v1/review-agent/prepare").status_code == 204
    forced = agent.post("/api/v1/review-agent/prepare?force=1")  # a hand-run starts over
    assert forced.status_code == 200


# --- Web ---


def test_reviews_page_says_who_makes_the_review(agent_app: FastAPI) -> None:
    with TestClient(agent_app) as client:
        client.post("/login", data={"password": "correct horse battery staple"})
        page = client.get("/reviews").text
    assert "made by the Claude Code worker on the server" in page
    assert "Generate now" not in page


def test_a_failed_external_review_shows_how_to_retry(
    agent_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze(monkeypatch, SUNDAY)
    with TestClient(agent_app, headers=REVIEW) as client:
        rid = client.post("/api/v1/review-agent/prepare").json()["review_id"]
        client.post(f"/api/v1/review-agent/{rid}/fail", json={"error": "boom"})
        client.headers.clear()
        client.post("/login", data={"password": "correct horse battery staple"})
        page = client.get(f"/reviews/{rid}").text
    assert "boom" in page and "weekly-review.sh --force" in page and "Try again" not in page


def test_output_schema_is_what_the_script_is_given() -> None:
    schema = ReviewOutput.model_json_schema()
    assert "$defs" in schema and schema["type"] == "object"
