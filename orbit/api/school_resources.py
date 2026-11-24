"""School resources API (Phase 18c, SPEC §2.20): add a page, PDF or note to a course, keep or
dismiss a suggestion, check one now. `POST /school/resources` and `/file` are what an iOS
share-sheet Shortcut calls (docs/SHORTCUTS.md)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Request, Response, UploadFile, status
from pydantic import BaseModel, Field

from orbit.api.schemas import CourseRefOut
from orbit.deps import DbSession, SettingsDep
from orbit.domain.enums import ResourceKind, ResourceOrigin, ResourceStatus
from orbit.domain.school import SchoolResource
from orbit.services import school, school_reader, school_resources
from orbit.services.errors import InvalidInputError

router = APIRouter(prefix="/school/resources", tags=["school"])


class ResourceIn(BaseModel):
    url: str | None = Field(default=None, max_length=1000)
    note: str | None = Field(default=None, max_length=school_resources.NOTE_CHARS)
    course_code: str | None = Field(default=None, max_length=20)
    label: str | None = Field(default=None, max_length=300)


class ResourcePatch(BaseModel):
    course_code: str | None = None  # null: unlink


class ResourceOut(BaseModel):
    id: int
    course: CourseRefOut | None
    kind: ResourceKind
    origin: ResourceOrigin
    url: str | None
    label: str
    kept: bool
    status: ResourceStatus
    checked_at: datetime | None
    changed_at: datetime | None
    read_at: datetime | None
    fail_count: int
    error: str | None
    suggested_reason: str | None
    has_text: bool
    last_diff: str | None


def resource_out(row: SchoolResource) -> ResourceOut:
    return ResourceOut(
        id=row.id,
        course=CourseRefOut.model_validate(row.course) if row.course is not None else None,
        kind=row.kind,
        origin=row.origin,
        url=row.url,
        label=row.label,
        kept=row.kept,
        status=row.status,
        checked_at=row.checked_at,
        changed_at=row.changed_at,
        read_at=row.read_at,
        fail_count=row.fail_count,
        error=row.error,
        suggested_reason=row.suggested_reason,
        has_text=bool(row.text),
        last_diff=row.last_diff,
    )


def _course_id(db: DbSession, code: str | None) -> int | None:
    return school.get_course(db, code).id if code else None


@router.get("", response_model=list[ResourceOut])
def list_resources(db: DbSession, course: str | None = None) -> list[ResourceOut]:
    course_id = _course_id(db, course)
    return [resource_out(r) for r in school_resources.list_resources(db, course_id)]


@router.post("", response_model=ResourceOut, status_code=status.HTTP_201_CREATED)
def add_resource(body: ResourceIn, db: DbSession) -> ResourceOut:
    """A URL or a note (exactly one). The course is optional: without it the resource waits in
    "Unsorted" until linked."""
    if (body.url is None) == (body.note is None):
        raise InvalidInputError("Send either a url or a note.")
    course_id = _course_id(db, body.course_code)
    if body.url is not None:
        row = school_resources.add_url(db, body.url, course_id, label=body.label)
    else:
        assert body.note is not None
        row = school_resources.add_note(db, body.note, course_id, body.label)
    db.commit()
    return resource_out(row)


@router.post("/file", response_model=ResourceOut, status_code=status.HTTP_201_CREATED)
async def add_file(
    db: DbSession,
    settings: SettingsDep,
    file: Annotated[UploadFile, File()],
    course_code: Annotated[str | None, Form()] = None,
) -> ResourceOut:
    """A PDF (at most 20 MB), stored on the server; its text is read at once."""
    data = await file.read(school_resources.MAX_FILE_BYTES + 1)
    row = school_resources.add_file(
        db,
        data,
        file.filename or "file.pdf",
        _course_id(db, course_code),
        settings.school_files_dir,
    )
    db.commit()
    return resource_out(row)


@router.post("/{resource_id}/keep", response_model=ResourceOut)
def keep_resource(resource_id: int, db: DbSession) -> ResourceOut:
    row = school_resources.keep(db, resource_id)
    db.commit()
    return resource_out(row)


@router.post("/{resource_id}/dismiss", response_model=ResourceOut)
def dismiss_resource(resource_id: int, db: DbSession) -> ResourceOut:
    row = school_resources.dismiss(db, resource_id)
    db.commit()
    return resource_out(row)


@router.patch("/{resource_id}", response_model=ResourceOut)
def patch_resource(resource_id: int, body: ResourcePatch, db: DbSession) -> ResourceOut:
    row = school_resources.link_course(db, resource_id, _course_id(db, body.course_code))
    db.commit()
    return resource_out(row)


@router.post("/{resource_id}/check")
def check_resource(
    resource_id: int, db: DbSession, settings: SettingsDep, request: Request
) -> dict[str, Any]:
    """Fetch it now and let Claude read it if it is new or changed (within today's budget)."""
    jobs = request.app.state.jobs
    if jobs.fetcher is None:
        raise InvalidInputError("Page fetching is off.")
    message = school_reader.check_now(
        db,
        resource_id,
        jobs.fetcher,
        jobs.claude,
        datetime.now(UTC),
        settings.timezone,
        settings.school_reads_per_day,
    )
    db.commit()
    return {"id": resource_id, "result": message}


@router.delete("/{resource_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_resource(resource_id: int, db: DbSession, settings: SettingsDep) -> Response:
    school_resources.remove(db, resource_id, settings.school_files_dir)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
