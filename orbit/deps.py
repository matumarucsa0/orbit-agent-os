"""FastAPI dependencies shared by the API and web routers."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import BackgroundTasks, Depends, Request
from sqlalchemy.orm import Session

from orbit.integrations.caldav_client import CalendarSource
from orbit.integrations.carddav import CardDavClient, ContactSource
from orbit.integrations.claude import (
    CaptureExtractor,
    MeetingLinker,
    MessageDrafter,
    ReportWriter,
    TransactionClassifier,
    WeeklyReviewer,
)
from orbit.integrations.ecb import RateSource
from orbit.integrations.ntfy import PushSender
from orbit.services import capture, capture_defer, notifications
from orbit.services.school_sync import SchoolSources
from orbit.settings import Settings


def get_app_settings(request: Request) -> Settings:
    """The effective settings: env values plus what was edited in the app (SPEC §2.21)."""
    settings: Settings = request.app.state.overrides.apply(request.app.state.settings)
    return settings


def get_db(request: Request) -> Iterator[Session]:
    with request.app.state.sessionmaker() as session:
        yield session


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
DbSession = Annotated[Session, Depends(get_db)]


def get_contact_source(settings: SettingsDep) -> ContactSource | None:
    """iCloud Contacts via CardDAV, or None when no credentials are configured."""
    if not settings.caldav_username or settings.caldav_password is None:
        return None
    return CardDavClient.connect(
        settings.carddav_url, settings.caldav_username, settings.caldav_password.get_secret_value()
    )


ContactSourceDep = Annotated[ContactSource | None, Depends(get_contact_source)]


def get_capture_extractor(request: Request) -> CaptureExtractor | None:
    """Claude, or None when ANTHROPIC_API_KEY / ANTHROPIC_MODEL aren't set."""
    extractor: CaptureExtractor | None = request.app.state.claude
    return extractor


def get_push_sender(request: Request) -> PushSender | None:
    """ntfy, or None when NTFY_TOPIC isn't set."""
    sender: PushSender | None = request.app.state.push_sender
    return sender


def get_transaction_classifier(request: Request) -> TransactionClassifier | None:
    """Claude, or None when it isn't configured."""
    classifier: TransactionClassifier | None = request.app.state.claude
    return classifier


def get_calendar_source(request: Request) -> CalendarSource | None:
    """iCloud Calendar, or None without CALDAV_USERNAME / CALDAV_PASSWORD / CALDAV_CALENDARS."""
    source: CalendarSource | None = request.app.state.calendar
    return source


def get_meeting_linker(request: Request) -> MeetingLinker | None:
    """Claude (to pick between people with the same name), or None when it isn't configured."""
    linker: MeetingLinker | None = request.app.state.claude
    return linker


def get_message_drafter(request: Request) -> MessageDrafter | None:
    """Claude (birthday / name-day message drafts), or None when it isn't configured."""
    drafter: MessageDrafter | None = request.app.state.claude
    return drafter


def get_weekly_reviewer(request: Request) -> WeeklyReviewer | None:
    """Claude (the weekly review), or None when it isn't configured."""
    reviewer: WeeklyReviewer | None = request.app.state.reviewer
    return reviewer


def get_report_writer(request: Request) -> ReportWriter | None:
    """Claude through the API (custom reports), or None when it isn't the API client."""
    writer: ReportWriter | None = request.app.state.report_writer
    return writer


def get_rate_source(request: Request) -> RateSource:
    """ECB daily rates (fetched lazily, cached per day)."""
    rates: RateSource = request.app.state.rates
    return rates


ReportWriterDep = Annotated[ReportWriter | None, Depends(get_report_writer)]
WeeklyReviewerDep = Annotated[WeeklyReviewer | None, Depends(get_weekly_reviewer)]
CaptureExtractorDep = Annotated[CaptureExtractor | None, Depends(get_capture_extractor)]
PushSenderDep = Annotated[PushSender | None, Depends(get_push_sender)]
TransactionClassifierDep = Annotated[
    TransactionClassifier | None, Depends(get_transaction_classifier)
]
RateSourceDep = Annotated[RateSource, Depends(get_rate_source)]
CalendarSourceDep = Annotated[CalendarSource | None, Depends(get_calendar_source)]
MeetingLinkerDep = Annotated[MeetingLinker | None, Depends(get_meeting_linker)]
MessageDrafterDep = Annotated[MessageDrafter | None, Depends(get_message_drafter)]


def get_school_sources(request: Request) -> SchoolSources:
    """SIS, ReCodEx and Owl readers (each None when its secret isn't set)."""
    sources: SchoolSources = request.app.state.school
    return sources


SchoolSourcesDep = Annotated[SchoolSources, Depends(get_school_sources)]


def run_capture(
    request: Request,
    background: BackgroundTasks,
    db: Session,
    settings: Settings,
    extractor: CaptureExtractor | None,
    text: str,
    *,
    meeting_id: int | None = None,
) -> capture.CaptureResult:
    """Save a dictation at once and let Claude read it afterwards (SPEC §2.33). Commits; raises
    InvalidInputError for empty text. With no Claude client the note draft is all there is."""
    result = capture.capture(
        db,
        extractor,
        text,
        today=datetime.now(UTC).astimezone(settings.timezone).date(),
        default_currency=settings.default_currency,
        meeting_id=meeting_id,
        tz=settings.timezone,
        defer=extractor is not None,
    )
    db.commit()
    if result.pending is not None and extractor is not None:
        capture_defer.dispatch(
            extractor,
            result.pending,
            sessions=request.app.state.sessionmaker,
            sender=request.app.state.push_sender,
            base_url=settings.orbit_url,
            quiet=notifications.quiet_hours(settings),
            run_later=background.add_task,
        )
    return result
