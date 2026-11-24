---
name: seed-demo-and-screenshot
description: Boot Orbit with the fictional demo persona and fake Claude on a throwaway database, check it, and take screenshots of a UI change. Use to see a web change working or to refresh the images in docs/images.
---

# Seed the demo and look at it

Everything is synthetic (persona Alex Novak), offline, on a temporary database. Never point this at a real database.

- Smoke test (seed, boot, `/healthz`, `/api/v1/context`, 401 check, stop): `make demo` (runs `scripts/demo_smoke.sh`, port `PORT`, default 8765).
- To look at pages: follow "Quick start with demo data" in `README.md` (throwaway secrets, `alembic upgrade head`, `uv run python -m orbit.cli seed-demo`,
  `uvicorn orbit.main:app`; password `demo-password`). Use `DATABASE_URL=sqlite:///./agent-scratch.db`; `ORBIT_CLAUDE_MODE=fake`; `SCHEDULER_ENABLED=false`.
  `seed-demo --force` deletes everything and is denied; use a fresh file instead (`agent-scratch.db` is git-ignored).
- Screenshots: `scripts/screenshots/capture.mjs` (Playwright, not a dependency of the repo) produced `docs/images/*.png`; its header lists the
  environment variables (`ORBIT_BASE_URL`, `ORBIT_DEMO_PASSWORD`, `PLAYWRIGHT_MODULE`). A screenshot with a real name
  or number is a leak: check each one before keeping it.
- Verify a UI change by loading the page and reading text (browser tool or `curl` with the session cookie), not by assuming the tests cover it.
- Stop the server when done and delete the scratch database.
