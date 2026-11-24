from __future__ import annotations

from datetime import UTC, datetime, time

import pytest
from sqlalchemy.orm import Session

from orbit.db import make_engine, make_sessionmaker
from orbit.domain import Base
from orbit.domain.app_settings import AppSetting, AppSettingLog
from orbit.services import schedule_config as sc
from orbit.services.errors import InvalidInputError, NotFoundError
from orbit.settings import Settings

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def row(db: Session, settings: Settings, key: str) -> sc.SettingRow:
    return next(r for r in sc.rows(db, settings) if r.definition.key == key)


def test_every_definition_is_a_settings_field_with_a_valid_default(settings: Settings) -> None:
    keys = [d.key for d in sc.DEFINITIONS]
    assert len(keys) == len(set(keys))
    assert {d.group for d in sc.DEFINITIONS} <= set(sc.GROUPS)
    for defn in sc.DEFINITIONS:
        default = sc.canonical(defn, getattr(settings, defn.key))
        # the default round-trips and sits inside the bounds the page enforces
        assert sc.parse(defn, default) == default, defn.key
        assert sc.canonical(defn, sc.to_setting(defn, default)) == default, defn.key


@pytest.mark.parametrize(
    ("key", "raw", "expected"),
    [
        ("caldav_poll_minutes", "15", 15),
        ("caldav_poll_minutes", 30, 30),
        ("reminder_time", "8:05", "08:05"),
        ("weekly_review_enabled", "off", False),
        ("weekly_review_enabled", "on", True),
        ("digest_weekday", "4", 4),
        ("quiet_hours", "23:00 - 7:30", "23:00-07:30"),
        ("school_exam_days", "1, 7, 7, 3", [7, 3, 1]),
        ("school_exam_days", [2, 5], [5, 2]),
    ],
)
def test_parse_makes_the_canonical_form(key: str, raw: object, expected: object) -> None:
    assert sc.parse(sc.definition(key), raw) == expected


@pytest.mark.parametrize(
    ("key", "raw"),
    [
        ("sis_poll_minutes", "5"),  # at least 15
        ("caldav_poll_minutes", "0"),
        ("caldav_poll_minutes", "ten"),
        ("caldav_poll_minutes", True),
        ("reminder_time", "25:00"),
        ("reminder_time", "9"),
        ("weekly_review_weekday", "2"),  # Friday to Sunday only
        ("weekly_review_catch_up_weekday", "4"),  # Monday to Thursday only
        ("digest_weekday", "7"),
        ("quiet_hours", "22:00"),
        ("school_exam_days", ""),
        ("school_exam_days", "7, x"),
        ("school_exam_days", "0, 7"),
        ("weekly_review_enabled", "maybe"),
    ],
)
def test_parse_refuses_bad_values(key: str, raw: object) -> None:
    with pytest.raises(InvalidInputError):
        sc.parse(sc.definition(key), raw)


def test_unknown_key(db: Session, settings: Settings) -> None:
    with pytest.raises(NotFoundError):
        sc.definition("orbit_api_token")  # a secret is never editable
    with pytest.raises(NotFoundError):
        sc.save(db, settings, {"orbit_api_token": "x" * 40})
    with pytest.raises(NotFoundError):
        sc.reset(db, "nope")


def test_secrets_are_not_editable() -> None:
    secret_fields = {
        name
        for name, field in Settings.model_fields.items()
        if "SecretStr" in str(field.annotation)
    }
    assert secret_fields  # the check looks at something
    assert not secret_fields & {d.key for d in sc.DEFINITIONS}


def test_layering_override_beats_env(db: Session, make_settings) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(caldav_poll_minutes=20)
    assert row(db, settings, "caldav_poll_minutes").value == 20
    assert sc.save(db, settings, {"caldav_poll_minutes": "5"}, NOW) == ["caldav_poll_minutes"]
    got = row(db, settings, "caldav_poll_minutes")
    assert (got.value, got.default, got.overridden) == (5, 20, True)
    assert sc.apply_overrides(settings, sc.stored_overrides(db)).caldav_poll_minutes == 5
    assert settings.caldav_poll_minutes == 20  # the env copy is not touched


def test_save_stores_only_what_differs(db: Session, settings: Settings) -> None:
    assert sc.save(db, settings, {"caldav_poll_minutes": "10"}) == []  # equals the default
    assert db.query(AppSetting).count() == 0
    assert db.query(AppSettingLog).count() == 0
    sc.save(db, settings, {"caldav_poll_minutes": "5"})
    assert sc.save(db, settings, {"caldav_poll_minutes": "5"}) == []  # unchanged
    # Typing the env value again removes the override.
    assert sc.save(db, settings, {"caldav_poll_minutes": "10"}) == ["caldav_poll_minutes"]
    assert db.query(AppSetting).count() == 0


def test_save_is_all_or_nothing(db: Session, settings: Settings) -> None:
    with pytest.raises(InvalidInputError):
        sc.save(db, settings, {"caldav_poll_minutes": "5", "reminder_time": "late"})
    assert db.query(AppSetting).count() == 0


def test_changes_are_logged_and_reset_removes_the_override(db: Session, settings: Settings) -> None:
    sc.save(db, settings, {"quiet_hours": "23:00-07:00"}, NOW)
    sc.save(db, settings, {"quiet_hours": "23:30-07:00"}, NOW)
    assert sc.reset(db, "quiet_hours", NOW) is True
    assert sc.reset(db, "quiet_hours", NOW) is False
    log = sc.recent_changes(db)
    assert [(c.old_json, c.new_json) for c in log] == [
        ("23:30-07:00", None),
        ("23:00-07:00", "23:30-07:00"),
        (None, "23:00-07:00"),
    ]
    assert not row(db, settings, "quiet_hours").overridden


def test_effective_settings_have_the_types_the_services_expect(
    db: Session, settings: Settings
) -> None:
    sc.save(
        db,
        settings,
        {
            "quiet_hours": "23:00-07:30",
            "reminder_time": "10:15",
            "school_exam_days": "10, 2",
            "weekly_review_enabled": "off",
        },
    )
    effective = sc.apply_overrides(settings, sc.stored_overrides(db))
    assert effective.quiet_hours == (time(23, 0), time(7, 30))
    assert effective.reminder_time == time(10, 15)
    assert effective.school_exam_days == [10, 2]
    assert effective.weekly_review_enabled is False
    assert effective.school_deadline_days == settings.school_deadline_days


def test_a_stored_value_that_no_longer_parses_is_ignored(db: Session, settings: Settings) -> None:
    db.add(AppSetting(key="sis_poll_minutes", value_json=5, updated_at=NOW))  # below the minimum
    db.add(AppSetting(key="gone_in_this_version", value_json=1, updated_at=NOW))
    db.flush()
    effective = sc.apply_overrides(settings, sc.stored_overrides(db))
    assert effective.sis_poll_minutes == settings.sis_poll_minutes


def test_override_store_reads_the_database_and_caches(settings: Settings) -> None:
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = make_sessionmaker(engine)
    store = sc.OverrideStore(sessions, settings, ttl=3600)
    assert store.effective().caldav_poll_minutes == 10
    with sessions() as session:
        sc.save(session, settings, {"caldav_poll_minutes": "3"})
        session.commit()
    assert store.effective().caldav_poll_minutes == 10  # cached
    store.invalidate()
    assert store.effective().caldav_poll_minutes == 3
    # `apply` lays the overrides over any Settings (tests swap app.state.settings).
    other = settings.model_copy(update={"default_currency": "CZK"})
    applied = store.apply(other)
    assert (applied.default_currency, applied.caldav_poll_minutes) == ("CZK", 3)
    engine.dispose()


def test_override_store_survives_a_missing_table(settings: Settings) -> None:
    engine = make_engine("sqlite://")  # no tables: the migration has not run
    store = sc.OverrideStore(make_sessionmaker(engine), settings)
    assert store.effective() is settings
    engine.dispose()
