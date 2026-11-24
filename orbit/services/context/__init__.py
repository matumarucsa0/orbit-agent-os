"""Context providers (one per module) and the shared context layer that combines them.

Agents call `build_context`; they never read a provider or a table directly. A new module adds
its provider to `PROVIDERS` (and to the snapshot in `snapshot.py`).
"""

from orbit.services.context.base import ContextFocus, ContextProvider, ContextWindow, ModuleContext
from orbit.services.context.fitness import FitnessProvider
from orbit.services.context.goals import GoalsProvider
from orbit.services.context.health import HealthProvider
from orbit.services.context.ledger import LedgerProvider
from orbit.services.context.meetings import MeetingsProvider
from orbit.services.context.people import PeopleProvider
from orbit.services.context.promises import PromisesProvider
from orbit.services.context.school import SchoolProvider
from orbit.services.context.snapshot import (
    DEFAULT_BUDGET_CHARS,
    PROVIDERS,
    BudgetReport,
    ContextSnapshot,
    FocusInfo,
    WindowInfo,
    build_context,
)
from orbit.services.context.windows import (
    DEFAULT_PRESET,
    PRESETS,
    preset_window,
    resolve_window,
)

__all__ = [
    "DEFAULT_BUDGET_CHARS",
    "DEFAULT_PRESET",
    "PRESETS",
    "PROVIDERS",
    "BudgetReport",
    "ContextFocus",
    "ContextProvider",
    "ContextSnapshot",
    "ContextWindow",
    "FitnessProvider",
    "FocusInfo",
    "GoalsProvider",
    "HealthProvider",
    "LedgerProvider",
    "MeetingsProvider",
    "ModuleContext",
    "PeopleProvider",
    "PromisesProvider",
    "SchoolProvider",
    "WindowInfo",
    "build_context",
    "preset_window",
    "resolve_window",
]
