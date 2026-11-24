from __future__ import annotations

from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient

from orbit import cli
from orbit.domain.enums import ProposalStatus
from orbit.domain.inbox import Proposal
from orbit.integrations.ntfy import NtfyError, PushMessage
from orbit.settings import Settings


class FakeNtfy:
    sent: ClassVar[list[tuple[str, str, str | None, PushMessage]]] = []
    fail: ClassVar[bool] = False

    def __init__(self, base_url: str, topic: str, token: str | None = None) -> None:
        self.args = (base_url, topic, token)

    def send(self, message: PushMessage) -> None:
        if FakeNtfy.fail:
            raise NtfyError("ntfy answered 401: unauthorized")
        FakeNtfy.sent.append((*self.args, message))

    def close(self) -> None:
        pass


@pytest.fixture
def ntfy(monkeypatch: pytest.MonkeyPatch, make_settings: Any) -> type[FakeNtfy]:
    FakeNtfy.sent, FakeNtfy.fail = [], False
    monkeypatch.setattr(cli, "NtfyClient", FakeNtfy)
    return FakeNtfy


def use(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: settings)


def test_test_push_sends_one_push(
    monkeypatch: pytest.MonkeyPatch, make_settings: Any, ntfy: type[FakeNtfy], capsys: Any
) -> None:
    use(monkeypatch, make_settings(ntfy_topic="orbit-t", ntfy_token="tk_x"))
    assert cli.main(["test-push"]) == 0
    ((url, topic, token, message),) = ntfy.sent
    assert (url, topic, token) == ("https://ntfy.sh", "orbit-t", "tk_x")
    assert message.actions[0].url == "http://testserver/inbox"
    assert "Sent a test push" in capsys.readouterr().out


def test_test_push_needs_a_topic(
    monkeypatch: pytest.MonkeyPatch, make_settings: Any, ntfy: type[FakeNtfy], capsys: Any
) -> None:
    use(monkeypatch, make_settings())
    assert cli.main(["test-push"]) == 1
    assert "NTFY_TOPIC" in capsys.readouterr().err
    assert ntfy.sent == []


def test_test_push_reports_failures(
    monkeypatch: pytest.MonkeyPatch, make_settings: Any, ntfy: type[FakeNtfy], capsys: Any
) -> None:
    use(monkeypatch, make_settings(ntfy_topic="orbit-t"))
    ntfy.fail = True
    assert cli.main(["test-push"]) == 1
    assert "401" in capsys.readouterr().err


def test_test_push_with_action_round_trip(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, app: Any, ntfy: type[FakeNtfy]
) -> None:
    configured = settings.model_copy(update={"ntfy_topic": "orbit-t"})
    use(monkeypatch, configured)
    # The CLI's own engine must see the app's database.
    monkeypatch.setattr(cli, "make_engine", lambda _url: app.state.engine)
    monkeypatch.setattr(app.state.engine, "dispose", lambda: None)

    assert cli.main(["test-push", "--action"]) == 0

    ((*_, message),) = ntfy.sent
    reject, inbox = message.actions
    assert (reject.kind, reject.label) == ("http", "Reject")
    assert reject.url.startswith("http://testserver/a/")
    assert inbox.kind == "view"
    with app.state.sessionmaker() as session:
        (proposal,) = session.query(Proposal).all()
        assert proposal.status is ProposalStatus.PENDING
        assert message.click == f"http://testserver/inbox/{proposal.id}"

    # What the phone does when Reject is tapped.
    with TestClient(app) as phone:
        response = phone.post(reject.url.removeprefix("http://testserver"))
    assert response.status_code == 200
    assert response.text == "Rejected."
    with app.state.sessionmaker() as session:
        assert session.query(Proposal).one().status is ProposalStatus.REJECTED


def test_test_push_with_action_keeps_no_item_when_the_push_fails(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, app: Any, ntfy: type[FakeNtfy]
) -> None:
    use(monkeypatch, settings.model_copy(update={"ntfy_topic": "orbit-t"}))
    monkeypatch.setattr(cli, "make_engine", lambda _url: app.state.engine)
    monkeypatch.setattr(app.state.engine, "dispose", lambda: None)
    ntfy.fail = True

    assert cli.main(["test-push", "--action"]) == 1
    with app.state.sessionmaker() as session:
        assert session.query(Proposal).count() == 0
