"""Single-purpose signed tokens for push action buttons (`/a/{token}`).

A token carries `{proposal_id, action, exp}`, HMAC-signed with ORBIT_SECRET_KEY. It is single-use
because an action only applies to a pending proposal: once confirmed or rejected, every token for
that proposal is spent, and stays spent if the proposal is reopened (tokens older than
`reopened_at` are refused). The API bearer token never goes into a push.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from itsdangerous import BadSignature, URLSafeSerializer

from orbit.domain.base import utcnow
from orbit.services.errors import InvalidInputError

SALT = "orbit.push-action"
TOKEN_LIFETIME = timedelta(days=7)
ACTIONS = frozenset({"confirm", "reject"})


@dataclass(frozen=True)
class ActionToken:
    proposal_id: int
    action: str
    issued_at: datetime


def _serializer(secret: str) -> URLSafeSerializer:
    return URLSafeSerializer(secret, salt=SALT)


def make_token(secret: str, proposal_id: int, action: str, now: datetime | None = None) -> str:
    """A signed, expiring token for one action on one proposal (push action buttons); never contains
    the API token."""
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    expires = (now or utcnow()) + TOKEN_LIFETIME
    return str(
        _serializer(secret).dumps({"p": proposal_id, "a": action, "exp": int(expires.timestamp())})
    )


def action_url(
    base_url: str, secret: str, proposal_id: int, action: str, now: datetime | None = None
) -> str:
    """The `/a/{token}` URL for one action on one proposal."""
    return f"{base_url}/a/{make_token(secret, proposal_id, action, now)}"


def read_token(secret: str, token: str, now: datetime | None = None) -> ActionToken:
    """Verify signature and expiry and return the token's content; raises InvalidInputError for a
    bad or expired token."""
    try:
        data = _serializer(secret).loads(token)
    except BadSignature as exc:
        raise InvalidInputError("This action link isn't valid.") from exc
    if not isinstance(data, dict) or data.get("a") not in ACTIONS:
        raise InvalidInputError("This action link isn't valid.")
    if (now or utcnow()).timestamp() > data.get("exp", 0):
        raise InvalidInputError("This action link has expired; open the Inbox instead.")
    issued_at = datetime.fromtimestamp(int(data["exp"]), UTC) - TOKEN_LIFETIME
    return ActionToken(proposal_id=int(data["p"]), action=str(data["a"]), issued_at=issued_at)
