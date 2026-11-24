"""scripts/claude_worker.py against a fake `claude` and a real Orbit (SPEC §2.19)."""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI

from orbit.app import create_app
from orbit.domain.base import utcnow
from orbit.domain.claude_job import ClaudeJob
from orbit.domain.enums import JobStatus
from orbit.integrations.claude import DraftRequest, draft_spec
from orbit.services import claude_jobs
from tests.conftest import concurrent_database_url, reset_schema
from tests.test_claude_jobs import TODAY

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "claude_worker.py"
TOKEN = "worker-token-" + "w" * 32

FAKE_CLAUDE = """\
#!{python}
import json, sys
from pathlib import Path

here = Path(__file__).parent
if "--version" in sys.argv:
    print("9.9.9 (Claude Code)")
    raise SystemExit(0)
mode = (here / "mode").read_text().strip()
calls = here / "calls"
count = int(calls.read_text()) + 1 if calls.exists() else 1
calls.write_text(str(count))
schema = json.loads(sys.argv[sys.argv.index("--json-schema") + 1])
(here / "args.json").write_text(json.dumps(sys.argv[1:]))
(here / "stdin.txt").write_text(sys.stdin.read())
if mode == "auth":
    print(json.dumps({{"is_error": True, "result": "Invalid API key · Please run /login"}}))
    raise SystemExit(1)
if mode == "rate":
    print(json.dumps({{"is_error": True, "result": "5-hour limit reached"}}))
    raise SystemExit(1)
if mode == "bad_then_ok" and count == 1:
    out = {{"wrong": 1}}
elif "ok" in schema.get("properties", {{}}):
    out = {{"ok": True}}
else:
    out = {{"text": "Všetko najlepšie!"}}
print(json.dumps({{
    "is_error": False, "structured_output": out,
    "usage": {{"input_tokens": 100, "cache_read_input_tokens": 50, "output_tokens": 20}},
    "modelUsage": {{"claude-test-1": {{}}}},
}}))
"""


def load_script() -> Any:
    spec = importlib.util.spec_from_file_location("claude_worker", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["claude_worker"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def server(make_settings: Any, tmp_path: Path) -> Iterator[tuple[FastAPI, str]]:
    app = create_app(
        make_settings(orbit_review_token=TOKEN, database_url=concurrent_database_url(tmp_path))
    )
    reset_schema(app.state.engine)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    uvi = uvicorn.Server(config)
    thread = threading.Thread(target=uvi.run, daemon=True)
    thread.start()
    for _ in range(100):
        if uvi.started:
            break
        time.sleep(0.05)
    assert uvi.started
    yield app, f"http://127.0.0.1:{port}"
    uvi.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def fake(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    claude = bin_dir / "claude"
    claude.write_text(FAKE_CLAUDE.format(python=sys.executable))
    claude.chmod(0o755)
    (bin_dir / "mode").write_text("ok")
    return claude


def run_worker(url: str, claude: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "ORBIT_REVIEW_ENV": "/nonexistent",
        "ORBIT_URL": url,
        "ORBIT_REVIEW_TOKEN": TOKEN,
        "CLAUDE_BIN": str(claude),
        "CLAUDE_TIMEOUT": "30",
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def add_job(app: FastAPI, name: str = "Paťo") -> int:
    with app.state.sessionmaker() as db:
        job = claude_jobs.enqueue(
            db,
            draft_spec(DraftRequest("birthday", name, "sk", TODAY, TODAY)),
            limits=claude_jobs.QueueLimits(wait_interactive=3600),
        )
        db.commit()
        return job.id


def job_of(app: FastAPI, job_id: int) -> ClaudeJob:
    with app.state.sessionmaker() as db:
        return claude_jobs.get_job(db, job_id)


# --- Unit ---


def test_env_file_parsing(tmp_path: Path) -> None:
    module = load_script()
    path = tmp_path / "env"
    path.write_text("# c\nORBIT_URL=https://o.test\nexport A='b c'\nB=\"d\"\nEMPTY=\nbad line\n")
    assert module.parse_env_file(path) == {
        "ORBIT_URL": "https://o.test",
        "A": "b c",
        "B": "d",
        "EMPTY": "",
    }
    assert module.parse_env_file(tmp_path / "missing") == {}


def test_failures_are_told_apart() -> None:
    module = load_script()
    assert module.classify_failure("Invalid API key · Please run /login") == "auth"
    assert module.classify_failure("Claude AI usage limit reached") == "rate_limit"
    assert module.classify_failure("5-hour limit reached") == "rate_limit"
    assert module.classify_failure("segfault") == "other"


# --- With a real Orbit ---


def test_the_worker_answers_a_job(server: tuple[FastAPI, str], fake: Path) -> None:
    app, url = server
    job_id = add_job(app)
    done = run_worker(url, fake, "--once")
    assert done.returncode == 0, done.stderr
    job = job_of(app, job_id)
    assert job.status is JobStatus.DONE and job.result_json == {"text": "Všetko najlepšie!"}
    assert (job.model, job.input_tokens, job.output_tokens) == ("claude-test-1", 150, 20)
    # Claude Code was run with no tools, from the job's own system prompt and schema.
    args = json.loads((fake.parent / "args.json").read_text())
    assert args[args.index("--tools") + 1] == ""
    assert "--no-session-persistence" in args and "--system-prompt" in args
    assert "Paťo" in (fake.parent / "stdin.txt").read_text()
    with app.state.sessionmaker() as db:
        assert claude_jobs.worker_row(db).last_seen_at is not None


def test_an_empty_queue_is_fine(server: tuple[FastAPI, str], fake: Path) -> None:
    assert run_worker(server[1], fake, "--once").returncode == 0


def test_a_rejected_answer_gets_one_more_try(server: tuple[FastAPI, str], fake: Path) -> None:
    app, url = server
    (fake.parent / "mode").write_text("bad_then_ok")
    job_id = add_job(app)
    assert run_worker(url, fake, "--once").returncode == 0
    assert job_of(app, job_id).status is JobStatus.DONE
    assert (fake.parent / "calls").read_text() == "2"
    assert "was rejected" in (fake.parent / "stdin.txt").read_text()


def test_a_bad_login_fails_the_job_and_flags_the_worker(
    server: tuple[FastAPI, str], fake: Path
) -> None:
    app, url = server
    (fake.parent / "mode").write_text("auth")
    job_id = add_job(app)
    run_worker(url, fake, "--once")
    job = job_of(app, job_id)
    assert job.status is JobStatus.FAILED and "login" in (job.error or "")
    with app.state.sessionmaker() as db:
        assert claude_jobs.worker_row(db).auth_ok is False


def test_a_rate_limit_puts_the_job_back_and_pauses(server: tuple[FastAPI, str], fake: Path) -> None:
    app, url = server
    (fake.parent / "mode").write_text("rate")
    job_id = add_job(app)
    run_worker(url, fake, "--once")
    assert job_of(app, job_id).status is JobStatus.QUEUED
    with app.state.sessionmaker() as db:
        until = claude_jobs.worker_row(db).rate_limited_until
        assert until is not None and until > utcnow() + timedelta(minutes=10)


def test_selftest(server: tuple[FastAPI, str], fake: Path) -> None:
    done = run_worker(server[1], fake, "--selftest")
    assert done.returncode == 0 and done.stdout.strip() == "ok"
    (fake.parent / "mode").write_text("auth")
    failed = run_worker(server[1], fake, "--selftest")
    assert failed.returncode == 1 and "auth" in failed.stdout


# --- The reserved slot for captures and drafts (SPEC §2.34) ---


def add_background_job(app: FastAPI) -> int:
    from orbit.integrations.claude import BriefRequest, brief_spec

    with app.state.sessionmaker() as db:
        job = claude_jobs.enqueue(
            db,
            brief_spec(BriefRequest(meeting="kafe", today=TODAY, people=())),
            limits=claude_jobs.QueueLimits(wait_background=3600),
        )
        db.commit()
        return job.id


def test_the_reserved_slot_takes_only_jobs_somebody_waits_for(
    server: tuple[FastAPI, str],
) -> None:
    app, _ = server
    background = add_background_job(app)
    with app.state.sessionmaker() as db:
        assert claude_jobs.claim(db, interactive_only=True) is None
        interactive = claude_jobs.enqueue(
            db,
            draft_spec(DraftRequest("birthday", "Paťo", "sk", TODAY, TODAY)),
            limits=claude_jobs.QueueLimits(wait_interactive=3600),
        )
        db.flush()
        taken = claude_jobs.claim(db, interactive_only=True)
        assert taken is not None and taken.id == interactive.id
        assert claude_jobs.claim(db, interactive_only=True) is None
        any_job = claude_jobs.claim(db)  # the other loop still gets the background job
        assert any_job is not None and any_job.id == background


def test_the_claim_endpoint_has_the_reserved_slot(server: tuple[FastAPI, str]) -> None:
    import urllib.request

    app, url = server
    add_background_job(app)

    def claim(query: str) -> int:
        request = urllib.request.Request(
            f"{url}/api/v1/claude-worker/claim{query}",
            method="POST",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return int(response.status)

    assert claim("?interactive_only=true") == 204
    assert claim("") == 200


class RecordingOrbit:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def call(self, method: str, path: str, body: Any = None, timeout: float = 60) -> Any:
        self.calls.append(path)
        return 204, None

    def heartbeat(self, **_: Any) -> None:
        pass


def test_a_step_asks_for_the_reserved_slot_when_told_to() -> None:
    module = load_script()
    orbit = RecordingOrbit()
    worker = module.Worker(module.Config(url="http://x", token="t"), orbit)
    worker.step(0)
    worker.step(0, interactive_only=True)
    assert orbit.calls == ["/claim?wait=0", "/claim?wait=0&interactive_only=true"]


def test_run_starts_a_second_loop_unless_it_is_turned_off() -> None:
    module = load_script()
    for slot, expected in ((True, [False, True]), (False, [False])):
        config = module.Config(url="http://x", token="t", interactive_slot=slot)
        worker = module.Worker(config, RecordingOrbit())
        started: list[bool] = []
        worker.check_health = lambda force=False: None
        worker.loop = lambda interactive_only=False, seen=started: seen.append(interactive_only)
        worker.run()
        assert sorted(started) == expected


def test_the_slot_setting_reads_the_env_file(tmp_path: Path, monkeypatch: Any) -> None:
    module = load_script()
    path = tmp_path / "env"
    for value, expected in (("0", False), ("off", False), ("1", True), ("", True)):
        path.write_text(
            f"ORBIT_URL=https://o.test\nORBIT_REVIEW_TOKEN=t\nWORKER_INTERACTIVE_SLOT={value}\n"
        )
        monkeypatch.setenv("ORBIT_REVIEW_ENV", str(path))
        monkeypatch.delenv("WORKER_INTERACTIVE_SLOT", raising=False)
        assert module.Config.load().interactive_slot is expected
