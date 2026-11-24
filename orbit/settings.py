"""Application settings, loaded from the environment / `.env` (see `.env.example`)."""

from __future__ import annotations

import re
from datetime import time
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

MIN_SECRET_LENGTH = 32
# The ical.php feeds Phase 18 reads (SPEC §2.20); komise, esc and term_uc2 are unused.
SIS_FEED_TYPES = ("rozvrhng", "term_st2", "stev")
_QUIET_HOURS_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


class ModelPrice(BaseModel):
    """What one model costs, in euros per million tokens (see orbit.services.pricing)."""

    input_eur_per_mtok: float = Field(ge=0)
    output_eur_per_mtok: float = Field(ge=0)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        validate_by_name=True,
    )

    # --- Core ---
    # How Claude's prompts refer to the one user ("the owner" unless you set a name).
    owner_name: str = Field(default="the owner", min_length=1, max_length=80)
    orbit_url: str = "http://localhost:8000"
    orbit_api_token: SecretStr
    orbit_web_password_hash: SecretStr
    orbit_secret_key: SecretStr
    ics_token: SecretStr
    # A second bearer token that can only call /api/v1/review-agent/* (the review made by an
    # external Claude session, SPEC §2.18). Blank = not set.
    orbit_review_token: SecretStr | None = None
    # A third bearer token for the MCP server: it opens only `GET /api/v1/context` (read-only).
    # Blank = not set (the MCP server then falls back to the API token with a warning).
    orbit_mcp_token: SecretStr | None = None
    timezone: ZoneInfo = ZoneInfo("Europe/Prague")
    database_url: str = "sqlite:///./orbit.db"
    session_max_age_days: int = Field(default=30, ge=1)
    # In-process jobs (sending queued pushes, …); run exactly one app process with this on.
    scheduler_enabled: bool = True

    # --- Claude ---
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str | None = None
    # Other models for other jobs (SPEC §2.31); unset means the default model above. Fast: meeting
    # links, bank row sorting, meeting briefs. Deep: the weekly review and reports, with adaptive
    # thinking at `anthropic_deep_effort` ("off": no thinking, for a model that has none).
    anthropic_model_fast: str | None = None
    anthropic_model_deep: str | None = None
    anthropic_deep_effort: Literal["off", "low", "medium", "high", "xhigh", "max"] = "high"
    brief_use_claude: bool = True
    # Who answers Claude's questions (SPEC §2.19): "api" (ANTHROPIC_API_KEY), "worker" (Claude
    # Code on the server, through the job queue), "none", or "auto": api if the key is set, else
    # worker if ORBIT_REVIEW_TOKEN is set, else none.
    claude_backend: Literal["auto", "api", "worker", "none"] = "auto"
    # ORBIT_CLAUDE_MODE overrides the backend above: "fake" answers offline with canned,
    # rule-based outputs (demo, screenshots, CI; DECISIONS #35); "api" and "worker" force that
    # backend; "auto" (default) keeps CLAUDE_BACKEND.
    claude_mode: Literal["auto", "fake", "api", "worker"] = Field(
        default="auto", validation_alias="ORBIT_CLAUDE_MODE"
    )
    # A demo instance (seed-demo prints this): with mode "auto" and no API key and no worker,
    # Claude is the offline FakeClaude instead of "none".
    orbit_demo: bool = False
    # How long a caller waits for the worker before it falls back (seconds).
    claude_wait_interactive_seconds: int = Field(default=45, ge=1, le=300)
    claude_wait_background_seconds: int = Field(default=120, ge=1, le=900)
    # Queued jobs allowed per priority; beyond it a call fails fast to its fallback.
    claude_queue_cap_interactive: int = Field(default=10, ge=1)
    claude_queue_cap_background: int = Field(default=50, ge=1)
    # Prompts and answers of finished jobs are cleared after this many days.
    claude_job_retention_days: int = Field(default=7, ge=1)
    # How often the housekeeping jobs run, and how long saved Apple cards are kept.
    claude_prune_hours: int = Field(default=1, ge=1, le=168)
    backup_prune_hours: int = Field(default=24, ge=1, le=168)
    contact_backup_keep_days: int = Field(default=90, ge=1, le=3650)
    # The Sunday-evening weekly review (needs Claude); see SPEC §2.17.
    weekly_review_enabled: bool = True
    # The review starts on this weekday (Fri 4 to Sun 6) at this time and plans the next week;
    # a missed one is still made until the catch-up weekday (Mon 0 to Thu 3).
    weekly_review_weekday: int = Field(default=6, ge=4, le=6)
    weekly_review_time: Annotated[time, NoDecode] = time(19, 0)
    weekly_review_catch_up_weekday: int = Field(default=2, ge=0, le=3)
    # Size limit (characters of JSON) of the context snapshot an agent reads; about 4 per token.
    context_budget_chars: int = Field(default=400_000, ge=1_000, le=4_000_000)
    # Optional hint for birthday / name-day drafts ("casual, short, an emoji at most").
    message_style: str | None = Field(default=None, max_length=500)

    # --- Notifications (ntfy) ---
    ntfy_url: str = "https://ntfy.sh"
    ntfy_topic: str | None = None
    ntfy_token: SecretStr | None = None
    quiet_hours: Annotated[tuple[time, time], NoDecode] = (time(22, 0), time(8, 0))
    # A failed push is tried again after this long, up to the attempt limit, and dropped when
    # older than the max age (SPEC §2.11). Editable on /settings/schedule (SPEC §2.21).
    notification_retry_minutes: int = Field(default=5, ge=1, le=120)
    notification_max_attempts: int = Field(default=5, ge=1, le=20)
    notification_max_age_hours: int = Field(default=24, ge=1, le=168)

    # --- Calendar (iCloud CalDAV) ---
    caldav_url: str = "https://caldav.icloud.com"
    caldav_username: str | None = None
    caldav_password: SecretStr | None = None
    caldav_calendars: Annotated[list[str], NoDecode] = Field(default_factory=list)
    caldav_poll_minutes: int = Field(default=10, ge=1)
    brief_minutes_before: int = Field(default=30, ge=0)
    capture_minutes_after: int = Field(default=15, ge=0)
    # iCloud Contacts (CardDAV); signs in with CALDAV_USERNAME / CALDAV_PASSWORD.
    carddav_url: str = "https://contacts.icloud.com"

    # --- Ledger / bank ---
    default_currency: str = Field(default="EUR", pattern=r"^[A-Z]{3}$")
    shared_min_amount_minor: int = Field(default=1500, ge=0, validation_alias="SHARED_MIN_AMOUNT")
    item_reminder_days: int = Field(default=21, ge=1)

    # --- Reminders and the digest (SPEC §2.11), editable on /settings/schedule ---
    reminder_time: Annotated[time, NoDecode] = time(9, 0)
    reminder_close_days_ahead: int = Field(default=5, ge=1, le=30)
    digest_weekday: int = Field(default=6, ge=0, le=6)
    digest_time: Annotated[time, NoDecode] = time(18, 0)
    # Printed in payment requests ("pay me back" texts); optional.
    payment_iban: str | None = None
    payment_name: str | None = Field(default=None, max_length=140)

    # --- School (SPEC §2.20): SIS token feeds, ReCodEx, Owl ---
    sis_base_url: str = "https://sis.example.edu/studium"
    # The Rozvrh "Export" key (roz_externi.php): lessons, when set.
    sis_timetable_key: SecretStr | None = None
    # The account-level ical.php token: exams and SIS events (and lessons without the key above).
    sis_ical_token: SecretStr | None = None
    sis_feed_types: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["rozvrhng", "term_st2", "stev"]
    )
    sis_poll_minutes: int = Field(default=60, ge=15)
    sis_faculty_id: str = Field(default="10000", pattern=r"^\d{3,6}$")
    sis_course_ttl_days: int = Field(default=30, ge=1)
    school_evening_time: Annotated[time, NoDecode] = time(20, 0)
    school_pushes_enabled: bool = True
    # Exam and deadline reminders go out at this time, this many days before.
    school_reminder_time: Annotated[time, NoDecode] = time(18, 0)
    school_exam_days: Annotated[list[int], NoDecode] = Field(default_factory=lambda: [7, 1])
    school_deadline_days: Annotated[list[int], NoDecode] = Field(default_factory=lambda: [3, 1])
    school_change_gap_minutes: int = Field(default=60, ge=1, le=1440)
    school_token_warn_days: int = Field(default=14, ge=1, le=90)
    # Course pages and PDFs (Phase 18c): checked once a day from this local hour; at most this
    # many Claude reads of them per day; uploaded PDFs are kept here.
    school_sites_hour: int = Field(default=7, ge=0, le=23)
    school_reads_per_day: int = Field(default=20, ge=1, le=200)
    school_files_dir: str = "data/school/files"
    recodex_base_url: str = "https://recodex.example.edu/api/v1"
    recodex_ical_token: SecretStr | None = None
    recodex_api_token: SecretStr | None = None
    owl_base_url: str = "https://owl.example.edu"
    owl_session_cookie: SecretStr | None = None

    # --- Fitness (SPEC §2.35): Liftoff workouts through the app's private API ---
    # The version is in the host name; after "server is deprecated" put the new host here.
    liftoff_api_base: str = "https://v2-16-3.api.getgymbros.com"
    # Captured from the app's traffic (Google sign-in has no password). Treat as a password.
    liftoff_refresh_token: SecretStr | None = None
    liftoff_poll_hours: int = Field(default=24, ge=1, le=168)

    # Cost (docs/PROMPTS.md, DECISIONS #36). Built-in list prices are in US dollars and converted
    # with USD_EUR_RATE, a fixed number you set (no live rate): the euro figures are estimates.
    # CLAUDE_PRICE_OVERRIDES is JSON, e.g. {"claude-haiku-4-5": {"input_eur_per_mtok": 0.9,
    # "output_eur_per_mtok": 4.6}}, matched by exact model name, in euros.
    usd_eur_rate: float = Field(default=0.92, gt=0, le=10)
    claude_price_overrides: dict[str, ModelPrice] = Field(default_factory=dict)
    # Estimated spend per calendar month on the API backend: a push at 80 percent, no more API
    # calls at 100 percent (they fall back as without a key). 0 = no limit.
    claude_monthly_budget_eur: float = Field(default=0, ge=0)
    # Tracing: "none" (default, nothing is created), "console" (spans printed to stdout) or "otlp"
    # (OTLP over HTTP; the endpoint comes from the standard OTEL_EXPORTER_OTLP_* variables and
    # needs the `otlp` extra).
    orbit_otel_exporter: Literal["none", "console", "otlp"] = "none"

    # Backups (BACKUP_*, RCLONE_CONFIG_*) are read by the backup container, not by the app.

    @property
    def resolved_claude_backend(self) -> Literal["api", "worker", "fake", "none"]:
        """The backend in use: ORBIT_CLAUDE_MODE, else `claude_backend` with "auto" worked out;
        "api" without a key and model is "none"; a demo instance with nothing else is "fake"."""
        if self.claude_mode == "fake":
            return "fake"
        choice = self.claude_backend if self.claude_mode == "auto" else self.claude_mode
        has_api = bool(self.anthropic_api_key and self.anthropic_model)
        has_worker = self.orbit_review_token is not None
        found: Literal["api", "worker", "none"] = "none"
        if choice == "auto":
            found = "api" if has_api else "worker" if has_worker else "none"
        elif choice == "api":
            found = "api" if has_api else "none"
        elif choice == "worker":
            found = "worker" if has_worker else "none"
        if found == "none" and self.orbit_demo and self.claude_mode == "auto":
            return "fake"
        return found

    @field_validator("orbit_url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("must start with http:// or https://")
        return value.rstrip("/")

    @field_validator("orbit_api_token", "orbit_secret_key", "ics_token")
    @classmethod
    def _check_secret_length(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < MIN_SECRET_LENGTH:
            raise ValueError(
                f"must be at least {MIN_SECRET_LENGTH} characters (use a random value)"
            )
        return value

    @field_validator("orbit_review_token", "orbit_mcp_token", mode="before")
    @classmethod
    def _blank_review_token(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("orbit_review_token", "orbit_mcp_token")
    @classmethod
    def _check_review_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            return cls._check_secret_length(value)
        return None

    @field_validator("orbit_web_password_hash")
    @classmethod
    def _check_password_hash(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("$argon2"):
            raise ValueError(
                "must be an argon2 hash (run: uv run python -m orbit.cli hash-password)"
            )
        return value

    @field_validator("payment_iban")
    @classmethod
    def _check_iban(cls, value: str | None) -> str | None:
        if value is None:
            return None
        compact = "".join(value.split()).upper()
        if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", compact):
            raise ValueError("expected an IBAN like SK31 1200 0000 1987 4263 7541")
        return compact

    @field_validator("quiet_hours", mode="before")
    @classmethod
    def _parse_quiet_hours(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        match = _QUIET_HOURS_RE.match(value)
        if not match:
            raise ValueError("expected HH:MM-HH:MM")
        h1, m1, h2, m2 = (int(g) for g in match.groups())
        return (time(h1, m1), time(h2, m2))

    @field_validator("caldav_calendars", mode="before")
    @classmethod
    def _parse_calendars(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [name.strip() for name in value.split(",") if name.strip()]
        return value

    @field_validator("sis_base_url", "recodex_base_url", "owl_base_url", "liftoff_api_base")
    @classmethod
    def _check_school_url(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("must start with https://")
        return value.rstrip("/")

    @field_validator("sis_feed_types", mode="before")
    @classmethod
    def _parse_feed_types(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = [name.strip() for name in value.split(",") if name.strip()]
        return value

    @field_validator("sis_feed_types")
    @classmethod
    def _check_feed_types(cls, value: list[str]) -> list[str]:
        unknown = [name for name in value if name not in SIS_FEED_TYPES]
        if unknown:
            raise ValueError(
                f"unknown feed type(s) {', '.join(unknown)}; use {', '.join(SIS_FEED_TYPES)}"
            )
        return list(dict.fromkeys(value))

    @field_validator(
        "school_evening_time",
        "school_reminder_time",
        "reminder_time",
        "digest_time",
        "weekly_review_time",
        mode="before",
    )
    @classmethod
    def _parse_clock_time(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", value)
        if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
            raise ValueError("expected HH:MM")
        return time(int(match.group(1)), int(match.group(2)))

    @field_validator("school_exam_days", "school_deadline_days", mode="before")
    @classmethod
    def _parse_day_list(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("school_exam_days", "school_deadline_days")
    @classmethod
    def _check_day_list(cls, value: list[int]) -> list[int]:
        if not value or any(not 1 <= day <= 60 for day in value):
            raise ValueError("expected one or more day counts between 1 and 60, like 7,1")
        return sorted(set(value), reverse=True)

    @field_validator(
        "sis_timetable_key",
        "sis_ical_token",
        "recodex_ical_token",
        "recodex_api_token",
        "owl_session_cookie",
        "liftoff_refresh_token",
        mode="before",
    )
    @classmethod
    def _blank_school_secret(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("shared_min_amount_minor", mode="before")
    @classmethod
    def _parse_amount(cls, value: Any) -> Any:
        """Accept a decimal major-unit string ("15.00") and convert it to minor units (1500)."""
        if isinstance(value, int):
            return value
        try:
            amount = Decimal(str(value).strip())
        except InvalidOperation as exc:
            raise ValueError("expected a decimal amount like 15.00") from exc
        if not amount.is_finite():
            raise ValueError("expected a decimal amount like 15.00")
        minor = amount * 100
        if minor != minor.to_integral_value():
            raise ValueError("at most 2 decimal places")
        return int(minor)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # required fields come from the environment
