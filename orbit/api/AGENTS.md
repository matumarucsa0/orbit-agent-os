# orbit/api: JSON API (`/api/v1`) and worker endpoints

Purpose: thin HTTP layer for iOS Shortcuts, the MCP server, the Claude worker and the review agent. Every route with
its handler and auth is in `docs/ROUTES.md` (generated).

## Local invariants
- A router file defines `router = APIRouter(tags=[...])` and is added to `router.py::build_api_router`; that function
  puts `require_api_token` on every route. A router that is not included is not served. Routers with their own
  prefix and token (`claude_worker.py`, `review_agent.py`: `ORBIT_REVIEW_TOKEN`) and the unauthenticated or token-in-URL
  ones (`actions.py`, `health.py`) are included in `orbit/app.py` instead; keep that list short.
- Only `GET /api/v1/context` (`context.py::get_context`) accepts the MCP token (`api/deps.py::_accepts_mcp_token` compares the
  endpoint function). Do not rename or wrap that function without updating the check and `tests/test_mcp_hardening.py`.
- Request and response bodies are Pydantic models in `schemas.py` (or next to the router); handlers parse, call a service,
  `db.commit()`, return. No business logic here. Errors: raise the service errors, `app.py` maps them (404, 409, 422).
- Writes that Claude or matching produce go through the Inbox (`services/inbox.py`); an API route must not apply them directly.
- Money in responses is integer minor units plus currency; datetimes UTC ISO strings.
- File uploads (`imports.py`, `intake.py`, `school_resources.py`): bounded size, parse errors become `InvalidInputError`.
- New secret-bearing fields must never be echoed in a response (see `tests/test_school_endpoints.py::test_status_never_shows_a_token`).

## Test it
`api` fixture (bearer token header set) in `tests/test_api_*.py`; also assert a 401 without the token. Walk-all-routes
checks: `tests/test_mcp_hardening.py`, `tests/test_api_auth.py`. `make one T=tests/test_api_people.py`.

## Common mistakes
- Forgetting `db.commit()` after a service call (data silently lost with the request-scoped session).
- Putting a query in the route instead of a service. Returning ORM objects instead of response models.
- Adding a route that takes the review token or the MCP token "just for convenience".
