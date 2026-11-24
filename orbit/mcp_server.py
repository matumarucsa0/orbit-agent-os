"""MCP server for Claude Code: reads Orbit's context over the HTTP API (SPEC §2.39, docs/MCP.md).

Run by Claude Code from `.mcp.json`: `uv run python -m orbit.mcp_server`.

One static, read-only tool (`get_context`). It uses `ORBIT_MCP_TOKEN`, a token the server accepts
only on `GET /api/v1/context`. Every argument is checked here before a request is made, and every
call (accepted or not) is appended to a JSONL audit log: tool, hash of the arguments, status,
response size and duration. The log never holds the token, the response text or rejected input.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
from mcp.server.mcpserver import MCPServer

log = logging.getLogger("orbit.mcp")

DEFAULT_URL = "http://localhost:8000"
TIMEOUT_SECONDS = 30.0

PRESETS = ("this_week", "next_7_days")
MAX_WINDOW_DAYS = 92  # the API's own limit (orbit.services.context.windows)
MIN_DATE = date(2000, 1, 1)
MAX_DATE = date(2100, 12, 31)
MAX_PERSON_ID = 2**31 - 1
MAX_ARG_LENGTH = 32
MAX_RESPONSE_BYTES = 2_000_000
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

DEFAULT_AUDIT_PATH = "~/.local/state/orbit/mcp-audit.jsonl"
DEFAULT_AUDIT_MAX_BYTES = 1_000_000
AUDIT_BACKUPS = 3


class OrbitMcpError(Exception):
    """A failed Orbit call, with a message that is safe to show (never headers or the token)."""


class InvalidArguments(OrbitMcpError):
    """An argument failed validation; nothing was sent to Orbit."""


# --- Validation ---


def _date(name: str, value: str) -> date:
    if len(value) > MAX_ARG_LENGTH or not _DATE_RE.fullmatch(value):
        raise InvalidArguments(f"{name} must be a date like 2026-10-01.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise InvalidArguments(f"{name} is not a real date.") from exc
    if not MIN_DATE <= parsed <= MAX_DATE:
        raise InvalidArguments(f"{name} must be between {MIN_DATE.year} and {MAX_DATE.year}.")
    return parsed


def validate_arguments(
    preset: str | None, start: str | None, end: str | None, person_id: int | None
) -> dict[str, str | int]:
    """The query parameters for `GET /context`, or InvalidArguments."""
    params: dict[str, str | int] = {}
    if preset:
        if preset not in PRESETS:  # an allow-list, not a pattern
            raise InvalidArguments(f"preset must be one of: {', '.join(PRESETS)}.")
        params["preset"] = preset
    if start or end:
        if not (start and end):
            raise InvalidArguments("Give both start and end, or neither.")
        first, last = _date("start", start), _date("end", end)
        if last < first:
            raise InvalidArguments("end is before start.")
        if (last - first).days + 1 > MAX_WINDOW_DAYS:
            raise InvalidArguments(f"A window can be at most {MAX_WINDOW_DAYS} days.")
        params["start"], params["end"] = first.isoformat(), last.isoformat()
    if person_id is not None:
        if isinstance(person_id, bool) or not isinstance(person_id, int):
            raise InvalidArguments("person_id must be a whole number.")
        if not 1 <= person_id <= MAX_PERSON_ID:
            raise InvalidArguments("person_id is out of range.")
        params["person_id"] = person_id
    return params


# --- Audit log ---


class AuditLog:
    """Append-only JSONL file, one line per call, rotated by size (`path.1` ... `path.N`).

    Opened with O_APPEND and mode 0600; the server never reads, edits or truncates it. A write
    failure is reported on stderr and does not stop the tool: the log is a record, not a gate.
    """

    def __init__(
        self,
        path: str | Path,
        max_bytes: int = DEFAULT_AUDIT_MAX_BYTES,
        backups: int = AUDIT_BACKUPS,
    ) -> None:
        self._path = Path(path).expanduser()
        self._max = max_bytes
        self._backups = backups
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def _rotate(self) -> None:
        for index in range(self._backups, 0, -1):
            source = (
                self._path if index == 1 else self._path.with_name(f"{self._path.name}.{index - 1}")
            )
            if source.exists():
                source.replace(self._path.with_name(f"{self._path.name}.{index}"))

    def write(self, record: dict[str, object]) -> None:
        line = json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"
        try:
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with contextlib.suppress(OSError):
                    if self._path.stat().st_size + len(line) > self._max:
                        self._rotate()
                fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    os.write(fd, line.encode())
                finally:
                    os.close(fd)
        except OSError as exc:
            print(f"orbit-mcp: audit log write failed: {type(exc).__name__}", file=sys.stderr)


def audit_log_from_env() -> AuditLog:
    path = os.environ.get("ORBIT_MCP_AUDIT_LOG") or DEFAULT_AUDIT_PATH
    try:
        max_bytes = int(os.environ.get("ORBIT_MCP_AUDIT_MAX_BYTES", ""))
    except ValueError:
        max_bytes = DEFAULT_AUDIT_MAX_BYTES
    return AuditLog(path, max(max_bytes, 10_000))


def _args_hash(raw: dict[str, object]) -> str:
    text = json.dumps(raw, sort_keys=True, default=repr)
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:32]


# --- Orbit client ---


def make_client(
    base_url: str, token: str, transport: httpx.BaseTransport | None = None
) -> httpx.Client:
    return httpx.Client(
        base_url=base_url.rstrip("/") + "/api/v1",
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT_SECONDS,
        transport=transport,
    )


@dataclass
class CallResult:
    text: str
    http_status: int


def fetch_context(
    client: httpx.Client,
    preset: str | None = None,
    start: str | None = None,
    end: str | None = None,
    person_id: int | None = None,
) -> str:
    """`GET /context` as JSON text, after validating the arguments."""
    params = validate_arguments(preset, start, end, person_id)
    return _get(client, params).text


def _get(client: httpx.Client, params: dict[str, str | int]) -> CallResult:
    try:
        response = client.get("/context", params=params)
    except httpx.HTTPError as exc:
        raise OrbitMcpError(
            f"Cannot reach Orbit at {client.base_url}: {type(exc).__name__}"
        ) from exc
    if response.status_code != httpx.codes.OK:
        detail = ""
        with contextlib.suppress(ValueError):
            detail = str(response.json().get("detail", ""))[:200]
        raise HttpFailure(
            response.status_code, f"Orbit answered {response.status_code}. {detail}".strip()
        )
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise HttpFailure(
            response.status_code,
            "The context is too large; ask for a shorter window or one person.",
        )
    return CallResult(response.text, response.status_code)


class HttpFailure(OrbitMcpError):
    def __init__(self, http_status: int, message: str) -> None:
        super().__init__(message)
        self.http_status = http_status


def build_server(client: httpx.Client, audit: AuditLog | None = None) -> MCPServer:
    server = MCPServer("orbit")

    @server.tool()
    def get_context(
        preset: str | None = None,
        start: str | None = None,
        end: str | None = None,
        person_id: int | None = None,
    ) -> str:
        """Orbit's masked context snapshot as JSON: people, ledger, meetings, promises, goals,
        school, fitness (Liftoff workouts) and health (sleep, heart rate, steps, weight).

        Window: `preset` is `this_week` (default) or `next_7_days`, or give `start` and `end` as
        ISO dates (YYYY-MM-DD). `person_id` focuses on one person."""
        started = time.monotonic()
        raw: dict[str, object] = {
            "preset": preset,
            "start": start,
            "end": end,
            "person_id": person_id,
        }
        record: dict[str, object] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "tool": "get_context",
            "args_sha256": _args_hash(raw),
        }
        try:
            params = validate_arguments(preset, start, end, person_id)
            record["args"] = params  # validated values only; rejected input is never written
            result = _get(client, params)
        except InvalidArguments as exc:
            record.update(status="rejected", reason=str(exc)[:120], bytes=0)
            raise
        except HttpFailure as exc:
            record.update(status="error", http_status=exc.http_status, bytes=0)
            raise
        except OrbitMcpError:
            record.update(status="error", http_status=None, bytes=0)
            raise
        else:
            record.update(
                status="ok", http_status=result.http_status, bytes=len(result.text.encode())
            )
            return result.text
        finally:
            record["duration_ms"] = round((time.monotonic() - started) * 1000)
            if audit is not None:
                audit.write(record)

    return server


def resolve_token(env: dict[str, str] | os._Environ[str]) -> str | None:
    """ORBIT_MCP_TOKEN, else (deprecated) the full API token with a warning."""
    token = env.get("ORBIT_MCP_TOKEN", "")
    if token:
        return token
    legacy = env.get("ORBIT_API_TOKEN", "")
    if legacy:
        log.warning(
            "ORBIT_MCP_TOKEN is not set; falling back to ORBIT_API_TOKEN, which can also write. "
            "This fallback is deprecated: create a read-only ORBIT_MCP_TOKEN (docs/MCP.md)."
        )
        print(
            "orbit-mcp: DEPRECATED: using ORBIT_API_TOKEN because ORBIT_MCP_TOKEN is not set.",
            file=sys.stderr,
        )
        return legacy
    return None


def main() -> int:
    token = resolve_token(os.environ)
    if not token:
        print("ORBIT_MCP_TOKEN is not set.", file=sys.stderr)
        return 1
    client = make_client(os.environ.get("ORBIT_URL", DEFAULT_URL), token)
    build_server(client, audit_log_from_env()).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
