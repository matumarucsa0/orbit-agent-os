from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import Session

from orbit.domain.people import Person
from orbit.services.context import (
    DEFAULT_BUDGET_CHARS,
    PROVIDERS,
    ContextFocus,
    ContextWindow,
    build_context,
    preset_window,
    resolve_window,
)
from orbit.services.context import snapshot as snapshot_module
from orbit.services.context.base import ModuleContext
from orbit.services.context.promises import PromiseContext, PromisesContext
from orbit.services.context.snapshot import mask_strings
from orbit.services.errors import InvalidInputError
from tests.test_context_providers import (  # noqa: F401
    IBAN,
    PHONE,
    PRAGUE,
    WINDOW,
    person,
    promise,
    world,
)

NOW = datetime(2026, 9, 24, 8, 0, 30, 123456, tzinfo=UTC)


def snapshot(db: Session, focus: ContextFocus | None = None, **kwargs: int) -> dict[str, object]:
    built = build_context(db, WINDOW, focus, now=NOW, **kwargs)
    return json.loads(built.model_dump_json())


# --- Windows ---


def test_this_week_is_monday_to_sunday() -> None:
    window = preset_window("this_week", date(2026, 10, 7), PRAGUE)  # a Wednesday
    assert (window.start, window.end) == (date(2026, 10, 5), date(2026, 10, 11))
    assert preset_window("this_week", date(2026, 10, 5), PRAGUE).start == date(2026, 10, 5)
    assert preset_window("this_week", date(2026, 10, 11), PRAGUE).start == date(2026, 10, 5)


def test_next_7_days_starts_today() -> None:
    window = preset_window("next_7_days", date(2026, 10, 7), PRAGUE)
    assert (window.start, window.end) == (date(2026, 10, 7), date(2026, 10, 13))


def test_resolve_window() -> None:
    today = date(2026, 10, 7)
    assert resolve_window(today, PRAGUE).start == date(2026, 10, 5)  # default: this week
    assert resolve_window(today, PRAGUE, "next_7_days").start == today
    custom = resolve_window(today, PRAGUE, "this_week", date(2026, 9, 1), date(2026, 9, 3))
    assert (custom.start, custom.end) == (date(2026, 9, 1), date(2026, 9, 3))  # dates win


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"preset": "fortnight"}, "Unknown window"),
        ({"start": date(2026, 9, 1)}, "both a start and an end"),
        ({"end": date(2026, 9, 1)}, "both a start and an end"),
        ({"start": date(2026, 9, 2), "end": date(2026, 9, 1)}, "ends before it starts"),
        ({"start": date(2026, 1, 1), "end": date(2026, 12, 31)}, "at most 92 days"),
    ],
)
def test_resolve_window_refuses(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(InvalidInputError, match=message):
        resolve_window(date(2026, 10, 7), PRAGUE, **kwargs)


# --- The snapshot for a fixed week ---


def test_snapshot_for_a_fixed_week(db: Session, world: dict[str, Person]) -> None:  # noqa: F811
    data = snapshot(db)
    assert data["generated_at"] == "2026-09-24T08:00:30Z"
    assert data["window"] == {"start": "2026-09-24", "end": "2026-09-30", "tz": "Europe/Prague"}
    assert data["focus"] is None
    assert list(data)[:3] == ["generated_at", "window", "focus"]
    assert [m for m in data if isinstance(data[m], dict) and "module" in data[m]] == [
        "goals",  # first: the spine
        "school",
        "people",
        "ledger",
        "meetings",
        "promises",
        "fitness",
        "health",
    ]
    people, ledger, meetings, promises = (
        data[k] for k in ("people", "ledger", "meetings", "promises")
    )
    assert [p["name"] for p in people["people"]] == ["Patrik Veselý", "Terka"]
    assert ledger["totals"] == [{"currency": "EUR", "owed_to_me_minor": 2900, "i_owe_minor": 0}]
    assert [m["title"] for m in meetings["meetings"]] == ["kafe s Paťom ••• 456", "Gym"]
    assert [p["person"] for p in promises["promises"]] == ["Terka", "Patrik Veselý"]
    budget = data["budget"]
    assert isinstance(budget, dict)
    assert budget["limit_chars"] == 400_000
    assert 0 < budget["used_chars"] < 400_000
    assert budget["trimmed"] == {}


def test_used_chars_is_the_json_without_the_budget_report(
    db: Session,
    world: dict[str, Person],  # noqa: F811
) -> None:
    data = snapshot(db)
    budget = data.pop("budget")
    # The measure that is compared with the limit; dates and times are not escaped.
    assert budget["used_chars"] == len(
        json.dumps(
            {
                k: data[k]
                for k in (
                    "goals",
                    "school",
                    "people",
                    "ledger",
                    "meetings",
                    "promises",
                    "fitness",
                    "health",
                )
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


# --- Budget ---


def test_budget_trims_from_the_end_of_the_biggest_module(
    db: Session,
    world: dict[str, Person],  # noqa: F811
) -> None:
    for i in range(30):
        promise(db, world["terka"], f"thing number {i} to do soon", due=date(2026, 9, 29))
    full = snapshot(db)
    used = full["budget"]["used_chars"]
    data = snapshot(db, budget_chars=used - 300)
    trimmed = data["budget"]["trimmed"]
    assert set(trimmed) == {"promises"}  # the biggest list loses items first
    assert data["budget"]["used_chars"] <= used - 300
    kept = data["promises"]["promises"]
    assert len(kept) == len(full["promises"]["promises"]) - trimmed["promises"]
    assert kept == full["promises"]["promises"][: len(kept)]  # the tail went
    assert data["promises"]["truncated"] is True
    assert data["people"] == full["people"]  # other modules untouched


def test_budget_never_trims_totals_and_stops_when_nothing_is_left(
    db: Session,
    world: dict[str, Person],  # noqa: F811
) -> None:
    data = snapshot(db, budget_chars=1)
    assert data["ledger"]["totals"] == [
        {"currency": "EUR", "owed_to_me_minor": 2900, "i_owe_minor": 0}
    ]
    for module, field in (
        ("people", "people"),
        ("ledger", "debts"),
        ("ledger", "balances"),
        ("meetings", "meetings"),
        ("promises", "promises"),
    ):
        assert data[module][field] == []
    assert data["budget"]["used_chars"] > 1  # reported, not hidden
    assert all(data[m]["truncated"] for m in ("people", "ledger", "meetings", "promises"))


def test_budget_counts_what_was_cut(db: Session, world: dict[str, Person]) -> None:  # noqa: F811
    full = snapshot(db)
    data = snapshot(db, budget_chars=full["budget"]["used_chars"] - 1)
    assert sum(data["budget"]["trimmed"].values()) >= 1


def test_ties_go_to_the_earlier_module() -> None:
    parts = {
        "a": {"items": ["xx"], "truncated": False},
        "b": {"items": ["xx"], "truncated": False},
    }
    lists = {"a": ("items",), "b": ("items",)}
    assert snapshot_module._trim_once(parts, lists) == "a"
    assert parts["a"]["items"] == []
    assert snapshot_module._trim_once(parts, lists) == "b"
    assert snapshot_module._trim_once(parts, lists) is None


# --- Focus ---


def test_focus_filters_every_module(db: Session, world: dict[str, Person]) -> None:  # noqa: F811
    data = snapshot(db, ContextFocus(person_id=world["pato"].id))
    assert data["focus"] == {"person_id": world["pato"].id, "person_name": "Patrik Veselý"}
    blob = json.dumps(data, ensure_ascii=False)
    assert "Terka" not in blob
    assert "Gym" not in blob  # a meeting Pato isn't at


def test_focus_on_a_missing_person_is_empty_not_an_error(db: Session) -> None:
    data = snapshot(db, ContextFocus(person_id=999))
    assert data["focus"] == {"person_id": 999, "person_name": None}
    assert data["people"]["people"] == []
    assert data["ledger"]["debts"] == []
    assert data["budget"]["trimmed"] == {}


def test_focus_none_person_means_no_focus(db: Session, world: dict[str, Person]) -> None:  # noqa: F811
    assert snapshot(db, ContextFocus())["focus"] is None


# --- Masking ---


class LeakyProvider:
    """A provider that forgets to mask."""

    name = "promises"

    def build(
        self, session: Session, window: ContextWindow, focus: ContextFocus | None = None
    ) -> ModuleContext:
        return PromisesContext(
            promises=[
                PromiseContext(
                    id=1,
                    person_id=1,
                    person=f"Pato {PHONE}",
                    direction="i_promised",
                    text=f"pay {IBAN}",
                    due=None,
                    overdue=False,
                    due_in_window=False,
                )
            ],
            recently_closed=[],
        )


def test_the_layer_masks_what_a_provider_forgot(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    providers = tuple(LeakyProvider() if p.name == "promises" else p for p in PROVIDERS)
    monkeypatch.setattr(snapshot_module, "PROVIDERS", providers)
    blob = build_context(db, WINDOW, now=NOW).model_dump_json()
    assert IBAN not in blob
    assert "1987426375" not in blob
    assert "905 123" not in blob
    assert "••• 456" in blob


def test_focus_name_is_masked(db: Session) -> None:
    pato = person(db, f"Pato {PHONE}")
    data = snapshot(db, ContextFocus(person_id=pato.id))
    assert data["focus"]["person_name"] == "Pato ••• 456"


def test_masking_is_idempotent_and_leaves_dates_alone() -> None:
    sample = {
        "text": f"call {PHONE} or pay {IBAN}",
        "list": [f"{PHONE}", "2026-09-25", "2026-09-25T14:00:00+02:00", "2026-09-24T08:00:30Z"],
        "n": 3,
    }
    once = mask_strings(sample)
    assert mask_strings(once) == once
    assert once["list"][1:] == sample["list"][1:]
    assert PHONE not in json.dumps(once)
    assert IBAN not in json.dumps(once)


def test_a_busy_week_fits_the_default_budget_untrimmed(
    db: Session,
    world: dict[str, Person],  # noqa: F811
) -> None:
    for i in range(100):
        person(db, f"Person {i}", birthday_month=9, birthday_day=26)
        promise(db, world["terka"], f"thing {i} " + "x" * 500, due=date(2026, 9, 28))
    used = build_context(db, WINDOW, now=NOW).budget
    assert used.limit_chars == DEFAULT_BUDGET_CHARS == 400_000
    assert 0 < used.used_chars < used.limit_chars
    assert used.trimmed == {}
