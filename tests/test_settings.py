from __future__ import annotations

from datetime import time
from pathlib import Path

import pytest
from dotenv import dotenv_values
from pydantic import ValidationError

from orbit.settings import Settings

ROOT = Path(__file__).resolve().parents[1]


def test_defaults(settings: Settings) -> None:
    assert settings.timezone.key == "Europe/Prague"
    assert settings.quiet_hours == (time(22, 0), time(8, 0))
    assert settings.caldav_calendars == []
    assert settings.shared_min_amount_minor == 1500
    assert settings.default_currency == "EUR"
    assert settings.anthropic_model is None


@pytest.fixture
def required_env(monkeypatch: pytest.MonkeyPatch, password_hash: str) -> pytest.MonkeyPatch:
    monkeypatch.setenv("ORBIT_API_TOKEN", "a" * 40)
    monkeypatch.setenv("ORBIT_WEB_PASSWORD_HASH", password_hash)
    monkeypatch.setenv("ORBIT_SECRET_KEY", "b" * 40)
    monkeypatch.setenv("ICS_TOKEN", "c" * 40)
    return monkeypatch


def test_env_parsing(required_env: pytest.MonkeyPatch) -> None:
    required_env.setenv("QUIET_HOURS", "23:30-07:15")
    required_env.setenv("CALDAV_CALENDARS", "Personal, Uni ,,Work")
    required_env.setenv("SHARED_MIN_AMOUNT", "12.5")
    required_env.setenv("TIMEZONE", "Europe/Bratislava")
    required_env.setenv("ORBIT_URL", "https://orbit.example.com/")

    settings = Settings(_env_file=None)

    assert settings.quiet_hours == (time(23, 30), time(7, 15))
    assert settings.caldav_calendars == ["Personal", "Uni", "Work"]
    assert settings.shared_min_amount_minor == 1250
    assert settings.timezone.key == "Europe/Bratislava"
    assert settings.orbit_url == "https://orbit.example.com"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("QUIET_HOURS", "22-08"),
        ("QUIET_HOURS", "25:00-08:00"),
        ("SHARED_MIN_AMOUNT", "15.001"),
        ("SHARED_MIN_AMOUNT", "-1"),
        ("SHARED_MIN_AMOUNT", "abc"),
        ("SHARED_MIN_AMOUNT", "Infinity"),
        ("TIMEZONE", "Mars/Olympus"),
        ("DEFAULT_CURRENCY", "euro"),
        ("ORBIT_URL", "orbit.example.com"),
    ],
)
def test_invalid_values(required_env: pytest.MonkeyPatch, name: str, value: str) -> None:
    required_env.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_required_secrets_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ORBIT_API_TOKEN", "ORBIT_WEB_PASSWORD_HASH", "ORBIT_SECRET_KEY", "ICS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    missing = {err["loc"][0] for err in excinfo.value.errors()}
    assert missing == {
        "orbit_api_token",
        "orbit_web_password_hash",
        "orbit_secret_key",
        "ics_token",
    }


def test_short_secret_rejected(required_env: pytest.MonkeyPatch) -> None:
    required_env.setenv("ORBIT_API_TOKEN", "change-me-long-random")
    with pytest.raises(ValidationError, match="at least 32"):
        Settings(_env_file=None)


def test_non_argon2_hash_rejected(required_env: pytest.MonkeyPatch) -> None:
    required_env.setenv("ORBIT_WEB_PASSWORD_HASH", "plaintext-password")
    with pytest.raises(ValidationError, match="argon2"):
        Settings(_env_file=None)


def test_env_example_matches_settings() -> None:
    values = dotenv_values(ROOT / ".env.example")
    # Read by the compose files and the backup container, not by the app.
    docker_only = {
        "ORBIT_DOCKER_DATABASE_URL",
        "ORBIT_DOMAIN",
        "POSTGRES_PASSWORD",
        "BACKUP_AGE_RECIPIENT",
        "BACKUP_TARGET",
        "BACKUP_TIME",
        "BACKUP_KEEP_DAYS",
        "BACKUP_KEEP_LOCAL_DAYS",
    }
    settings_keys = {
        str(field.validation_alias or name).upper() for name, field in Settings.model_fields.items()
    }
    assert set(values) - docker_only == settings_keys
    # `KEY=   # comment` would turn the comment into the value.
    assert not [key for key, value in values.items() if value and value.startswith("#")]


def test_dotenv_file(tmp_path: Path, password_hash: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Quoted argon2 hash (contains `$`), trailing comments and empty values."""
    for name in ("ORBIT_API_TOKEN", "ORBIT_WEB_PASSWORD_HASH", "ORBIT_SECRET_KEY", "ICS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                f"ORBIT_API_TOKEN={'a' * 40}",
                f"ORBIT_WEB_PASSWORD_HASH='{password_hash}'",
                f"ORBIT_SECRET_KEY={'b' * 40}          # session cookies",
                f"ICS_TOKEN={'c' * 40}",
                "# set to the current model id",
                "ANTHROPIC_MODEL=",
                "SHARED_MIN_AMOUNT=15.00",
            ]
        ),
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.orbit_web_password_hash.get_secret_value() == password_hash
    assert settings.orbit_secret_key.get_secret_value() == "b" * 40
    assert settings.anthropic_model is None
    assert settings.shared_min_amount_minor == 1500
