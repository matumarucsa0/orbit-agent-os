"""The Claude worker's endpoints and the backend choice (SPEC §2.19)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orbit.app import create_app
from orbit.domain.enums import ReviewStatus
from orbit.domain.inbox import Notification
from orbit.domain.review import WeeklyReview
from orbit.integrations.claude import ClaudeClient
from orbit.services.claude_jobs import JobClaudeClient
from tests.conftest import AUTH, concurrent_database_url, reset_schema
from tests.test_claude_jobs import note_answer

WORKER_TOKEN = "worker-token-" + "w" * 32
WORKER = {"Authorization": f"Bearer {WORKER_TOKEN}"}
BASE = "/api/v1/claude-worker"


@pytest.fixture
def worker_app(make_settings: Any, tmp_path: Path) -> FastAPI:
    app = create_app(
        make_settings(
            database_url=concurrent_database_url(tmp_path),
            orbit_review_token=WORKER_TOKEN,
            claude_wait_interactive_seconds=10,
            weekly_review_enabled=False,  # tests that want one switch it on
        )
    )
    reset_schema(app.state.engine)
    return app


@pytest.fixture
def worker(worker_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(worker_app) as client:
        client.headers.update(WORKER)
        yield client


# --- Which backend ---


def test_the_backend_follows_the_settings(make_settings: Any) -> None:
    assert make_settings().resolved_claude_backend == "none"
    assert make_settings(orbit_review_token=WORKER_TOKEN).resolved_claude_backend == "worker"
    keyed = {"anthropic_api_key": "sk-test", "anthropic_model": "m"}
    assert make_settings(**keyed).resolved_claude_backend == "api"
    both = make_settings(orbit_review_token=WORKER_TOKEN, **keyed)
    assert both.resolved_claude_backend == "api"
    assert make_settings(claude_backend="worker", **keyed).resolved_claude_backend == "none"
    assert (
        make_settings(
            claude_backend="worker", orbit_review_token=WORKER_TOKEN, **keyed
        ).resolved_claude_backend
        == "worker"
    )
    assert make_settings(claude_backend="api").resolved_claude_backend == "none"
    assert make_settings(claude_backend="none", **keyed).resolved_claude_backend == "none"


def test_the_worker_backend_does_not_make_reviews_inside_orbit(worker_app: FastAPI) -> None:
    assert isinstance(worker_app.state.claude, JobClaudeClient)
    assert worker_app.state.reviewer is None


def test_the_api_backend_keeps_everything_as_before(make_settings: Any) -> None:
    app = create_app(make_settings(anthropic_api_key="sk-test", anthropic_model="m"))
    assert isinstance(app.state.claude, ClaudeClient)
    assert app.state.reviewer is app.state.claude


# --- Tokens ---


def test_every_endpoint_needs_a_token(client: TestClient) -> None:
    for method, path in (
        ("post", "/claim"),
        ("post", "/jobs/1/submit"),
        ("post", "/jobs/1/fail"),
        ("post", "/jobs/1/rerun"),
        ("post", "/heartbeat"),
        ("get", "/status"),
    ):
        assert getattr(client, method)(BASE + path).status_code == 401, path
        bad = getattr(client, method)(BASE + path, headers={"Authorization": "Bearer nope"})
        assert bad.status_code == 401, path


def test_the_worker_token_opens_nothing_else(worker: TestClient) -> None:
    for method, path in (("get", "/api/v1/people"), ("post", "/api/v1/capture")):
        assert getattr(worker, method)(path).status_code == 401, path


def test_the_main_token_works_here_too(worker_app: FastAPI) -> None:
    with TestClient(worker_app, headers=AUTH) as client:
        assert client.get(f"{BASE}/status").status_code == 200


# --- The endpoints ---


def test_claim_is_204_when_the_queue_is_empty_and_shows_the_worker_alive(
    worker: TestClient,
) -> None:
    assert worker.get(f"{BASE}/status").json()["alive"] is False
    assert worker.post(f"{BASE}/claim").status_code == 204
    assert worker.get(f"{BASE}/status").json()["alive"] is True


def test_heartbeat_reports_login_and_rate_limit(worker: TestClient) -> None:
    status = worker.post(
        f"{BASE}/heartbeat",
        json={"cli_version": "2.1.0", "auth_ok": False, "rate_limited_for_seconds": 600},
    ).json()
    assert status["cli_version"] == "2.1.0" and status["auth_ok"] is False
    assert status["rate_limited_until"] is not None
    healthy = worker.post(f"{BASE}/heartbeat", json={"auth_ok": True}).json()
    assert healthy["auth_ok"] is True


def test_a_capture_goes_through_the_worker_end_to_end(worker: TestClient) -> None:
    """The request answers at once with a draft; the worker claims and answers; the answer replaces
    the draft (SPEC §2.33)."""
    worker.post(f"{BASE}/heartbeat", json={"auth_ok": True})  # the worker is up
    response = worker.post("/api/v1/capture", json={"text": "Kamil má rád kávu"}, headers=AUTH)
    assert response.status_code == 200, response.text
    assert response.json()["summary"].startswith("Saved; Claude is reading it")
    draft = worker.get("/api/v1/inbox", headers=AUTH).json()
    assert draft[0]["payload"]["processing"] is True

    claimed = worker.post(f"{BASE}/claim")
    assert claimed.status_code == 200, claimed.text
    job = claimed.json()
    assert job["kind"] == "capture" and "Kamil má rád kávu" in job["prompt"]
    assert job["schema"]["type"] == "object" and job["system"]
    bad = worker.post(f"{BASE}/jobs/{job['id']}/submit", json={"output": {"nope": 1}})
    assert bad.status_code == 422 and bad.json()["detail"]
    ok = worker.post(
        f"{BASE}/jobs/{job['id']}/submit",
        json={"output": note_answer("Likes coffee"), "model": "m", "input_tokens": 5},
    )
    assert ok.status_code == 200 and ok.json()["status"] == "done"
    assert worker.post(f"{BASE}/jobs/{job['id']}/submit", json={"output": {}}).status_code == 409
    worker.post(f"{BASE}/claim")  # the next poll uses the answer
    inbox = worker.get("/api/v1/inbox", headers=AUTH).json()
    assert "Likes coffee" in str(inbox) and inbox[0]["payload"]["processing"] is False


def test_a_capture_with_the_worker_away_saves_a_note_draft_at_once(worker: TestClient) -> None:
    response = worker.post("/api/v1/capture", json={"text": "Kamil má rád kávu"}, headers=AUTH)
    assert response.status_code == 200, response.text
    assert "Kamil má rád kávu" in str(worker.get("/api/v1/inbox", headers=AUTH).json())


def test_fail_and_rerun(worker: TestClient, worker_app: FastAPI) -> None:
    from orbit.integrations.claude import DraftRequest, draft_spec
    from orbit.services import claude_jobs
    from tests.test_claude_jobs import TODAY

    sessions = worker_app.state.sessionmaker
    with sessions() as db:
        job = claude_jobs.enqueue(
            db,
            draft_spec(DraftRequest("birthday", "Paťo", "sk", TODAY, TODAY)),
            limits=claude_jobs.QueueLimits(wait_interactive=3600),
        )
        db.commit()
        job_id = job.id
    assert worker.post(f"{BASE}/claim").json()["id"] == job_id
    assert (
        worker.post(f"{BASE}/jobs/{job_id}/fail", json={"error": "x", "kind": "auth"}).json()[
            "status"
        ]
        == "failed"
    )
    assert worker.get(f"{BASE}/status").json()["auth_ok"] is False
    assert worker.post(f"{BASE}/jobs/{job_id}/fail", json={"error": "x"}).status_code == 409
    again = worker.post(f"{BASE}/jobs/{job_id}/rerun")
    assert again.status_code == 200 and again.json()["status"] == "queued"
    assert worker.post(f"{BASE}/jobs/{job_id}/rerun").status_code == 409
    assert worker.post(f"{BASE}/jobs/999/rerun").status_code == 404


# --- The ops page ---


def test_the_ops_page_shows_the_worker_and_reruns_a_failed_job(
    worker_app: FastAPI, worker: TestClient
) -> None:
    from orbit.integrations.claude import DraftRequest, draft_spec
    from orbit.services import claude_jobs
    from tests.conftest import TEST_PASSWORD
    from tests.test_claude_jobs import TODAY

    with worker_app.state.sessionmaker() as db:
        job = claude_jobs.enqueue(
            db,
            draft_spec(DraftRequest("birthday", "Paťo", "sk", TODAY, TODAY)),
            limits=claude_jobs.QueueLimits(wait_interactive=3600),
        )
        db.commit()
        job_id = job.id
    worker.post(f"{BASE}/claim")
    worker.post(f"{BASE}/jobs/{job_id}/fail", json={"error": "boom", "kind": "auth"})
    worker.headers.clear()
    assert worker.get("/ops/claude", follow_redirects=False).status_code in (303, 401)
    login = worker.post("/login", data={"password": TEST_PASSWORD}, follow_redirects=False)
    assert login.status_code == 303
    page = worker.get("/ops/claude")
    assert page.status_code == 200
    assert "boom" in page.text and "claude setup-token" in page.text and "Rerun" in page.text
    again = worker.post(f"/ops/claude/jobs/{job_id}/rerun", follow_redirects=False)
    assert again.status_code == 303
    assert "queued" in worker.get("/ops/claude").text


# --- The weekly review as a job ---

SUNDAY_EVENING = datetime(2026, 10, 4, 17, 30, tzinfo=UTC)  # 19:30 in Prague


@pytest.fixture
def review_worker(make_settings: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    app = create_app(make_settings(orbit_review_token=WORKER_TOKEN))
    reset_schema(app.state.engine)
    monkeypatch.setattr("orbit.api.claude_worker.utcnow", lambda: SUNDAY_EVENING)
    with TestClient(app, headers=WORKER) as client:
        client.app = app  # type: ignore[attr-defined]
        yield client


def test_an_idle_worker_is_handed_the_weekly_review(review_worker: TestClient) -> None:
    from orbit.integrations.claude import ReviewOutput
    from tests.test_review import empty_output

    claimed = review_worker.post(f"{BASE}/claim")
    assert claimed.status_code == 200, claimed.text
    job = claimed.json()
    assert job["kind"] == "review" and job["prompt"] and job["schema"]["type"] == "object"
    assert review_worker.post(f"{BASE}/claim").status_code == 204  # not twice
    answer = empty_output().model_dump(mode="json")
    assert ReviewOutput.model_validate(answer)
    done = review_worker.post(
        f"{BASE}/jobs/{job['id']}/submit",
        json={"output": answer, "model": "m", "input_tokens": 9, "output_tokens": 4},
    )
    assert done.status_code == 200, done.text
    app = review_worker.app  # type: ignore[attr-defined]
    with app.state.sessionmaker() as db:
        row = db.query(WeeklyReview).one()
        assert row.status is ReviewStatus.READY and row.model == "m"
        assert db.query(Notification).filter_by(kind="weekly_review").count() == 1
    assert review_worker.post(f"{BASE}/claim").status_code == 204


def test_a_failed_review_job_lets_the_schedule_try_again(review_worker: TestClient) -> None:
    app = review_worker.app  # type: ignore[attr-defined]
    first = review_worker.post(f"{BASE}/claim").json()
    review_worker.post(f"{BASE}/jobs/{first['id']}/fail", json={"error": "boom"})
    with app.state.sessionmaker() as db:
        row = db.query(WeeklyReview).one()
        assert row.status is ReviewStatus.FAILED and row.attempts == 1
    second = review_worker.post(f"{BASE}/claim")
    assert second.status_code == 200 and second.json()["id"] != first["id"]
    # A rate limit does not use up an attempt, and nothing starts while the worker is limited.
    review_worker.post(
        f"{BASE}/jobs/{second.json()['id']}/fail", json={"error": "429", "kind": "rate_limit"}
    )
    with app.state.sessionmaker() as db:
        assert db.query(WeeklyReview).one().attempts == 1
    assert review_worker.post(f"{BASE}/claim").status_code == 204
    assert review_worker.get(f"{BASE}/status").json()["rate_limited_until"] is not None
    assert review_worker.post(f"{BASE}/jobs/{first['id']}/rerun").status_code == 409


def test_a_review_waits_for_a_working_login(review_worker: TestClient) -> None:
    review_worker.post(f"{BASE}/heartbeat", json={"auth_ok": False})
    assert review_worker.post(f"{BASE}/claim").status_code == 204
    review_worker.post(f"{BASE}/heartbeat", json={"auth_ok": True})
    assert review_worker.post(f"{BASE}/claim").status_code == 200
