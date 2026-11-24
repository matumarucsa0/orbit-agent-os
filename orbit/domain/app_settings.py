"""Schedule settings edited in the app (SPEC §2.21): overrides, their change log, job status."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from orbit.domain.base import Base, utcnow


class AppSetting(Base):
    """One stored override: it wins over the env value. No row = the env value applies."""

    __tablename__ = "app_setting"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    # Canonical JSON form (see services/schedule_config.py): int, bool, "HH:MM", [7, 1], …
    value_json: Mapped[Any] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class AppSettingLog(Base):
    """Every change of an override, so a value can be put back. None = no override."""

    __tablename__ = "app_setting_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(60), index=True)
    old_json: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    new_json: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class JobRun(Base):
    """The last run of each scheduled job (one row per job)."""

    __tablename__ = "job_run"

    job_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    last_started_at: Mapped[datetime]
    last_finished_at: Mapped[datetime]
    ok: Mapped[bool]
    message: Mapped[str] = mapped_column(String(500), default="")
    manual: Mapped[bool] = mapped_column(default=False)
