from __future__ import annotations

import re
from collections.abc import Iterator

import pytest

from orbit import cli
from orbit.services.passwords import hash_password, verify_password


def test_hash_and_verify() -> None:
    hashed = hash_password("hunter2hunter2")
    assert hashed.startswith("$argon2id$")
    assert verify_password(hashed, "hunter2hunter2")
    assert not verify_password(hashed, "hunter3hunter3")


def test_verify_with_invalid_hash_is_false() -> None:
    assert not verify_password("not-a-hash", "whatever")


def _answers(monkeypatch: pytest.MonkeyPatch, *values: str) -> None:
    it: Iterator[str] = iter(values)
    monkeypatch.setattr(cli, "getpass", lambda _prompt="": next(it))


def test_cli_hash_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _answers(monkeypatch, "long enough password", "long enough password")
    assert cli.main(["hash-password"]) == 0
    line = capsys.readouterr().out.strip()
    match = re.fullmatch(r"ORBIT_WEB_PASSWORD_HASH='(\$argon2id\$[^']+)'", line)
    assert match
    assert verify_password(match.group(1), "long enough password")


def test_cli_rejects_mismatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _answers(monkeypatch, "long enough password", "different password")
    assert cli.main(["hash-password"]) == 1
    assert capsys.readouterr().out == ""


def test_cli_rejects_short(monkeypatch: pytest.MonkeyPatch) -> None:
    _answers(monkeypatch, "short")
    assert cli.main(["hash-password"]) == 1
