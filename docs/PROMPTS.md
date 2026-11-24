# Prompt versions

Every prompt Orbit sends to Claude has a version, written `name@N` (for example `capture@1`). The
version is stored on each `claude_call` row (column `prompt_version`), on each proposal that
Claude produced (`proposal.prompt_version`) and on the tracing span of the call. That lets you ask
"which prompt produced this answer" and compare cost, failure rate and corrections per version.

The registry is `orbit/integrations/prompts_registry.py`. The prompt texts stay in
`orbit/integrations/claude.py` (the system prompts, with `{owner}` placeholders). A version covers
the system prompt text only. The per-request user prompt is built by code from the data and is
covered by the tests of that code.

| Purpose | Constant | Used for |
|---|---|---|
| `capture` | `CAPTURE_SYSTEM` | dictated text to notes, debts, promises, appointments |
| `classification` | `CLASSIFY_SYSTEM` | bank rows to categories |
| `meeting_link` | `LINK_SYSTEM` | calendar events to people |
| `brief` | `BRIEF_SYSTEM` | one-line pre-meeting brief |
| `draft` | `DRAFT_SYSTEM` | birthday and name-day messages |
| `weekly_review` | `REVIEW_SYSTEM` | the weekly review (the worker calls this job `review`) |
| `report` | `REPORT_SYSTEM` | custom reports |
| `school_read` | `SCHOOL_READ_SYSTEM` | course pages and PDFs to deadlines and facts |

## Changing a prompt

1. Edit the text in `claude.py`.
2. Raise that prompt's number in `prompts_registry.py`.
3. Add a line to the changelog below.
4. Run `uv run python -m orbit.cli prompts-snapshot` to record the new hash in
   `tests/prompt_hashes.json`.

`tests/test_prompt_versions.py` fails when the text hash differs from the snapshot and the version
did not move (so a prompt cannot change silently), when the snapshot was not regenerated after a
bump, and when this file has no line for a current version. If an eval suite exists, run it before
and after the change and note the result in the changelog line.

## Changelog

- `capture@1`: baseline when versioning started. Owner placeholder, neutral pronouns, corrections and
  rejected lists fed back into the prompt.
- `classification@1`: baseline.
- `meeting_link@1`: baseline.
- `brief@1`: baseline.
- `draft@1`: baseline.
- `weekly_review@1`: baseline.
- `report@1`: baseline.
- `school_read@1`: baseline. Already told Claude that page text is data, not instructions.
