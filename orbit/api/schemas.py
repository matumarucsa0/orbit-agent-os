"""JSON API response/request models."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from orbit.domain.enums import (
    DebtDirection,
    DebtType,
    FetchSource,
    FetchStatus,
    GoalHorizon,
    GoalStatus,
    HandleType,
    InteractionKind,
    LessonKind,
    MeetingLinkSource,
    PromiseDirection,
    PromiseStatus,
    ProposalKind,
    ProposalStatus,
    ReviewStatus,
    SchoolEventKind,
    SchoolEventSource,
    ScoreSource,
    Semester,
)


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class TagOut(_Out):
    id: int
    name: str
    cadence_days: int | None


class TagWithCountOut(TagOut):
    people: int


class AliasOut(_Out):
    id: int
    value: str


class HandleOut(_Out):
    id: int
    type: HandleType
    value: str
    value_norm: str
    label: str | None


class NoteOut(_Out):
    id: int
    text: str
    created_at: datetime
    updated_at: datetime


class PersonRef(_Out):
    id: int
    display_name: str


class InteractionOut(_Out):
    id: int
    occurred_on: date
    kind: InteractionKind
    summary: str | None
    people: list[PersonRef]


class PersonSummaryOut(_Out):
    id: int
    display_name: str
    first_name: str | None
    last_name: str | None
    city: str | None
    archived: bool
    tags: list[TagOut]


class PersonOut(PersonSummaryOut):
    birthday_month: int | None
    birthday_day: int | None
    birthday_year: int | None
    nameday_country: str | None
    nameday_name: str | None
    country: str | None
    how_met: str | None
    met_through_id: int | None
    photo_url: str | None
    cadence_days: int | None
    vcard_uid: str | None
    created_at: datetime
    updated_at: datetime
    aliases: list[AliasOut]
    handles: list[HandleOut]
    notes: list[NoteOut]
    interactions: list[InteractionOut]
    last_interaction_on: date | None = None


class HandleIn(BaseModel):
    type: HandleType
    value: str = Field(min_length=1, max_length=255)
    label: str | None = Field(default=None, max_length=60)


class AliasIn(BaseModel):
    value: str = Field(min_length=1, max_length=100)


class TagIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class NoteIn(BaseModel):
    text: str = Field(min_length=1)


class MergeIn(BaseModel):
    keep_id: int
    merge_id: int


class MergeRecordOut(_Out):
    id: int
    keep_id: int
    merged_id: int
    merged_name: str
    created_at: datetime


class TagCadenceIn(BaseModel):
    cadence_days: int | None = Field(ge=1, le=3650)


class PairIn(BaseModel):
    a_id: int
    b_id: int


class DuplicateOut(BaseModel):
    a: PersonSummaryOut
    b: PersonSummaryOut
    reason: str


class ImportReportOut(BaseModel):
    created: int
    updated: int
    unchanged: int
    skipped: int
    conflicts: list[str]
    summary: str


class SyncChangeOut(BaseModel):
    id: int
    person_id: int | None
    person_name: str
    field: str
    label: str
    direction: str
    kind: str
    state: str
    old: str
    new: str
    choices: list[str]
    default: str | None
    error: str | None


class SyncRunOut(BaseModel):
    id: int
    source: str
    status: str
    started_at: datetime
    summary: dict[str, Any]
    changes: list[SyncChangeOut]


class SyncApplyIn(BaseModel):
    # change id -> choice ("apply", "apple", "orbit", "link", "new"); anything else skips it.
    choices: dict[int, str] = Field(default_factory=dict)
    # Changes I was shown; the others stay open. Default: all of them.
    seen: list[int] | None = None


class SyncApplyOut(BaseModel):
    applied: int
    skipped: int
    failed: list[str]
    run: SyncRunOut


class SyncUndoIn(BaseModel):
    change_id: int


class BankImportOut(BaseModel):
    import_id: int
    bank: str
    rows_total: int
    new: int
    duplicates: int
    skipped: int
    flagged: int
    unclassified: int
    proposal_ids: list[int]
    payments: int  # incoming payments waiting in the Inbox to be matched
    payment_proposal_ids: list[int]
    summary: str  # "12 new, 3 flagged for splitting" (shown by the `Orbit Import` Shortcut)


class DebtOut(_Out):
    id: int
    person: PersonRef
    direction: DebtDirection
    type: DebtType
    amount_minor: int | None
    currency: str | None
    description: str | None
    reference_code: str | None
    due: date | None
    reminder_days: int | None
    parent_id: int | None
    created_at: datetime
    updated_at: datetime
    settled_at: datetime | None
    settled_by_transaction_id: int | None


class SettleIn(BaseModel):
    # Omit for a full settlement; a smaller amount settles part of a money debt.
    amount_minor: int | None = Field(default=None, gt=0)


class SettleOut(BaseModel):
    settled: DebtOut
    remainder: DebtOut | None


class PersonBalanceOut(BaseModel):
    person: PersonRef
    currency: str
    amount_minor: int  # > 0: they owe me; < 0: I owe them


class CurrencyTotalOut(BaseModel):
    currency: str
    owed_to_me: int
    i_owe: int
    net: int


class BalancesOut(BaseModel):
    totals: list[CurrencyTotalOut]
    people: list[PersonBalanceOut]


class PromiseOut(_Out):
    id: int
    person: PersonRef
    direction: PromiseDirection
    text: str
    due: date | None
    status: PromiseStatus
    goal_id: int | None
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None


class GoalPromiseOut(_Out):
    id: int
    text: str
    due: date | None
    status: PromiseStatus


class GoalOut(_Out):
    id: int
    title: str
    why: str | None
    horizon: GoalHorizon | None
    target: str | None
    target_date: date | None
    status: GoalStatus
    rank: int | None  # active goals only; 1 = most important
    reviewed_at: datetime | None
    closed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    people: list[PersonRef]
    promises: list[GoalPromiseOut]


class GoalRankIn(BaseModel):
    ids: list[int]  # every active goal exactly once, most important first


class GoalPersonIn(BaseModel):
    person_id: int


class GoalPromiseIn(BaseModel):
    promise_id: int


# --- Inbox & capture ---


class CaptureIn(BaseModel):
    text: str = Field(min_length=1, max_length=10000)
    # From the post-meeting push; the Shortcut sends "" when launched without one.
    meeting_id: int | None = None

    @field_validator("meeting_id", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value


class CaptureOut(BaseModel):
    summary: str  # shown by the Shortcut as a notification
    proposal_ids: list[int]
    items: int


class ProposalOut(_Out):
    id: int
    kind: ProposalKind
    status: ProposalStatus
    summary: str
    payload: dict[str, Any] = Field(validation_alias="payload_json")
    confidence: float | None
    source_type: str
    source_id: str | None
    created_at: datetime
    resolved_at: datetime | None
    reopened_at: datetime | None


class ConfirmIn(BaseModel):
    # An edited payload replaces the stored one before it's applied ("edit, then confirm").
    payload: dict[str, Any] | None = None


class ConfirmOut(BaseModel):
    proposal: ProposalOut
    message: str
    person_id: int | None = None


# --- Meetings ---


class MeetingPersonOut(BaseModel):
    id: int
    display_name: str
    link_source: MeetingLinkSource


class MeetingOut(BaseModel):
    id: int
    calendar_name: str
    title: str
    location: str | None
    start_utc: datetime
    end_utc: datetime
    cancelled: bool
    brief_sent_at: datetime | None
    capture_sent_at: datetime | None
    people: list[MeetingPersonOut]


class MeetingLinkIn(BaseModel):
    person_id: int


class SyncOut(BaseModel):
    new: int
    updated: int
    cancelled: int
    calendars: list[str]
    missing: list[str]  # allow-listed calendars (CALDAV_CALENDARS) the server doesn't have
    summary: str


# --- Weekly review ---


class ReviewOut(_Out):
    id: int
    plan_start: date
    plan_end: date
    review_start: date
    review_end: date
    status: ReviewStatus
    output: dict[str, Any] | None
    error: str | None
    attempts: int
    model: str | None
    input_chars: int | None
    input_tokens: int
    output_tokens: int
    pushed_at: datetime | None
    created_at: datetime
    proposal_ids: list[int]


# --- School (SPEC §2.20) ---


class CourseRefOut(_Out):
    id: int
    code: str
    name: str


class LessonOut(_Out):
    id: int
    uid: str
    ticket_code: str | None
    kind: LessonKind
    course: CourseRefOut | None
    title: str
    teachers_text: str | None
    room: str | None
    location: str | None
    start_utc: datetime
    end_utc: datetime
    cancelled: bool
    changed_at: datetime | None
    change_note: str | None


class CourseScoreOut(_Out):
    source: ScoreSource
    gained: float
    total: float
    pass_threshold: float | None
    passes: bool | None
    fetched_at: datetime


class CourseOut(_Out):
    id: int
    code: str
    name: str
    name_en: str | None
    credits: int | None
    hours_lecture: int | None
    hours_seminar: int | None
    completion: str | None
    semester: Semester | None
    department: str | None
    guarantor: str | None
    language: str | None
    web_url: str | None
    sis_url: str
    detail_fetched_at: datetime | None
    next_lesson_utc: datetime | None = None
    scores: list[CourseScoreOut] = Field(default_factory=list)


class SchoolEventOut(_Out):
    id: int
    source: SchoolEventSource
    kind: SchoolEventKind
    title: str
    description: str | None
    location: str | None
    url: str | None
    course: CourseRefOut | None
    start_utc: datetime
    end_utc: datetime | None
    second_deadline_utc: datetime | None
    points: float | None
    max_points: float | None
    all_day: bool
    cancelled: bool


class CourseDetailOut(BaseModel):
    course: CourseOut
    lessons: list[LessonOut]  # from today, the next 60 days
    events: list[SchoolEventOut]


class SchoolEventIn(BaseModel):
    """A manual exam, deadline or other item: `day` (local), `at` "HH:MM" (none: all day)."""

    title: str = Field(min_length=1, max_length=300)
    kind: SchoolEventKind = SchoolEventKind.DEADLINE
    day: date
    at: str | None = Field(default=None, pattern=r"^\d{1,2}:\d{2}$")
    course_code: str | None = Field(default=None, max_length=20)
    note: str | None = Field(default=None, max_length=5000)


class SchoolEventPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    kind: SchoolEventKind | None = None
    day: date | None = None
    at: str | None = Field(default=None, pattern=r"^\d{1,2}:\d{2}$")
    course_code: str | None = Field(default=None, max_length=20)
    note: str | None = Field(default=None, max_length=5000)


class FetchOut(_Out):
    status: FetchStatus
    started_at: datetime
    items_seen: int
    items_changed: int
    error: str | None


class FeedStatusOut(BaseModel):
    source: FetchSource
    last: FetchOut | None
    last_ok_at: datetime | None


class SchoolStatusOut(BaseModel):
    configured: bool
    sis: bool
    recodex_feed: bool
    recodex_api: bool
    owl: bool
    recodex_token_expires: datetime | None
    feeds: list[FeedStatusOut]


class SchoolSyncOut(BaseModel):
    summary: str
    results: list[dict[str, Any]]
