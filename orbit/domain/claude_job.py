"""The Claude job queue and the state of the worker that serves it (SPEC §2.19)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from orbit.domain.base import Base, enum_column, utcnow
from orbit.domain.enums import JobStatus


class ClaudeJob(Base):
    """One question for Claude, answered by the Claude Code worker on the server.

    `system`, `prompt` and `schema_json` hold what was sent (already masked); they and the result
    are cleared after CLAUDE_JOB_RETENTION_DAYS. The row, status and usage stay."""

    __tablename__ = "claude_job"

    id: Mapped[int] = mapped_column(primary_key=True)
    # capture, classification, meeting_link, brief, draft (the `purpose` of the API client), review.
    kind: Mapped[str] = mapped_column(String(40))
    status: Mapped[JobStatus] = mapped_column(enum_column(JobStatus, "job_status"), index=True)
    priority: Mapped[int]  # 1 = someone is waiting, 2 = background; lower goes first
    input_hash: Mapped[str] = mapped_column(String(64), index=True)  # sha256 of kind + prompt
    system: Mapped[str] = mapped_column(Text)
    prompt: Mapped[str] = mapped_column(Text)
    schema_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    # When the caller stops waiting. Later, the job is expired unless `late_json` is set.
    wait_until: Mapped[datetime]
    # What the late handler needs to use an answer that comes after the caller gave up (captures).
    late_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # The weekly review this job makes (kind "review"); the review row keeps its own attempt count.
    review_id: Mapped[int | None] = mapped_column(
        ForeignKey("weekly_review.id", ondelete="SET NULL")
    )
    # The report this job makes (kind "report", SPEC §2.22); the report row keeps its own state.
    report_id: Mapped[int | None] = mapped_column(
        ForeignKey("review_report.id", ondelete="SET NULL")
    )
    # True once the caller or the late handler has used the answer.
    consumed: Mapped[bool] = mapped_column(default=False)
    attempts: Mapped[int] = mapped_column(default=0)
    claimed_at: Mapped[datetime | None]
    lease_until: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(100))
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    duration_ms: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


class ClaudeWorker(Base):
    """The single row (id 1) the worker keeps up to date: is it alive, logged in, rate limited."""

    __tablename__ = "claude_worker"

    id: Mapped[int] = mapped_column(primary_key=True)
    last_seen_at: Mapped[datetime | None]
    cli_version: Mapped[str | None] = mapped_column(String(100))
    auth_ok: Mapped[bool] = mapped_column(default=True)
    rate_limited_until: Mapped[datetime | None]
    last_selftest_at: Mapped[datetime | None]
