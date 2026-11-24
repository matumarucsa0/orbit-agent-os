"""The school map (Phase 18c, SPEC §2.20): a data flow map of every source behind the classes.

Orbit computes the layout here and serves it as JSON (`GET /school/map.json`); the page draws
cards as HTML and edges as one inline SVG (DECISIONS #14). Four columns, left to right: sources
(the systems, then the resources of each course), processing, courses, and what Orbit makes of
them. Edge width grows with volume; state is shown in words and an icon, never colour alone.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orbit.domain.enums import (
    FetchSource,
    FetchStatus,
    ProposalKind,
    ProposalStatus,
    ResourceKind,
    ResourceOrigin,
    ResourceStatus,
    SchoolEventKind,
)
from orbit.domain.inbox import Proposal
from orbit.domain.school import Lesson, SchoolEvent, SchoolResource, SisFetch
from orbit.services import school, school_reader, school_resources
from orbit.services.school_sync import SchoolSources

COLUMN_X = (0, 300, 600, 900)
CARD_W = 220
CARD_H = 58
GAP = 14
GROUP_GAP = 30
TOP = 20
COLORS = 8  # the page defines --course-0 .. --course-7
STALE_AFTER = timedelta(days=3)

STATE_LABELS = {
    "connected": ("●", "connected"),
    "changed": ("◆", "changed today"),
    "suggested": ("◌", "suggested"),
    "login": ("🔒", "login only"),
    "stale": ("◷", "stale"),
    "blocked": ("⛔", "blocked"),
    "queued": ("⏳", "queued"),
    "off": ("○", "not set up"),
}
KIND_ICONS = {
    ResourceKind.PAGE: "🌐",
    ResourceKind.PDF: "📄",
    ResourceKind.FILE: "📎",
    ResourceKind.GITHUB: "🐙",
    ResourceKind.NOTE: "📝",
    ResourceKind.LOGIN: "🔒",
}
ORIGIN_LABELS = {
    ResourceOrigin.SIS: "from SIS",
    ResourceOrigin.OWL: "from Owl",
    ResourceOrigin.TICKET: "from a SIS ticket",
    ResourceOrigin.CLAUDE: "found by Claude",
    ResourceOrigin.MANUAL: "added by you",
}


@dataclass
class Node:
    id: str
    col: int
    kind: str  # system, resource, process, course, output
    icon: str
    label: str
    sub: str = ""
    state: str = "connected"
    course: str | None = None  # course code, for tracing and colour
    color: int | None = None
    x: int = 0
    y: int = 0
    w: int = CARD_W
    h: int = CARD_H
    progress: float | None = None  # 0..1 toward credit, on course cards

    def __post_init__(self) -> None:
        self.x = COLUMN_X[self.col]

    def as_json(self) -> dict[str, Any]:
        icon, label = STATE_LABELS[self.state]
        return {
            "id": self.id,
            "col": self.col,
            "kind": self.kind,
            "icon": self.icon,
            "label": self.label,
            "sub": self.sub,
            "state": self.state,
            "state_icon": icon,
            "state_label": label,
            "course": self.course,
            "color": self.color,
            "x": self.x,
            "y": self.y,
            "w": self.w,
            "h": self.h,
            "progress": self.progress,
        }


@dataclass
class Edge:
    source: str
    target: str
    volume: int = 1
    style: str = (
        "solid"  # solid, dashed (suggested or queued), blocked, confirm (after you confirm)
    )
    course: str | None = None
    color: int | None = None
    label: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "id": f"{self.source}>{self.target}",
            "from": self.source,
            "to": self.target,
            "volume": self.volume,
            "width": round(1.5 + 2.2 * math.log10(1 + max(self.volume, 0)), 2),
            "style": self.style,
            "course": self.course,
            "color": self.color,
            "label": self.label,
            "flowing": self.style == "solid" and self.volume > 0,
        }


@dataclass
class SchoolMap:
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)

    def node(self, node_id: str) -> Node | None:
        return next((n for n in self.nodes if n.id == node_id), None)

    def as_json(self) -> dict[str, Any]:
        width = COLUMN_X[-1] + CARD_W + TOP
        height = max((n.y + n.h for n in self.nodes), default=0) + TOP
        return {
            "width": width,
            "height": height,
            "nodes": [n.as_json() for n in self.nodes],
            "edges": [e.as_json() for e in self.edges],
        }


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _system_state(
    session: Session, sources: list[FetchSource], configured: bool, now: datetime, tz: ZoneInfo
) -> str:
    if not configured:
        return "off"
    rows = [
        row
        for row in (
            session.scalars(
                select(SisFetch)
                .where(SisFetch.source == source)
                .order_by(SisFetch.id.desc())
                .limit(1)
            ).first()
            for source in sources
        )
        if row is not None
    ]
    if not rows:
        return "queued"
    if any(r.status in (FetchStatus.DENIED, FetchStatus.ERROR) for r in rows):
        return "blocked"
    today = datetime.combine(now.astimezone(tz).date(), time.min, tz).astimezone(UTC)
    if any(r.items_changed and r.started_at >= today for r in rows):
        return "changed"
    if all(now - r.started_at > STALE_AFTER for r in rows):
        return "stale"
    return "connected"


def resource_state(resource: SchoolResource, now: datetime, tz: ZoneInfo) -> str:
    if resource.kind is ResourceKind.LOGIN:
        return "login"
    if not resource.kept:
        return "suggested"
    if resource.status in (ResourceStatus.ERROR, ResourceStatus.BROKEN):
        return "blocked"
    today = datetime.combine(now.astimezone(tz).date(), time.min, tz).astimezone(UTC)
    if (
        resource.status is ResourceStatus.CHANGED
        and resource.changed_at
        and resource.changed_at >= today
    ):
        return "changed"
    if (
        resource.text
        and resource.changed_at
        and (resource.read_at is None or resource.read_at < resource.changed_at)
    ):
        return "queued"
    if resource.kind in (ResourceKind.PAGE, ResourceKind.PDF, ResourceKind.GITHUB) and (
        resource.checked_at is None or now - resource.checked_at > STALE_AFTER
    ):
        return "stale"
    return "connected"


def _stack(nodes: list[Node], start: int = TOP) -> int:
    """Place nodes under each other in the order given; returns the next free y."""
    y = start
    for node in nodes:
        node.y = y
        y += node.h + GAP
    return y


def _place_near(nodes: list[tuple[Node, float]]) -> None:
    """Put each node as close to its wanted y as possible without overlap (order kept)."""
    floor = TOP
    for node, wanted in sorted(nodes, key=lambda pair: pair[1]):
        node.y = max(int(wanted), floor)
        floor = node.y + node.h + GAP


def build_map(
    session: Session,
    sources: SchoolSources,
    now: datetime,
    tz: ZoneInfo,
    reads_per_day: int,
) -> SchoolMap:
    result = SchoolMap()
    courses = school.current_courses(session)
    colors = {c.code: i % COLORS for i, c in enumerate(courses)}
    code_of = {c.id: c.code for c in courses}
    scores = school.scores(session, [c.id for c in courses])

    lesson_rows = session.execute(
        select(Lesson.course_id, func.count(Lesson.id))
        .where(Lesson.course_id.is_not(None), Lesson.cancelled.is_(False))
        .group_by(Lesson.course_id)
    ).all()
    event_rows = session.execute(
        select(SchoolEvent.course_id, func.count(SchoolEvent.id))
        .where(
            SchoolEvent.course_id.is_not(None),
            SchoolEvent.cancelled.is_(False),
            SchoolEvent.kind.in_((SchoolEventKind.EXAM, SchoolEventKind.DEADLINE)),
        )
        .group_by(SchoolEvent.course_id)
    ).all()
    lesson_counts = {cid: n for cid, n in lesson_rows if cid is not None}
    event_counts = {cid: n for cid, n in event_rows if cid is not None}
    pending: dict[int | None, int] = {}
    for payload in session.scalars(
        select(Proposal.payload_json).where(
            Proposal.status == ProposalStatus.PENDING,
            Proposal.kind.in_((ProposalKind.SCHOOL_DEADLINE, ProposalKind.COURSE_FACT)),
        )
    ):
        key = payload.get("course_id")
        pending[key if isinstance(key, int) else None] = (
            pending.get(key if isinstance(key, int) else None, 0) + 1
        )
    total_pending = sum(pending.values())

    # --- Column 0: systems, then resources by course ---
    systems = [
        Node(
            "sys:sis",
            0,
            "system",
            "🎓",
            "SIS",
            "timetable, exams, harmonogram",
            _system_state(
                session,
                [
                    FetchSource.ROZ_EXTERNI,
                    FetchSource.ROZVRHNG,
                    FetchSource.TERM_ST2,
                    FetchSource.STEV,
                ],
                bool(sources.sis and sources.sis.feeds),
                now,
                tz,
            ),
        ),
        Node(
            "sys:recodex",
            0,
            "system",
            "💻",
            "ReCodEx",
            "deadlines and points",
            _system_state(
                session,
                [FetchSource.RECODEX, FetchSource.RECODEX_API],
                sources.recodex is not None,
                now,
                tz,
            ),
        ),
        Node(
            "sys:owl",
            0,
            "system",
            "🦉",
            "Owl",
            "topics, points, course rules",
            _system_state(session, [FetchSource.OWL], sources.owl is not None, now, tz),
        ),
    ]
    result.nodes += systems
    y = _stack(systems)

    resources = school_resources.list_resources(session)
    by_course: dict[str | None, list[SchoolResource]] = {}
    for r in resources:
        code = code_of.get(r.course_id) if r.course_id is not None else None
        by_course.setdefault(code, []).append(r)
    order = [c.code for c in courses if c.code in by_course] + ([None] if None in by_course else [])
    resource_nodes: list[tuple[Node, SchoolResource]] = []
    for code in order:
        y += GROUP_GAP - GAP
        group = []
        for r in by_course[code]:
            node = Node(
                f"res:{r.id}",
                0,
                "resource",
                KIND_ICONS[r.kind],
                r.label,
                ORIGIN_LABELS[r.origin],
                resource_state(r, now, tz),
                code,
                colors.get(code) if code else None,
            )
            group.append(node)
            resource_nodes.append((node, r))
        y = _stack(group, y)
        result.nodes += group

    # --- Column 1: processing ---
    reads = school_reader.reads_today(session, now, tz)
    sync = Node(
        "proc:sync",
        1,
        "process",
        "🔄",
        "Sync",
        "SIS · ReCodEx · Owl",
        "connected" if any(s.state != "off" for s in systems) else "off",
    )
    watcher = Node(
        "proc:watcher",
        1,
        "process",
        "👁",
        "Page watcher",
        _plural(sum(1 for _, r in resource_nodes if r.kept), "kept resource"),
        "connected" if resource_nodes else "off",
    )
    reader = Node(
        "proc:reader",
        1,
        "process",
        "🤖",
        "Claude reader",
        f"{reads} of {reads_per_day} reads today",
        "queued" if school_reader.pending_reads(session) else "connected",
    )
    inbox = Node(
        "proc:inbox",
        1,
        "process",
        "📥",
        "Inbox",
        _plural(total_pending, "proposal") + " waiting",
        "queued" if total_pending else "connected",
    )
    processing = [sync, watcher, reader, inbox]
    result.nodes += processing

    # --- Column 2: courses ---
    course_nodes = []
    for c in courses:
        gained = sum(float(s.gained) for s in scores.get(c.id, [])[:1])
        total = sum(float(s.total) for s in scores.get(c.id, [])[:1])
        node = Node(
            f"course:{c.code}",
            2,
            "course",
            "📚",
            f"{c.code} {c.name}",
            " · ".join(
                part
                for part in (
                    f"{c.credits} credits" if c.credits is not None else "",
                    c.completion or "",
                    f"{gained:g}/{total:g} points" if total else "",
                )
                if part
            ),
            "connected",
            c.code,
            colors[c.code],
            progress=(gained / total if total else None),
        )
        course_nodes.append(node)
    height = max(y, len(course_nodes) * (CARD_H + GAP) + TOP)
    step = (height - TOP) / max(len(course_nodes), 1)
    for i, node in enumerate(course_nodes):
        node.y = int(TOP + i * step)
    result.nodes += course_nodes

    # --- Column 3: outputs ---
    week = Node("out:week", 3, "output", "📅", "Week view", "lessons and deadlines")
    deadlines = Node("out:deadlines", 3, "output", "⏰", "Deadlines + pushes", "reminders, changes")
    credits = Node("out:credits", 3, "output", "🎯", "Credit progress", "points toward credit")
    review = Node("out:review", 3, "output", "🗓", "Weekly review", "reads the school module")
    outputs = [week, deadlines, credits, review]
    result.nodes += outputs
    _place_near([(o, TOP + i * ((height - TOP) / 4)) for i, o in enumerate(outputs)])

    # Processing cards sit next to what they read.
    middle = (height / 2) - CARD_H
    _place_near(
        [
            (sync, sum(s.y for s in systems) / len(systems)),
            (
                watcher,
                (sum(n.y for n, _ in resource_nodes) / len(resource_nodes))
                if resource_nodes
                else middle,
            ),
            (reader, middle + 80),
            (
                inbox,
                (sum(n.y for n in course_nodes) / len(course_nodes))
                if course_nodes
                else middle + 160,
            ),
        ]
    )

    # --- Edges ---
    for system, volume, label in (
        (systems[0], sum(lesson_counts.values()), "lessons"),
        (systems[1], sum(1 for _ in course_nodes), "courses"),
        (systems[2], sum(1 for _ in course_nodes), "courses"),
    ):
        style = {"off": "dashed", "blocked": "blocked"}.get(system.state, "solid")
        result.edges.append(
            Edge(system.id, sync.id, volume, style, label=_plural(volume, label[:-1], label))
        )
    for node, _resource in resource_nodes:
        style = (
            "dashed"
            if node.state in ("suggested", "queued")
            else "blocked"
            if node.state == "blocked"
            else "solid"
        )
        if node.state == "login":
            style = "dashed"
        result.edges.append(Edge(node.id, watcher.id, 1, style, node.course, node.color, "text"))
    result.edges.append(
        Edge(
            watcher.id,
            reader.id,
            sum(1 for _, r in resource_nodes if r.kept),
            "solid" if reader.state == "connected" else "dashed",
            label="changed text",
        )
    )
    result.edges.append(
        Edge(
            reader.id,
            inbox.id,
            total_pending,
            "dashed" if total_pending else "solid",
            label=_plural(total_pending, "proposal"),
        )
    )
    for c, node in zip(courses, course_nodes, strict=True):
        count = lesson_counts.get(c.id, 0) + event_counts.get(c.id, 0)
        result.edges.append(
            Edge(sync.id, node.id, count, "solid", c.code, node.color, _plural(count, "item"))
        )
        waiting = pending.get(c.id, 0)
        result.edges.append(
            Edge(
                inbox.id,
                node.id,
                waiting,
                "confirm",
                c.code,
                node.color,
                _plural(waiting, "proposal") + " after you confirm",
            )
        )
        for out, label in (
            (week, "lessons"),
            (deadlines, "deadlines"),
            (credits, "credit"),
            (review, "context"),
        ):
            volume = (
                lesson_counts.get(c.id, 0)
                if out is week
                else event_counts.get(c.id, 0)
                if out is deadlines
                else 1
            )
            result.edges.append(Edge(node.id, out.id, volume, "solid", c.code, node.color, label))
    return result
