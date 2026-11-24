"""`/api/v1` — every route below requires the bearer token."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from orbit.api import (
    contacts,
    context,
    export,
    goals,
    health_days,
    imports,
    inbox,
    intake,
    interactions,
    ledger,
    meetings,
    people,
    reports,
    reviews,
    schedule,
    school,
    school_resources,
)
from orbit.api.deps import require_api_token


def build_api_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_token)])
    router.include_router(people.router)
    router.include_router(interactions.router)
    router.include_router(imports.router)
    router.include_router(contacts.router)
    router.include_router(ledger.router)
    router.include_router(inbox.router)
    router.include_router(intake.router)
    router.include_router(goals.router)
    router.include_router(health_days.router)
    router.include_router(reviews.router)
    router.include_router(reports.router)
    router.include_router(meetings.router)
    router.include_router(school.router)
    router.include_router(school_resources.router)
    router.include_router(schedule.router)
    router.include_router(context.router)
    router.include_router(export.router)
    return router
