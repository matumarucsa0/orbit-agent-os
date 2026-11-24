---
name: add-api-route
description: Add a JSON API route under /api/v1 (or a web page route) to Orbit with the right auth, a service function, Pydantic models and tests. Use when adding or changing an endpoint.
---

# Add a route

Rules: `orbit/api/AGENTS.md` (API), `orbit/web/AGENTS.md` (pages). Existing routes: `docs/ROUTES.md`.

## JSON API
1. Logic in a service function (`orbit/services/<area>.py`): typed, docstring, flushes, does not commit.
2. Pydantic request/response models in `orbit/api/schemas.py` (or beside the router). Amounts as integer minor units, instants as UTC.
3. Handler in `orbit/api/<area>.py` using `DbSession`/`SettingsDep` from `orbit/deps.py`: parse, call the service, `db.commit()`, return.
   Raise service errors; `app.py` maps them. If the result changes data on behalf of Claude or matching, it must create/confirm a `Proposal`.
4. New router file: `router = APIRouter(tags=["x"])` and `router.include_router(x.router)` in `orbit/api/router.py::build_api_router`.
5. Test with the `api` fixture: happy path, validation error (422), unknown id (404), and 401 without the token.
   `tests/test_mcp_hardening.py` walks every route and checks the MCP token opens only `get_context`; run it.
6. `docs/SPEC.md` section 4 (API surface), CHANGELOG, `make codemap` (ROUTES.md is generated).

## Web page
Router with `dependencies=[Depends(require_login)]` in `orbit/web/<area>.py`, included in `orbit/app.py`; template under
`orbit/web/templates/<area>/`; POST for every change, 303 redirect, `flash(...)`. Test with the `web` fixture and an unauthenticated redirect case.
