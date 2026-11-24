from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from orbit import cli
from orbit.domain import Base
from orbit.domain.enums import DebtDirection
from orbit.services import export, ledger, people
from orbit.settings import Settings


def seed(db: Session) -> int:
    pato = people.create_person(
        db, people.PersonCreate(display_name="Paťo", birthday_month=3, birthday_day=17)
    )
    people.add_note(db, pato, "likes jazz")
    people.add_tag(db, pato, "Uni")
    ledger.create_debt(
        db,
        ledger.DebtCreate(
            person_id=pato.id, direction=DebtDirection.THEY_OWE_ME, amount_minor=1200
        ),
        "EUR",
    )
    db.commit()
    return pato.id


def test_exports_every_table(db: Session) -> None:
    pato_id = seed(db)
    data = export.export_data(db, now=datetime(2026, 9, 24, 12, 0, tzinfo=UTC))
    assert (data["format"], data["version"]) == ("orbit-export", 1)
    assert data["exported_at"] == "2026-09-24T12:00:00+00:00"
    assert data["schema_revision"] is None  # made without Alembic
    assert set(data["tables"]) == {t.name for t in Base.metadata.sorted_tables}
    (person,) = data["tables"]["person"]
    assert (person["id"], person["display_name"], person["birthday_month"]) == (pato_id, "Paťo", 3)
    assert isinstance(person["created_at"], str)
    (debt,) = data["tables"]["debt"]
    assert (debt["amount_minor"], debt["currency"], debt["direction"]) == (
        1200,
        "EUR",
        "they_owe_me",
    )
    assert data["tables"]["person_tag"] == [{"person_id": pato_id, "tag_id": 1}]
    assert json.loads(export.to_json(data)) == data  # plain JSON, diacritics kept
    assert "Paťo" in export.to_json(data)


def test_web_download_needs_login(client: TestClient, app: FastAPI) -> None:
    with app.state.sessionmaker() as session:
        seed(session)
    assert client.get("/export.json", follow_redirects=False).status_code == 303
    client.post("/login", data={"password": "correct horse battery staple"})
    response = client.get("/export.json")
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith('attachment; filename="orbit-export-')
    assert response.json()["tables"]["note"][0]["text"] == "likes jazz"
    assert "Download all data" in client.get("/import").text


def test_api_export(api: TestClient, app: FastAPI) -> None:
    with app.state.sessionmaker() as session:
        seed(session)
    assert api.get("/api/v1/export").json()["tables"]["tag"][0]["name"] == "Uni"


def test_cli_export(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "make_engine", lambda _url: app.state.engine)
    monkeypatch.setattr(app.state.engine, "dispose", lambda: None)
    with app.state.sessionmaker() as session:
        seed(session)
    target = tmp_path / "out.json"
    assert cli.main(["export", "-o", str(target)]) == 0
    assert (
        json.loads(target.read_text(encoding="utf-8"))["tables"]["person"][0]["display_name"]
        == "Paťo"
    )
    assert cli.main(["export"]) == 0
    assert json.loads(capsys.readouterr().out)["format"] == "orbit-export"
