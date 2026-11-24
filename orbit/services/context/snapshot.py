"""The shared context layer: every provider's summary in one snapshot, within a size budget.

`build_context` is the only way an agent reads module data. It runs the providers, masks every
string once more (a safety net: a provider that forgets still can't leak an IBAN or phone
number), then trims the snapshot to fit `budget_chars`. Read-only; nothing is stored.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from orbit.domain.people import Person
from orbit.integrations.claude import mask_sensitive
from orbit.services.context.base import (
    ContextFocus,
    ContextProvider,
    ContextWindow,
    ModuleContext,
)
from orbit.services.context.fitness import FitnessContext, FitnessProvider
from orbit.services.context.goals import GoalsContext, GoalsProvider
from orbit.services.context.health import HealthContext, HealthProvider
from orbit.services.context.ledger import LedgerContext, LedgerProvider
from orbit.services.context.meetings import MeetingsContext, MeetingsProvider
from orbit.services.context.people import PeopleContext, PeopleProvider
from orbit.services.context.promises import PromisesContext, PromisesProvider
from orbit.services.context.school import SchoolContext, SchoolProvider

DEFAULT_BUDGET_CHARS = 400_000  # about 100,000 tokens: generous on purpose (DECISIONS #7)

# Order matters: agents weigh earlier modules first, and ties in trimming go to the earlier one.
# A new module adds its provider here and its field to `ContextSnapshot`.
PROVIDERS: tuple[ContextProvider, ...] = (
    GoalsProvider(),
    SchoolProvider(),
    PeopleProvider(),
    LedgerProvider(),
    MeetingsProvider(),
    PromisesProvider(),
    FitnessProvider(),
    HealthProvider(),
)


def _empty_modules() -> dict[str, ModuleContext]:
    """What a module reads as when it was left out of a snapshot (`build_context(only=...)`)."""
    return {
        "goals": GoalsContext(goals=[]),
        "school": SchoolContext(),
        "people": PeopleContext(people=[]),
        "ledger": LedgerContext(totals=[], balances=[], debts=[], recently_settled=[]),
        "meetings": MeetingsContext(meetings=[]),
        "promises": PromisesContext(promises=[], recently_closed=[]),
        "fitness": FitnessContext(),
        "health": HealthContext(),
    }


class WindowInfo(BaseModel):
    start: date
    end: date
    tz: str


class FocusInfo(BaseModel):
    person_id: int
    person_name: str | None  # None: no such person, so the modules are empty


class BudgetReport(BaseModel):
    limit_chars: int
    # The snapshot's JSON without this report. Above the limit only when nothing was left to cut.
    used_chars: int
    trimmed: dict[str, int]  # module -> items cut to fit


class ContextSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    generated_at: datetime
    window: WindowInfo
    focus: FocusInfo | None
    goals: GoalsContext  # first: the spine everything else is weighed against
    school: SchoolContext  # the time and workload side of the week (SPEC §2.20)
    people: PeopleContext
    ledger: LedgerContext
    meetings: MeetingsContext
    promises: PromisesContext
    fitness: FitnessContext  # training load from Liftoff (SPEC §2.35)
    health: HealthContext  # sleep, heart rate, steps, weight from the watch (SPEC §2.36)
    budget: BudgetReport
    # Modules left out on purpose (their state is in another snapshot); empty here, not sent.
    omitted: tuple[str, ...] = ()

    def prompt_json(self) -> str:
        """The snapshot as sent to Claude: without the modules that were left out."""
        return self.model_dump_json(exclude=set(self.omitted) | {"omitted"})


def _compact(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def mask_strings(value: Any) -> Any:
    """`mask_sensitive` on every string in a JSON-like structure. Idempotent. Dates and times
    pass through unchanged (their separators never form a 9-digit run)."""
    if isinstance(value, str):
        return mask_sensitive(value)
    if isinstance(value, list):
        return [mask_strings(item) for item in value]
    if isinstance(value, dict):
        return {key: mask_strings(item) for key, item in value.items()}
    return value


def _trim_once(
    parts: dict[str, dict[str, Any]],
    lists: dict[str, tuple[str, ...]],
    in_order: frozenset[str] = frozenset(),
) -> str | None:
    """Cut the last item from the biggest list of the biggest module that has any (for a module
    in `in_order`: from its first non-empty list). Returns that module, or None when nothing
    can be cut."""
    best: tuple[int, int, str, str] | None = None  # (size, -module order, module, field)
    sizes = {name: len(_compact(part)) for name, part in parts.items()}
    for order, (name, fields) in enumerate(lists.items()):
        filled = [(len(_compact(parts[name][f])), f) for f in fields if parts[name][f]]
        if not filled:
            continue
        _, field = filled[0] if name in in_order else max(filled)
        candidate = (sizes[name], -order, name, field)
        if best is None or candidate[:2] > best[:2]:
            best = candidate
    if best is None:
        return None
    _, _, name, field = best
    parts[name][field] = parts[name][field][:-1]
    parts[name]["truncated"] = True
    return name


def build_context(
    session: Session,
    window: ContextWindow,
    focus: ContextFocus | None = None,
    *,
    budget_chars: int = DEFAULT_BUDGET_CHARS,
    now: datetime | None = None,
    only: Collection[str] | None = None,
) -> ContextSnapshot:
    """`only` names the modules to build; the others are left out (empty, and not in
    `prompt_json`). A review sends state that is true "now" once and only what depends on the
    week for the week before (SPEC §2.30)."""
    empty = _empty_modules()
    built: dict[str, ModuleContext] = {
        p.name: p.build(session, window, focus) if only is None or p.name in only else empty[p.name]
        for p in PROVIDERS
    }
    omitted = tuple(p.name for p in PROVIDERS if only is not None and p.name not in only)
    parts = {name: mask_strings(m.model_dump(mode="json")) for name, m in built.items()}
    lists = {name: m.trimmable for name, m in built.items()}
    in_order = frozenset(name for name, m in built.items() if m.trim_in_order)

    trimmed: dict[str, int] = {}
    while len(_compact(parts)) > budget_chars:
        module = _trim_once(parts, lists, in_order)
        if module is None:
            break
        trimmed[module] = trimmed.get(module, 0) + 1

    focus_info = None
    if focus is not None and focus.person_id is not None:
        person = session.get(Person, focus.person_id)
        name = mask_sensitive(person.display_name) if person is not None else None
        focus_info = FocusInfo(person_id=focus.person_id, person_name=name)
    moment = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    return ContextSnapshot(
        generated_at=moment,
        window=WindowInfo(start=window.start, end=window.end, tz=_tz_name(window.tz)),
        focus=focus_info,
        budget=BudgetReport(
            limit_chars=budget_chars, used_chars=len(_compact(parts)), trimmed=trimmed
        ),
        omitted=omitted,
        **parts,
    )


def _tz_name(tz: ZoneInfo) -> str:
    return tz.key or "UTC"
