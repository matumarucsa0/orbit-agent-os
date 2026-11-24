# orbit/web: server-rendered pages (Jinja2 + HTMX)

Purpose: the mobile-first UI. Routers in `orbit/web/*.py`, templates in `orbit/web/templates/<area>/`, static files in
`orbit/web/static/` (HTMX is vendored; there is no JS build step and no SPA). Routes: `docs/ROUTES.md`.

## Local invariants
- Every router except `auth.py` and `calendar.py` is `APIRouter(dependencies=[Depends(require_login)])`. A router without
  it is an unauthenticated page. Register the router in `orbit/app.py` (order matters: `/school/map` before `/school/{...}`).
- Pages call services and commit like API routes; no business logic in routes or templates. Forms post, redirect with 303,
  and use `templating.flash(request, message, kind)` for feedback. HTMX requests that lose their session get `HX-Redirect`.
- Templates autoescape; never mark user text `|safe`. Link static files with `static_url('path')` (hash cache-busting).
- State-changing actions are POST. There is no CSRF token yet (known residual risk, `docs/threat-model.md` §8): do not add
  GET routes that change data.
- Money is shown with `domain/money.py::format_amount`, local times with `settings.timezone`; page length is bounded
  (`tests/test_web_page_length.py`), navigation lives in `templates/more.html` and the base layout (`test_web_nav.py`).
- Inbox cards: markup in `templates/inbox/_card.html` and `_macros.html` (kind-specific forms such as `_split_form.html`);
  labels in `web/inbox.py::KIND_LABELS`.
- Text from outside (bank rows, calendar titles) is shown escaped; it must never reach a prompt without `untrusted(...)`.

## Test it
`web` fixture (logged in) in `tests/test_web_*.py`; unauthenticated redirect cases use `client`. A UI change is not
verified by tests alone: `make demo` boots the demo and `seed-demo-and-screenshot` takes screenshots.

## Common mistakes
- A new router without `require_login`. `|safe` on stored text. A form route that does not commit.
- Hardcoding a URL instead of `settings.orbit_url`/`url_for`. Forgetting `KIND_LABELS` and the card markup when adding a proposal kind.
