"""FastAPI app factory."""

from __future__ import annotations

import mimetypes
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import SecretStr
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import Scope

from orbit import tracing
from orbit.api import actions, claude_worker, health, review_agent
from orbit.api.router import build_api_router
from orbit.db import make_engine, make_sessionmaker
from orbit.integrations.caldav_client import CalDavCalendarSource
from orbit.integrations.claude import ClaudeClient
from orbit.integrations.ecb import EcbRates
from orbit.integrations.fake_claude import FakeClaude
from orbit.integrations.liftoff import LiftoffClient
from orbit.integrations.ntfy import NtfyClient
from orbit.integrations.owl import OwlHttpSource
from orbit.integrations.pricing import cost_micro_eur
from orbit.integrations.recodex import RecodexHttpSource
from orbit.integrations.resources import HttpFetcher
from orbit.integrations.sis import PoliteClient, SisHttpSource
from orbit.jobs.scheduler import JobContext, flush_usage, start_scheduler
from orbit.services.budget import BudgetGuard
from orbit.services.claude_jobs import JobClaudeClient, QueueLimits
from orbit.services.errors import HandleConflictError, InvalidInputError, NotFoundError
from orbit.services.schedule_config import OverrideStore
from orbit.services.school_sync import SchoolSources
from orbit.services.usage import UsageLog
from orbit.settings import Settings, get_settings
from orbit.web import (
    auth,
    bank,
    calendar,
    contacts,
    context,
    duplicates,
    fitness,
    goals,
    health_days,
    inbox,
    ledger,
    meetings,
    pages,
    people,
    reports,
    reviews,
    school,
    school_map,
)
from orbit.web import claude as claude_ops
from orbit.web import schedule as schedule_web
from orbit.web.auth import safe_next
from orbit.web.templating import STATIC_DIR, flash, templates

IMMUTABLE = "public, max-age=31536000, immutable"


class VersionedStaticFiles(StaticFiles):
    """Static files that are cached for a year when asked for with `?v=<hash>` (`static_url`)."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200 and b"v=" in scope.get("query_string", b""):
            response.headers["Cache-Control"] = IMMUTABLE
        return response


# Not in every platform's mime table; browsers want this type for the PWA manifest.
mimetypes.add_type("application/manifest+json", ".webmanifest")


def _is_api(request: Request) -> bool:
    return request.url.path.startswith("/api/")


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(NotFoundError)
    async def not_found(request: Request, exc: NotFoundError) -> Response:
        if _is_api(request):
            return JSONResponse({"detail": str(exc)}, status_code=status.HTTP_404_NOT_FOUND)
        return templates.TemplateResponse(
            request, "error.html", {"message": str(exc)}, status_code=status.HTTP_404_NOT_FOUND
        )

    @app.exception_handler(HandleConflictError)
    async def handle_conflict(request: Request, exc: HandleConflictError) -> Response:
        return JSONResponse(
            {"detail": str(exc), "owner_id": exc.owner_id, "owner_name": exc.owner_name},
            status_code=status.HTTP_409_CONFLICT,
        )

    @app.exception_handler(InvalidInputError)
    async def invalid_input(request: Request, exc: InvalidInputError) -> Response:
        return JSONResponse({"detail": str(exc)}, status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)

    @app.exception_handler(RequestValidationError)
    async def form_validation(request: Request, exc: RequestValidationError) -> Response:
        if _is_api(request):
            return JSONResponse(
                {"detail": jsonable_errors(exc)},
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
        # Web forms: go back to the page with a readable message instead of a JSON error.
        fields = sorted({str(err["loc"][-1]) for err in exc.errors() if err.get("loc")})
        flash(
            request, "Please check: " + ", ".join(fields) if fields else "Invalid input.", "error"
        )
        referer = urlsplit(request.headers.get("referer", ""))
        back = "/"
        if referer.netloc == request.url.netloc:
            back = referer.path + (f"?{referer.query}" if referer.query else "")
        return RedirectResponse(safe_next(back), status_code=status.HTTP_303_SEE_OTHER)


def jsonable_errors(exc: RequestValidationError) -> list[dict[str, object]]:
    return [
        {"loc": list(err.get("loc", ())), "msg": err.get("msg", ""), "type": err.get("type", "")}
        for err in exc.errors()
    ]


def _secret(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value is not None else None


def build_workout_source(settings: Settings) -> LiftoffClient | None:
    """The Liftoff reader, when the refresh token is set (SPEC §2.35)."""
    token = _secret(settings.liftoff_refresh_token)
    return LiftoffClient(settings.liftoff_api_base, token) if token else None


def build_school_sources(settings: Settings) -> tuple[SchoolSources, PoliteClient | None]:
    """SIS, ReCodEx and Owl readers for the secrets that are set (SPEC §2.20), sharing one
    polite client that redacts all of them."""
    ical, key = _secret(settings.sis_ical_token), _secret(settings.sis_timetable_key)
    rx_ical, rx_api = _secret(settings.recodex_ical_token), _secret(settings.recodex_api_token)
    cookie = _secret(settings.owl_session_cookie)
    secrets = [s for s in (ical, key, rx_ical, rx_api, cookie) if s]
    if not secrets:
        return SchoolSources(), None
    client = PoliteClient(secrets)
    sources = SchoolSources(
        sis=SisHttpSource(
            client,
            settings.sis_base_url,
            ical_token=ical,
            timetable_key=key,
            feed_types=settings.sis_feed_types,
        )
        if ical or key
        else None,
        recodex=RecodexHttpSource(
            client, settings.recodex_base_url, ical_token=rx_ical, api_token=rx_api
        )
        if rx_ical or rx_api
        else None,
        owl=OwlHttpSource(client, settings.owl_base_url, cookie=cookie) if cookie else None,
    )
    return sources, client


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)
    sessions = make_sessionmaker(engine)
    usage_log = UsageLog()
    tracing.configure(settings.orbit_otel_exporter)
    price_overrides = {
        name: (p.input_eur_per_mtok, p.output_eur_per_mtok)
        for name, p in settings.claude_price_overrides.items()
    }

    def price(model: str, tokens_in: int, tokens_out: int) -> int:
        return cost_micro_eur(model, tokens_in, tokens_out, settings.usd_eur_rate, price_overrides)

    backend = settings.resolved_claude_backend
    api_client: ClaudeClient | None = (
        FakeClaude(on_usage=usage_log.add, owner_name=settings.owner_name)
        if backend == "fake"
        else ClaudeClient(
            settings.anthropic_api_key.get_secret_value(),
            settings.anthropic_model,
            on_usage=usage_log.add,
            fast_model=settings.anthropic_model_fast,
            deep_model=settings.anthropic_model_deep,
            deep_effort=None
            if settings.anthropic_deep_effort == "off"
            else settings.anthropic_deep_effort,
            owner_name=settings.owner_name,
            pricer=price,
            budget_guard=BudgetGuard(
                sessions, usage_log, settings.claude_monthly_budget_eur, settings.timezone
            ),
        )
        if backend == "api" and settings.anthropic_api_key and settings.anthropic_model
        else None
    )
    claude: ClaudeClient | JobClaudeClient | None = api_client
    if backend == "worker":
        claude = JobClaudeClient(
            sessions,
            QueueLimits(
                wait_interactive=settings.claude_wait_interactive_seconds,
                wait_background=settings.claude_wait_background_seconds,
                cap_interactive=settings.claude_queue_cap_interactive,
                cap_background=settings.claude_queue_cap_background,
            ),
            owner_name=settings.owner_name,
        )
    ntfy = (
        NtfyClient(
            settings.ntfy_url,
            settings.ntfy_topic,
            settings.ntfy_token.get_secret_value() if settings.ntfy_token else None,
        )
        if settings.ntfy_topic
        else None
    )

    calendar_source = (
        CalDavCalendarSource(
            settings.caldav_url,
            settings.caldav_username,
            settings.caldav_password.get_secret_value(),
            settings.caldav_calendars,
            settings.timezone,
        )
        if settings.caldav_username and settings.caldav_password and settings.caldav_calendars
        else None
    )
    rates = EcbRates()
    fetcher = HttpFetcher()
    school_sources, school_client = build_school_sources(settings)
    workout_source = build_workout_source(settings)
    # Env values plus what was edited on /settings/schedule (SPEC §2.21).
    overrides = OverrideStore(sessions, settings)
    jobs = JobContext(
        sessions,
        overrides.effective,
        ntfy,
        calendar_source,
        claude,
        api_client,
        usage_log,
        school_sources,
        fetcher,
        workouts=workout_source,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        scheduler = (
            start_scheduler(
                sessions,
                ntfy,
                settings,
                calendar=calendar_source,
                claude=claude,
                reviewer=api_client,
                usage_log=usage_log,
                school=school_sources,
                context=jobs,
            )
            if settings.scheduler_enabled
            else None
        )
        yield
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        flush_usage(sessions, usage_log, ntfy, settings)
        tracing.shutdown()
        if ntfy is not None:
            ntfy.close()
        if claude is not None:
            claude.close()
        rates.close()
        fetcher.close()
        if school_client is not None:
            school_client.close()
        if workout_source is not None:
            workout_source.close()
        engine.dispose()

    # No public OpenAPI docs: this is a private, single-user instance.
    app = FastAPI(title="Orbit", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = sessions
    app.state.claude = claude
    # Only the API client makes the review inside Orbit; the worker uses prepare/submit.
    app.state.reviewer = api_client
    # Custom reports (SPEC §2.22) take the same path: the API client, else the worker, else plain.
    app.state.report_writer = api_client
    app.state.push_sender = ntfy
    app.state.rates = rates
    app.state.calendar = calendar_source
    app.state.school = school_sources
    app.state.usage_log = usage_log
    app.state.overrides = overrides
    app.state.jobs = jobs

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.orbit_secret_key.get_secret_value(),
        session_cookie="orbit_session",
        max_age=settings.session_max_age_days * 24 * 3600,
        same_site="lax",
        https_only=settings.orbit_url.startswith("https://"),
    )
    app.mount("/static", VersionedStaticFiles(directory=STATIC_DIR), name="static")
    _register_error_handlers(app)

    app.include_router(health.router)
    app.include_router(actions.router)
    app.include_router(calendar.router)
    app.include_router(build_api_router())
    app.include_router(review_agent.router)
    app.include_router(claude_worker.router)
    app.include_router(auth.router)
    app.include_router(pages.router)
    app.include_router(duplicates.router)
    app.include_router(people.router)
    app.include_router(contacts.router)
    app.include_router(ledger.router)
    app.include_router(inbox.router)
    app.include_router(bank.router)
    app.include_router(meetings.router)
    app.include_router(goals.router)
    app.include_router(school_map.router)  # /school/map before the /school/{...} pages
    app.include_router(school.router)
    app.include_router(fitness.router)
    app.include_router(health_days.router)
    app.include_router(reviews.router)
    app.include_router(reports.router)
    app.include_router(claude_ops.router)
    app.include_router(schedule_web.router)
    app.include_router(context.router)
    return app
