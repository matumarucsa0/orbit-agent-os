# orbit/jobs: in-process scheduler

Purpose: `scheduler.py` holds every background job (`JOBS`, `JobSpec`), the run wrapper `execute()`, and `start_scheduler`
(APScheduler `BackgroundScheduler`, one thread pool inside the app process). The same `execute()` backs the "Run now"
buttons on `/settings/schedule`. Table of jobs: `docs/CODEMAP.md` section 4.

## Local invariants
- Run exactly one app process with `SCHEDULER_ENABLED=true`; tests and agent runs have it off (`make_settings`, `.claude/settings.json`).
- A job is `run(ctx, settings, manual) -> str` (a short result line). It opens its own session from `ctx.sessions`, commits
  itself, and may raise: `execute()` catches, logs, and records the outcome in `job_runs` (never raises, per-job lock, so
  a job never overlaps itself).
- `available(ctx)` returns a reason string when the needed client is missing; `enabled(settings)` is the user's switch.
  Both are shown on the schedule page; do not skip them by checking inside `run`.
- Intervals come from `Settings` (and the edits stored by `services/schedule_config.py`); `sync_intervals` applies a change
  at the next tick. Pushes must be idempotent (dedupe keys) because a job can run twice after a restart.
- A job that asks Claude uses `ctx.claude` / `ctx.reviewer`; with the budget exhausted or no client it must degrade, not fail.
- The worker queue is `services/claude_jobs.py`, not APScheduler.

## Test it
`tests/test_scheduler.py`, `test_schedule_jobs.py`, `test_schedule_config.py`: build a `JobContext` with fakes and call
`execute(ctx, job_spec("<id>"))` or the job function with fixed `now`; never start the real scheduler.

## Common mistakes
- Sleeping or looping inside a job. Sending a push without a dedupe key. Reading `get_settings()` instead of `ctx.settings()`
  (loses the edits made on the schedule page). Adding a job without a `JobSpec` row (it never runs and never shows up).
