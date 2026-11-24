#!/usr/bin/env python3
"""The Claude Code worker (SPEC §2.19): answers Orbit's Claude questions with Claude Code.

Runs on the VM host (not in a container), as the unprivileged user that is logged in to Claude
Code, normally as the systemd service `orbit-claude-worker`. Standard library only.

    claude_worker.py             serve forever: claim a job, ask Claude Code, send the answer back
    claude_worker.py --once      claim at most one job (no waiting), answer it, exit
    claude_worker.py --selftest  run the real Claude Code call on a tiny question; exit 0 if ok

Every question goes to `claude -p` with **every tool switched off**, from an empty directory: the
prompts hold text other people wrote (notes, meeting titles), and a session with tools could be
talked into acting on it. Orbit checks each answer against the job's schema.

Settings come from ORBIT_REVIEW_ENV (default /etc/orbit-review.env) and the environment:
ORBIT_URL, ORBIT_REVIEW_TOKEN (the worker token), CLAUDE_CODE_OAUTH_TOKEN (from `claude
setup-token`), CLAUDE_BIN, WORKER_CLAUDE_MODEL (all jobs), WORKER_CLAUDE_MODEL_BACKGROUND (briefs,
meeting links, bank classification; a cheaper model uses less of the subscription's limits),
CLAUDE_TIMEOUT (seconds per question; by default 3 minutes for a waiting request, 5 for
background work, 25 for the weekly review), WORKER_INTERACTIVE_SLOT (default 1: a second loop
that takes only captures and drafts, so a long job never blocks them; 0 for one job at a time).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("claude-worker")

CLAIM_WAIT_SECONDS = 25
SELFTEST_EVERY_SECONDS = 6 * 3600
HEARTBEAT_EVERY_SECONDS = 30
RATE_LIMIT_DEFAULT_SECONDS = 900
INTERACTIVE_TIMEOUT_SECONDS = 180
BACKGROUND_TIMEOUT_SECONDS = 300
REVIEW_TIMEOUT_SECONDS = 1500  # Orbit's lease on a review or report job is 30 minutes
BACKGROUND_KINDS = frozenset({"brief", "meeting_link", "classification", "school_read"})
SELFTEST_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
}

AUTH_MARKERS = (
    "not logged in",
    "please run /login",
    "invalid api key",
    "authentication",
    "oauth token",
    "unauthorized",
    "credentials",
)
RATE_MARKERS = (
    "rate limit",
    "usage limit",
    "limit reached",
    "too many requests",
    "429",
    "overloaded",
)


class ClaudeFailure(Exception):
    """Claude Code gave no usable answer. `kind` is auth, rate_limit or other."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class Answer:
    output: dict[str, Any]
    model: str | None
    input_tokens: int
    output_tokens: int


@dataclass
class Config:
    url: str
    token: str
    claude_bin: str = "claude"
    model: str | None = None
    model_background: str | None = None
    timeout: int | None = None  # CLAUDE_TIMEOUT: one limit for every kind; else by kind
    # A second loop that takes only jobs somebody waits for (capture, draft), so a long school
    # read, report or review never blocks them. Two Claude Code processes can run at once.
    interactive_slot: bool = True

    @classmethod
    def load(cls) -> Config:
        values = parse_env_file(Path(os.environ.get("ORBIT_REVIEW_ENV", "/etc/orbit-review.env")))
        values.update({k: v for k, v in os.environ.items() if v})
        for key in ("ORBIT_URL", "ORBIT_REVIEW_TOKEN"):
            if not values.get(key):
                sys.exit(f"{key} is not set")
        if values.get("CLAUDE_CODE_OAUTH_TOKEN"):
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = values["CLAUDE_CODE_OAUTH_TOKEN"]
        return cls(
            url=values["ORBIT_URL"].rstrip("/"),
            token=values["ORBIT_REVIEW_TOKEN"],
            claude_bin=values.get("CLAUDE_BIN") or "claude",
            model=values.get("WORKER_CLAUDE_MODEL") or values.get("REVIEW_CLAUDE_MODEL") or None,
            model_background=values.get("WORKER_CLAUDE_MODEL_BACKGROUND") or None,
            timeout=int(values["CLAUDE_TIMEOUT"]) if values.get("CLAUDE_TIMEOUT") else None,
            interactive_slot=values.get("WORKER_INTERACTIVE_SLOT", "1").lower()
            not in ("0", "false", "no", "off"),
        )

    def timeout_for(self, kind: str) -> int:
        """Seconds one question may take: short for someone waiting, long for the review."""
        if self.timeout is not None:
            return self.timeout
        if kind in ("review", "report"):
            return REVIEW_TIMEOUT_SECONDS
        return (
            BACKGROUND_TIMEOUT_SECONDS if kind in BACKGROUND_KINDS else INTERACTIVE_TIMEOUT_SECONDS
        )

    def model_for(self, kind: str) -> str | None:
        if kind in BACKGROUND_KINDS and self.model_background:
            return self.model_background
        return self.model


def parse_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines, as the shell would read them (quotes stripped, `export` allowed)."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


# --- Orbit ---


class Orbit:
    def __init__(self, config: Config) -> None:
        self._base = f"{config.url}/api/v1/claude-worker"
        self._token = config.token

    def call(
        self, method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 60
    ) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self._base + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                **({"Content-Type": "application/json"} if data is not None else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw) if raw else None
            except ValueError:
                return exc.code, raw.decode(errors="replace")

    def heartbeat(self, **fields: Any) -> None:
        try:
            self.call("POST", "/heartbeat", fields, timeout=20)
        except (urllib.error.URLError, OSError) as exc:
            log.warning("heartbeat failed: %s", exc)


# --- Claude Code ---


def cli_version(config: Config) -> str | None:
    try:
        done = subprocess.run(
            [config.claude_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() or None


def classify_failure(text: str) -> str:
    lowered = text.lower()
    if any(marker in lowered for marker in AUTH_MARKERS):
        return "auth"
    if any(marker in lowered for marker in RATE_MARKERS):
        return "rate_limit"
    return "other"


def ask_claude(
    config: Config,
    *,
    system: str,
    prompt: str,
    schema: dict[str, Any],
    kind: str,
    extra: str = "",
) -> Answer:
    """One `claude -p` run with no tools; returns the answer object or raises ClaudeFailure."""
    command = [
        config.claude_bin,
        "-p",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema),
        "--tools",
        "",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--system-prompt",
        system,
    ]
    timeout = config.timeout_for(kind)
    model = config.model_for(kind)
    if model:
        command += ["--model", model]
    # Claude Code reads CLAUDE.md and settings from the directory it runs in: use an empty one.
    with tempfile.TemporaryDirectory(prefix="orbit-claude-") as workdir:
        try:
            done = subprocess.run(
                command,
                input=prompt + extra,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=workdir,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ClaudeFailure("other", f"Claude Code took longer than {timeout}s") from exc
        except OSError as exc:
            raise ClaudeFailure("other", f"could not run {config.claude_bin}: {exc}") from exc
    try:
        envelope = json.loads(done.stdout)
    except ValueError:
        text = (done.stderr or done.stdout).strip()[:500]
        raise ClaudeFailure(
            classify_failure(text), text or f"exit code {done.returncode}"
        ) from None
    if not isinstance(envelope, dict):
        raise ClaudeFailure("other", "unexpected output from Claude Code")
    if envelope.get("is_error"):
        text = str(envelope.get("result") or done.stderr or "Claude Code reported an error")[:500]
        raise ClaudeFailure(classify_failure(text), text)
    output = envelope.get("structured_output")
    if output is None:
        try:
            output = json.loads(str(envelope.get("result")))
        except ValueError:
            match = re.search(r"\{.*\}", str(envelope.get("result")), re.DOTALL)
            output = json.loads(match.group(0)) if match else None
    if not isinstance(output, dict):
        raise ClaudeFailure("other", "Claude Code gave no JSON object")
    usage = envelope.get("usage") or {}
    models = list((envelope.get("modelUsage") or {}).keys())
    return Answer(
        output=output,
        model=models[0] if models else None,
        input_tokens=sum(
            int(usage.get(key) or 0)
            for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        ),
        output_tokens=int(usage.get("output_tokens") or 0),
    )


def selftest(config: Config) -> tuple[bool, str]:
    """The real invocation on a tiny question. (works, what was wrong)."""
    try:
        answer = ask_claude(
            config,
            system="Answer with the JSON object only.",
            prompt='Return {"ok": true}.',
            schema=SELFTEST_SCHEMA,
            kind="selftest",
        )
    except ClaudeFailure as exc:
        return False, f"{exc.kind}: {exc}"
    return answer.output.get("ok") is True, "" if answer.output.get("ok") is True else "bad answer"


# --- The loop ---


class Worker:
    def __init__(self, config: Config, orbit: Orbit | None = None) -> None:
        self.config = config
        self.orbit = orbit or Orbit(config)
        self.stop = False
        self.rate_limited_until = 0.0
        self._next_selftest = 0.0
        self._last_heartbeat = 0.0

    def check_health(self, force: bool = False) -> None:
        """Self-test now and then; tell Orbit the CLI version, the login and the rate limit."""
        now = time.monotonic()
        if not force and now < self._next_selftest:
            return
        ok, why = selftest(self.config)
        self._next_selftest = now + SELFTEST_EVERY_SECONDS
        fields: dict[str, Any] = {"cli_version": cli_version(self.config)}
        if ok:
            fields.update(auth_ok=True, selftest_ok=True)
        else:
            log.error("self-test failed: %s", why)
            if why.startswith("auth"):
                fields["auth_ok"] = False
            elif why.startswith("rate_limit"):
                fields["rate_limited_for_seconds"] = RATE_LIMIT_DEFAULT_SECONDS
                self.rate_limited_until = time.monotonic() + RATE_LIMIT_DEFAULT_SECONDS
            self._next_selftest = now + 300  # look again soon
        self.orbit.heartbeat(**fields)
        self._last_heartbeat = now

    def process(self, job: dict[str, Any]) -> None:
        """Answer one job: ask Claude Code, submit; one retry when Orbit rejects the answer."""
        job_id, kind = job["id"], job["kind"]
        log.info("job %s (%s)", job_id, kind)
        started = time.monotonic()
        extra = ""
        for attempt in (1, 2):
            try:
                answer = ask_claude(
                    self.config,
                    system=job["system"],
                    prompt=job["prompt"],
                    schema=job["schema"],
                    kind=kind,
                    extra=extra,
                )
            except ClaudeFailure as exc:
                self.report_failure(job_id, exc)
                return
            status, reply = self.orbit.call(
                "POST",
                f"/jobs/{job_id}/submit",
                {
                    "output": answer.output,
                    "model": answer.model,
                    "input_tokens": answer.input_tokens,
                    "output_tokens": answer.output_tokens,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                },
            )
            if status == 200:
                log.info("job %s done in %.1fs", job_id, time.monotonic() - started)
                return
            if status == 422 and attempt == 1:
                detail = json.dumps(reply.get("detail") if isinstance(reply, dict) else reply)
                extra = (
                    "\n\nYour previous answer was rejected: "
                    f"{detail[:1500]}. Return a corrected object."
                )
                continue
            if status == 409:
                log.warning("job %s is no longer running: %s", job_id, reply)
                return
            self.orbit.call(
                "POST",
                f"/jobs/{job_id}/fail",
                {"error": f"Orbit answered {status}", "kind": "other"},
            )
            return

    def report_failure(self, job_id: int, failure: ClaudeFailure) -> None:
        log.error("job %s failed (%s): %s", job_id, failure.kind, failure)
        body: dict[str, Any] = {"error": str(failure), "kind": failure.kind}
        if failure.kind == "rate_limit":
            body["retry_after_seconds"] = RATE_LIMIT_DEFAULT_SECONDS
            self.rate_limited_until = time.monotonic() + RATE_LIMIT_DEFAULT_SECONDS
        self.orbit.call("POST", f"/jobs/{job_id}/fail", body)

    def step(self, wait: int, interactive_only: bool = False) -> bool:
        """Claim and answer one job; True when there was one."""
        query = f"wait={wait}" + ("&interactive_only=true" if interactive_only else "")
        status, job = self.orbit.call("POST", f"/claim?{query}", timeout=wait + 30)
        if status == 204:
            return False
        if status != 200 or not isinstance(job, dict):
            raise OSError(f"claim answered {status}")
        self.process(job)
        return True

    def loop(self, interactive_only: bool = False) -> None:
        """Claim and answer jobs until stopped. The reserved slot (`interactive_only`) takes only
        jobs somebody waits for; the other loop also does the health check."""
        failures = 0
        while not self.stop:
            now = time.monotonic()
            if now < self.rate_limited_until:
                time.sleep(min(30, self.rate_limited_until - now))
                if not interactive_only:
                    self.orbit.heartbeat()
                continue
            if not interactive_only:
                self.check_health()
            try:
                self.step(CLAIM_WAIT_SECONDS, interactive_only)
                failures = 0
            except (urllib.error.URLError, OSError) as exc:
                failures += 1
                delay = min(60, 2**failures)
                log.warning("cannot reach Orbit (%s); retrying in %ss", exc, delay)
                time.sleep(delay)

    def run(self) -> None:
        self.check_health(force=True)
        slot = None
        if self.config.interactive_slot:
            slot = threading.Thread(
                target=self.loop, kwargs={"interactive_only": True}, name="interactive", daemon=True
            )
            slot.start()
            log.info("a second slot is reserved for captures and drafts")
        self.loop()
        if slot is not None:
            slot.join(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--once", action="store_true", help="answer at most one job, then exit")
    parser.add_argument(
        "--selftest", action="store_true", help="check Claude Code works, then exit"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[claude-worker] %(message)s", stream=sys.stderr)
    config = Config.load()
    if args.selftest:
        ok, why = selftest(config)
        print("ok" if ok else f"failed: {why}")
        return 0 if ok else 1
    worker = Worker(config)
    if args.once:
        worker.step(0)
        return 0
    signal.signal(signal.SIGTERM, lambda *_: setattr(worker, "stop", True))
    signal.signal(signal.SIGINT, lambda *_: setattr(worker, "stop", True))
    worker.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
