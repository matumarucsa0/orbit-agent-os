from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from orbit.services.action_tokens import TOKEN_LIFETIME, action_url, make_token, read_token
from orbit.services.errors import InvalidInputError

SECRET = "s" * 40
NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


def test_round_trip() -> None:
    token = make_token(SECRET, 17, "confirm", NOW)
    action = read_token(SECRET, token, NOW + timedelta(days=1))
    assert (action.proposal_id, action.action) == (17, "confirm")


def test_url_points_at_the_action_route_without_the_api_token() -> None:
    url = action_url("https://orbit.example.com", SECRET, 5, "reject", NOW)
    assert url.startswith("https://orbit.example.com/a/")
    assert read_token(SECRET, url.rsplit("/", 1)[1], NOW).action == "reject"


def test_expires_after_seven_days() -> None:
    token = make_token(SECRET, 1, "confirm", NOW)
    read_token(SECRET, token, NOW + TOKEN_LIFETIME)
    with pytest.raises(InvalidInputError, match="expired"):
        read_token(SECRET, token, NOW + TOKEN_LIFETIME + timedelta(seconds=1))


def test_tampered_or_foreign_tokens_are_rejected() -> None:
    token = make_token(SECRET, 1, "confirm", NOW)
    with pytest.raises(InvalidInputError, match="isn't valid"):
        read_token("other" * 10, token, NOW)
    with pytest.raises(InvalidInputError, match="isn't valid"):
        read_token(SECRET, token[:-2] + ("AA" if not token.endswith("AA") else "BB"), NOW)
    with pytest.raises(InvalidInputError):
        read_token(SECRET, "garbage", NOW)


def test_unknown_action_is_refused() -> None:
    with pytest.raises(ValueError):
        make_token(SECRET, 1, "delete_everything", NOW)


def test_issued_at_is_read_back() -> None:
    token = make_token(SECRET, 3, "reject", NOW)
    assert read_token(SECRET, token, NOW).issued_at == NOW
