"""School resources (Phase 18c, SPEC §2.20): pages, PDFs, repositories and notes that belong to a
course. Orbit fetches and extracts the text, keeps the old text so a change can be shown, and
hands only text to the reader (`school_reader.py`). Callers commit.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit.domain.base import utcnow
from orbit.domain.enums import ResourceKind, ResourceOrigin, ResourceStatus
from orbit.domain.school import Course, CourseFact, OwlCourse, SchoolResource
from orbit.integrations import resources as web
from orbit.integrations.resources import ResourceError, ResourceFetcher
from orbit.services.errors import InvalidInputError, NotFoundError

MAX_TEXT = 200_000  # characters kept per resource
MAX_DIFF = 8_000
MAX_FILE_BYTES = 20 * 1024 * 1024
BROKEN_AFTER = 7  # days in a row
MAX_PER_RUN = 60  # resources one run checks (the rest wait for the next one)
LABEL_CHARS = 300
NOTE_CHARS = 20_000
FETCHED = (ResourceKind.PAGE, ResourceKind.PDF, ResourceKind.GITHUB)


# --- Queries ---


def get_resource(session: Session, resource_id: int) -> SchoolResource:
    resource = session.get(SchoolResource, resource_id)
    if resource is None:
        raise NotFoundError(f"Resource {resource_id} not found.")
    return resource


def list_resources(
    session: Session, course_id: int | None = None, *, suggestions: bool = True
) -> list[SchoolResource]:
    """Resources by course, kept ones first. Dismissed suggestions are never listed."""
    query = select(SchoolResource).where(SchoolResource.dismissed.is_(False))
    if course_id is not None:
        query = query.where(SchoolResource.course_id == course_id)
    if not suggestions:
        query = query.where(SchoolResource.kept.is_(True))
    return list(session.scalars(query.order_by(SchoolResource.kept.desc(), SchoolResource.id)))


def _exists(session: Session, url: str) -> bool:
    return (
        session.scalars(select(SchoolResource.id).where(SchoolResource.url == url)).first()
        is not None
    )


def _label(url: str) -> str:
    parts = urlsplit(url)
    return (parts.netloc + parts.path.rstrip("/"))[:LABEL_CHARS]


def _check_course(session: Session, course_id: int | None) -> None:
    if course_id is not None and session.get(Course, course_id) is None:
        raise InvalidInputError("That course does not exist.")


# --- Adding, keeping, removing ---


def add_url(
    session: Session,
    url: str,
    course_id: int | None = None,
    *,
    label: str | None = None,
    origin: ResourceOrigin = ResourceOrigin.MANUAL,
    reason: str | None = None,
) -> SchoolResource:
    """A web address as a resource. By hand it is kept at once; any other origin starts as a
    suggestion. The kind comes from the address; a login page is listed and never fetched."""
    try:
        clean = web.check_url(url)
    except ResourceError as exc:
        raise InvalidInputError(str(exc)) from exc
    _check_course(session, course_id)
    if _exists(session, clean):
        raise InvalidInputError("That address is already in your resources.")
    kind = ResourceKind(web.detect_kind(clean))
    manual = origin is ResourceOrigin.MANUAL
    resource = SchoolResource(
        course_id=course_id,
        kind=kind,
        origin=origin,
        url=clean,
        label=(" ".join((label or "").split()) or _label(clean))[:LABEL_CHARS],
        kept=manual,
        status=ResourceStatus.LOGIN if kind is ResourceKind.LOGIN else ResourceStatus.OK,
        suggested_reason=reason[:500] if reason else None,
    )
    session.add(resource)
    session.flush()
    return resource


def add_note(
    session: Session,
    text: str,
    course_id: int | None = None,
    label: str | None = None,
    now: datetime | None = None,
) -> SchoolResource:
    """Text I paste (rules said in class). Read once, like a new page."""
    body = web.normalize_text(text)
    if not body:
        raise InvalidInputError("The note is empty.")
    if len(body) > NOTE_CHARS:
        raise InvalidInputError(f"The note is longer than {NOTE_CHARS} characters.")
    _check_course(session, course_id)
    now = now or utcnow()
    resource = SchoolResource(
        course_id=course_id,
        kind=ResourceKind.NOTE,
        origin=ResourceOrigin.MANUAL,
        label=(" ".join((label or "").split()) or body.splitlines()[0])[:LABEL_CHARS],
        kept=True,
        status=ResourceStatus.OK,
        text=body,
        content_hash=hashlib.sha256(body.encode()).hexdigest(),
        checked_at=now,
        changed_at=now,
    )
    session.add(resource)
    session.flush()
    return resource


def file_path(files_dir: str | Path, sha256: str) -> Path:
    return Path(files_dir) / f"{sha256}.pdf"


def add_file(
    session: Session,
    data: bytes,
    filename: str,
    course_id: int | None,
    files_dir: str | Path,
    now: datetime | None = None,
) -> SchoolResource:
    """An uploaded PDF (at most 20 MB), stored under its hash; its text is read at once."""
    if len(data) > MAX_FILE_BYTES:
        raise InvalidInputError("The file is larger than 20 MB.")
    if not data.startswith(b"%PDF"):
        raise InvalidInputError("Only PDF files can be added.")
    _check_course(session, course_id)
    try:
        text = web.pdf_to_text(data)
    except ResourceError as exc:
        raise InvalidInputError(str(exc)) from exc
    sha = hashlib.sha256(data).hexdigest()
    existing = session.scalars(
        select(SchoolResource).where(SchoolResource.file_sha256 == sha)
    ).first()
    if existing is not None:
        raise InvalidInputError("That file is already in your resources.")
    target = file_path(files_dir, sha)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    now = now or utcnow()
    clipped = text[:MAX_TEXT]
    resource = SchoolResource(
        course_id=course_id,
        kind=ResourceKind.FILE,
        origin=ResourceOrigin.MANUAL,
        file_sha256=sha,
        label=(Path(filename).stem or "PDF")[:LABEL_CHARS],
        kept=True,
        status=ResourceStatus.OK if clipped else ResourceStatus.NO_TEXT,
        text=clipped or None,
        content_hash=hashlib.sha256(clipped.encode()).hexdigest() if clipped else None,
        checked_at=now,
        changed_at=now if clipped else None,
    )
    session.add(resource)
    session.flush()
    return resource


def keep(session: Session, resource_id: int) -> SchoolResource:
    """Keep a suggestion: from now on it is fetched and read."""
    resource = get_resource(session, resource_id)
    resource.kept = True
    resource.dismissed = False
    session.flush()
    return resource


def dismiss(session: Session, resource_id: int) -> SchoolResource:
    """Turn a suggestion down for good (it stays as a row so it is never suggested again)."""
    resource = get_resource(session, resource_id)
    if resource.kept:
        raise InvalidInputError("Only a suggestion can be dismissed; remove a kept resource.")
    resource.dismissed = True
    session.flush()
    return resource


def remove(session: Session, resource_id: int, files_dir: str | Path | None = None) -> None:
    """Delete a kept resource (and its stored file when nothing else uses it)."""
    resource = get_resource(session, resource_id)
    sha = resource.file_sha256
    session.delete(resource)
    session.flush()
    if sha and files_dir is not None:
        file_path(files_dir, sha).unlink(missing_ok=True)


def link_course(session: Session, resource_id: int, course_id: int | None) -> SchoolResource:
    resource = get_resource(session, resource_id)
    _check_course(session, course_id)
    resource.course_id = course_id
    session.flush()
    return resource


# --- Suggestions ---


def suggest_links(session: Session) -> int:
    """Suggest the course's own page (SIS "Další informace") and the links in an Owl course
    description. Nothing is fetched until I keep one. Returns how many were added."""
    added = 0
    candidates: list[tuple[str, int | None, ResourceOrigin, str]] = []
    for course in session.scalars(select(Course).where(Course.web_url.is_not(None))):
        assert course.web_url is not None
        candidates.append((course.web_url, course.id, ResourceOrigin.SIS, "Course page in SIS"))
    for owl in session.scalars(select(OwlCourse).where(OwlCourse.description.is_not(None))):
        assert owl.description is not None
        for url in web.links_in(owl.description):
            candidates.append(
                (url, owl.course_id, ResourceOrigin.OWL, "Link in the Owl course text")
            )
    for url, course_id, origin, reason in candidates:
        try:
            clean = web.check_url(url)
        except ResourceError:
            continue
        if _exists(session, clean):
            continue
        add_url(session, clean, course_id, origin=origin, reason=reason)
        added += 1
    return added


# --- Checking ---


def _diff(old: str, new: str) -> str:
    lines = [
        line
        for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0)
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]
    return "\n".join(lines)[:MAX_DIFF]


def _store_text(resource: SchoolResource, text: str, now: datetime) -> None:
    """Keep new text: a changed hash makes the old text `prev_text` and records the diff."""
    text = text[:MAX_TEXT]
    digest = hashlib.sha256(text.encode()).hexdigest() if text else None
    resource.fail_count = 0
    resource.error = None
    if not text:
        resource.status = ResourceStatus.NO_TEXT
        return
    if digest == resource.content_hash:
        resource.status = ResourceStatus.UNCHANGED
        return
    first = resource.content_hash is None
    resource.prev_text = None if first else resource.text
    resource.last_diff = None if first else _diff(resource.text or "", text)
    resource.text = text
    resource.content_hash = digest
    resource.changed_at = now
    resource.status = ResourceStatus.OK if first else ResourceStatus.CHANGED


def _fail(resource: SchoolResource, message: str, now: datetime) -> None:
    """A failed check counts once a day; seven days in a row make the resource `broken`."""
    new_day = resource.checked_at is None or resource.checked_at.date() != now.date()
    if new_day or resource.fail_count == 0:
        resource.fail_count += 1
    resource.error = message[:500]
    resource.status = (
        ResourceStatus.BROKEN if resource.fail_count >= BROKEN_AFTER else ResourceStatus.ERROR
    )


def _fetch_github(url: str, fetcher: ResourceFetcher) -> str:
    owner, repo = web.github_repo(url)
    readme = fetcher.fetch(f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/README.md")
    commits = fetcher.fetch(f"https://github.com/{owner}/{repo}/commits/HEAD.atom")
    if readme.status == "error" and commits.status == "error":
        raise ResourceError(commits.error or readme.error or "Could not read the repository.")
    parts = []
    if readme.status == "ok":
        parts.append("README\n" + web.normalize_text(web.decode(readme.body, readme.content_type)))
    if commits.status == "ok":
        parts.append("Recent commits\n" + web.atom_to_text(commits.body))
    return "\n\n".join(parts)


def check(
    session: Session,
    resource: SchoolResource,
    fetcher: ResourceFetcher,
    now: datetime | None = None,
) -> ResourceStatus:
    """Fetch one resource and update its text, hash, diff and status. Never raises for a bad
    page: the failure is recorded on the row."""
    moment = now or utcnow()
    if resource.kind in (ResourceKind.NOTE, ResourceKind.FILE):
        return resource.status
    if resource.kind is ResourceKind.LOGIN or resource.url is None:
        resource.status = ResourceStatus.LOGIN
        return resource.status
    try:
        if resource.kind is ResourceKind.GITHUB:
            _store_text(resource, _fetch_github(resource.url, fetcher), moment)
        else:
            result = fetcher.fetch(
                resource.url, etag=resource.etag, last_modified=resource.last_modified
            )
            if result.status == "error":
                raise ResourceError(result.error or "Could not fetch it.")
            if result.status == "not_modified":
                resource.status = ResourceStatus.UNCHANGED
                resource.fail_count = 0
                resource.error = None
            else:
                resource.etag = result.etag
                resource.last_modified = result.last_modified
                is_pdf = web.detect_kind(resource.url, result.content_type) == "pdf" or (
                    result.body.startswith(b"%PDF")
                )
                text = (
                    web.pdf_to_text(result.body)
                    if is_pdf
                    else web.html_to_text(result.body, result.content_type)
                )
                _store_text(resource, text, moment)
    except ResourceError as exc:
        _fail(resource, str(exc), moment)
    resource.checked_at = moment
    session.flush()
    return resource.status


@dataclass
class CheckReport:
    checked: int = 0
    changed: int = 0
    failed: int = 0
    waiting: int = 0  # left for the next run (more than MAX_PER_RUN due)


def due_resources(
    session: Session, now: datetime, tz: ZoneInfo, hour: int, *, force: bool = False
) -> list[SchoolResource]:
    """Kept resources that are fetched and not yet checked today (from `hour` local time on).
    `force` (Check now) takes every fetched one."""
    local = now.astimezone(tz)
    if not force and local.hour < hour:
        return []
    today = local.date()
    found = []
    for resource in session.scalars(
        select(SchoolResource)
        .where(SchoolResource.kept.is_(True), SchoolResource.kind.in_(FETCHED))
        .order_by(
            SchoolResource.checked_at.is_not(None), SchoolResource.checked_at, SchoolResource.id
        )
    ):
        if (
            force
            or resource.checked_at is None
            or resource.checked_at.astimezone(tz).date() < today
        ):
            found.append(resource)
    return found


def run_checks(
    session: Session,
    fetcher: ResourceFetcher,
    now: datetime,
    tz: ZoneInfo,
    hour: int,
    *,
    force: bool = False,
    only: Sequence[int] | None = None,
) -> CheckReport:
    due = due_resources(session, now, tz, hour, force=force)
    if only is not None:
        due = [r for r in due if r.id in set(only)]
    report = CheckReport(waiting=max(len(due) - MAX_PER_RUN, 0))
    for resource in due[:MAX_PER_RUN]:
        status = check(session, resource, fetcher, now)
        report.checked += 1
        report.changed += status is ResourceStatus.CHANGED
        report.failed += status in (ResourceStatus.ERROR, ResourceStatus.BROKEN)
    return report


_ZERO_WIDTH = re.compile("[​﻿]")


def preview(resource: SchoolResource, limit: int = 600) -> str:
    """The first part of the stored text, for the side panel."""
    text = _ZERO_WIDTH.sub("", resource.text or "")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def delete_fact(session: Session, fact_id: int) -> None:
    """Remove a saved course rule."""
    fact = session.get(CourseFact, fact_id)
    if fact is None:
        raise NotFoundError(f"Rule {fact_id} not found.")
    session.delete(fact)
    session.flush()
