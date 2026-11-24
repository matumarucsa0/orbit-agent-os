# Orbit — Roadmap

Work **one phase at a time**. At the start of each phase: re-read `AGENTS.md` + the relevant `SPEC.md` sections,
propose a short plan, then implement. A phase is done when all acceptance criteria pass, `pytest`, `ruff` and `mypy`
are green, and the phase has been committed. Tick the boxes as you go.

---

## Phase 0 — Foundations
- [x] `uv init`, `pyproject.toml`, ruff + mypy + pytest config, pre-commit hook (ruff)
- [x] `orbit/settings.py` (pydantic-settings) matching `.env.example`
- [x] FastAPI app factory, `/healthz`, bearer-token dependency, web password login + session cookie
- [x] SQLAlchemy engine/session, Alembic set up, base model with `created_at/updated_at`
- [x] `normalize()` helpers (diacritics, whitespace, E.164 phone via `phonenumbers`) + tests
- [x] Base Jinja2 layout (mobile-first, HTMX from a pinned CDN or vendored file), minimal CSS
- [x] `Dockerfile` + `docker-compose.yml` (app + optional Postgres), `README.md` quickstart

**Done when:** `docker compose up` serves `/healthz`; the login page works; the test suite runs in CI (GitHub Actions: ruff, mypy, pytest).

## Phase 1 — People core
- [x] Models: Person, Alias, Handle, Tag, PersonTag, Note, Interaction(+InteractionPerson) + migration
- [x] JSON API CRUD for people, handles, aliases, tags, notes, interactions
- [x] Diacritics-insensitive search (name, aliases, handles, tags, notes)
- [x] vCard import (Apple Contacts export + one-way iCloud CardDAV sync), idempotent; fixture test with 5+ anonymized cards
- [x] Duplicate candidate finder + merge service (moves handles/notes/debts etc., keeps aliases)
- [x] Web UI: People list + search + tag filter, Person detail (edit fields, handles, notes timeline), Add person (name-only works)

**Done when:** I can import my contacts, add "@someone" as an IG-only person, find "Pato" by typing "pato" or "paťo", and merge two duplicates.

## Phase 2 — Dates & ICS feed
- [x] `data/namedays_sk.json` and `data/namedays_cz.json` — **source them from an authoritative public list, cite the source in
      `data/README.md`, and add a test that spot-checks ~10 known dates for each country**
- [x] Name-day resolver (normalized first name + country → dates, multiple dates allowed)
- [x] ICS feed `/calendar/{ICS_TOKEN}.ics` with birthdays and name days (stable UIDs, all-day, yearly)
- [x] Home page: today + next 7 days of birthdays and name days

**Done when:** subscribing to the feed in Apple Calendar shows correct birthdays and name days, and editing a person updates the feed without duplicating events.

## Phase 3 — Ledger & promises (manual)
- [x] Models: Debt (money/item/favor, both directions, parent for partials, reference_code), Promise + migration
- [x] Ledger service: create, settle (full/partial → remainder), per-person + global per-currency balances — thorough unit tests
- [x] `reference_code` generator (`D-0001`…), unique
- [x] Promises CRUD + status transitions
- [x] Extend the Phase 1 merge service (`services/duplicates.py`) to move debts and promises
- [x] Web UI: Ledger page (who owes me / whom I owe / items / favors), ledger + promises on Person detail, quick-add forms
- [x] ICS feed: promise due dates, lent-item reminder dates

**Done when:** I can log "Pato has my charger", "I owe Terka a favor (moving)", "Pato owes me 12 € pizza", settle 5 € of it, and see correct balances everywhere.

## Phase 4 — Inbox, notifications, Claude capture
- [x] Proposal model + Inbox service (confirm applies payload atomically; reject; edit-then-confirm)
- [x] Inbox web UI (cards per kind, edit form for `capture` items, "amount missing" prompt)
- [x] `integrations/ntfy.py`: send with `view` + `http` actions; Notification table with dedupe keys; quiet hours queue
- [x] `integrations/claude.py`: client wrapper, structured output → Pydantic models, retries, masking of IBAN/phones, model from env
- [x] Capture service: fuzzy pre-match of names/aliases → Claude → `capture` proposal (+ `new_person` for unknowns)
- [x] `POST /api/v1/capture` returning a short summary string for Shortcuts
- [x] Tests with a mocked Claude client, incl. Slovak/Czech input ("zaplatil som Paťovi pizzu 12€, sľúbil som mu poslať rozvrh")

**Done when:** dictating a sentence from my iPhone via the `Orbit Capture` Shortcut produces a correct Inbox proposal, and confirming it creates the note/debt/promise.

## Phase 5 — Deploy
- [x] Production compose: app + Postgres + Caddy (HTTPS), volumes, healthchecks (`docker-compose.prod.yml`, `docs/DEPLOY.md`)
- [x] Nightly encrypted backup (`pg_dump` → `age` → off-site target from env), plus a restore script that has been tested
      (`scripts/backup-drill.sh`, run in CI; ntfy push on failure)
- [x] Moving data from dev SQLite into production Postgres (`orbit.cli copy-db`); `orbit.cli smoke-test`; test suite also runs on Postgres in CI
- [ ] `docs/SHORTCUTS.md` verified end-to-end against the deployed instance (go-live checklist at the end of SHORTCUTS.md; needs the server and the iPhone)

**Done when:** it runs on my server over HTTPS, Shortcuts and ntfy work from my phone, and a backup restore has been tested.

## Phase 6 — Bank CSV import & split flagging
- [x] **Ask me for anonymized sample CSVs from Revolut and Tatra banka** → `tests/fixtures/bank/` (anonymized from real
      exports; the Revolut sample only has top-ups and exchanges — a sample with card payments and transfers is still wanted)
- [x] Transaction + BankImport models; parser interface; `revolut` and `tatra` parsers; header auto-detect
- [x] Hash dedupe; re-importing an overlapping file yields 0 new rows (test)
- [x] Rule-based classifier + Claude batch classifier for unresolved rows (mocked in tests); ECB rates for the threshold
- [x] `split_transaction` proposals + push; split UI (pre-select people from overlapping meetings once Phase 8 exists; person picker for now), even/custom split
- [x] Import page (upload, summary) + `POST /api/v1/imports/bank` for the share-sheet Shortcut `Orbit Import`
- [ ] `Orbit Import` verified from the iPhone (checklist in SHORTCUTS.md; needs the deployed server)
- [ ] A month of real CSV imported with Claude on (the rules alone were checked against a real Tatra export)

**Done when:** importing a month of real CSV flags the shared dinners correctly, and splitting one creates the right debts.

## Phase 7 — Incoming payment matching (manual settle)
- [x] Payer identification (IBAN handle → bank_name handle → reference code → fuzzy name/alias)
- [x] Candidate search: reference_code → exact amount → subset-sum (≤4 debts) → partial; ranked options
- [x] `payment_match` proposal + push with [Settle] [Choose…] [Not a debt] (`http` actions to `/a/{token}`)
- [x] Unknown payer → proposal to link the bank name to a person (creates `bank_name` / `iban` handles on confirm)
- [x] Tests: exact, multi-debt, partial, reference code, unknown payer, currency mismatch
- [ ] A real friend's payment back pushed and settled from the iPhone (checklist in SHORTCUTS.md; needs the deployed server)

**Done when:** a friend paying me back produces a push, one tap settles the right debts, and **nothing is ever settled without a tap**.

## Phase 8 — Calendar, pre-meeting brief, post-meeting capture
- [x] `integrations/caldav_client.py` (iCloud, app-specific password), calendar allow-list from env; recurring events
      expanded per occurrence, all-day events skipped; parser tested on an ICS fixture (`tests/fixtures/calendar/`)
- [x] Meeting sync job (upsert by UID, cancellations, moves), idempotent; "Sync now" + `POST /api/v1/meetings/sync`
- [x] Meeting→person linking (attendee emails, title tokens vs names/aliases with fuzzy + Slovak/Czech inflection tolerance, Claude fallback); manual link UI
- [x] Brief builder service + push at T-30 (dedupe), `/meetings/{id}` page (+ Meetings list, today's meetings on Home)
- [x] Post-meeting push at end+15 with [Dictate] → `shortcuts://run-shortcut?...` carrying meeting_id; capture adds an interaction
- [x] Split UI: pre-select people from meetings overlapping the transaction time (and Claude's bank classifier is told about them)
- [ ] Verified with the real iCloud calendar and the iPhone (checklist in SHORTCUTS.md; needs the deployed server)

**Done when:** a "kafe s Paťom" event triggers a correct brief 30 minutes before, and a dictation afterwards lands in the Inbox linked to that meeting.

## Phase 9 — Reminders & digest
- [x] Birthday / name-day pushes (09:00 on the day; T-5 for `close` tag)
- [x] Lent-item reminders, overdue promise pushes
- [x] Sunday 18:00 digest
- [x] Home page polish (search, "Needs attention", money totals); PWA manifest + icon
- [ ] A full week on the deployed instance: reminders arrive once, on time, never during quiet hours (checklist in
      SHORTCUTS.md; needs the server and the iPhone)

**Done when:** for a full week, reminders arrive once, on time, and never during quiet hours.

## Phase 10 — Pay-back texts, keep in touch, drafts, ops
- [x] Ask-to-pay texts per currency (reference codes, optional `PAYMENT_IBAN`) and ask-back texts for lent items, in the person's language
- [x] Keep-in-touch cadence per tag with per-person override; Home, `/keep-in-touch`, Sunday digest
- [x] Birthday / name-day message drafts by Claude (plain wish fallback)
- [x] Claude timeout and token usage; merge undo; full JSON export

---

## Phase 11 — Contact sync, pull side (SPEC §2.13)
- [x] Migration `0012`: `contact_link`, `sync_run`, `sync_change`; people that already have a `vcard_uid` get their link (empty base) on the first sync
- [x] Diff service (`orbit/services`, no I/O): three-way diff per field, set diff for phones/emails, `BDAY` forms, fixture tests for every case in the table (pull, both changed, same change, removed upstream)
- [x] CardDAV read + `.vcf` upload feed the same diff; `.vcf` only has the Orbit section
- [x] Review screen (HTMX): per-field rows, checkboxes, group select, search/filter, bulk select; Apply in one DB transaction
- [x] Auto-fill of empty Orbit fields, pre-checked, logged with Undo; names never auto
- [x] Rejection memory; second sync with no changes creates nothing
- [x] Sync button on the Contacts page + `POST /api/v1/contacts/sync`
- [ ] Verified with the real iCloud account and the iPhone (needs the deployed server)

**Done when:** pressing Sync on the real iCloud account shows a correct diff, I can take a birthday and skip a phone for the same person, and a second Sync right after shows no changes.

## Phase 12 — Contact sync, write-back (SPEC §2.13)
- [x] vCard patching that keeps unknown properties; round-trip tests on anonymized fixtures with photos, `X-*` fields and groups
- [x] "Into Apple contacts" preview section with the exact per-contact diff; auto-fill of empty Apple fields pre-checked, names unchecked
- [x] `PUT` with `If-Match`; ETag mismatch skips that contact (the next Sync diffs it again); per-contact failures reported
- [x] `contact_backup` snapshots and revert; 90-day pruning job
- [x] Conflict picker (both values shown, pick one; typing a third value is not built)
- [x] Verified against real iCloud on a throwaway contact (2026-10-02): create, preview, push, year-less `BDAY` (`X-APPLE-OMIT-YEAR`), unknown `X-*` property kept, second sync clean, undo, restore, stale-ETag block, contact deleted afterwards

**Done when:** a birthday added in Orbit appears in Apple Contacts after Apply, nothing else on that card changed, and a revert restores the original card.

---

# Life OS track (see `VISION.md`, `DECISIONS.md` #3)

Phases 13+ turn Orbit from a CRM into a personal operating system. The rules above still apply, plus one more:
**every phase that adds a module or an agent starts with a spec.** First write or extend the `SPEC.md` section, add the
models to `SPEC.md §3`, and list open product questions. Then wait for my OK before writing code.

The unticked live-verification boxes in Phases 5–12 mean: not yet checked end to end on a deployed server with a real phone and
real accounts. The code is covered by automated tests against fakes and synthetic fixtures; that is all that has been verified. They do not block
Phase 13. Keep them open until they are checked for real.

Phases after 16 are listed but not ordered. I choose the next domain when Phase 16 is done.

## Phase 13 — Module seams (refactor, no behaviour change)
- [x] Proposal kind registry: kind → payload model, apply function, reopen rule. It replaces the `if/elif` dispatch in
      `services/inbox.py`. Adding a kind touches the module that owns it plus one registration line
- [x] Context provider protocol (`VISION.md §4`): a typed, bounded, masked Pydantic summary per module, for a time window
      and an optional focus (person). Implement it for People, Ledger, Meetings and Promises
- [x] Move the existing brief and capture prompts to read through the providers where it makes them simpler. Prompts
      and outputs stay the same. *Looked at, not moved:* the brief summarizer reads rows `build_brief` already loaded
      (also used for the push) and capture's candidates come from fuzzy name matching; going through providers would add
      queries and clip text now sent whole (a change to what Claude sees). New agents (Phase 16) read providers first
- [x] Tests: provider output for fixed fixtures, masking (no IBAN or full phone number), size limits

**Done when:** all existing tests pass unchanged, and a test builds the context for one person from all four providers.

## Phase 14 — Shared context layer
- [x] Spec first: `SPEC.md` section for the context layer (snapshot types, windows, focus kinds, size budget)
- [x] `services/context/` (a package: providers + `snapshot.py`): `build_context(window, focus)` combines the providers into one snapshot, with masking and
      size limits applied in one place
- [x] Debug view in the web UI: show exactly what an agent would receive for "this week" and for a person
- [x] Tests: snapshot for a fixed week, budget enforcement, focus filtering

**Done when:** the "this week" debug view shows people, money, meetings and promises together, and nothing sensitive
is unmasked.

## Phase 15 — Goals & priorities
- [x] Spec first: Goal model (title, why, horizon, rank, status, optional measurable target), links to people, promises
      and later domains, and the weekly re-ranking flow
- [x] Models + migration, service, API, web UI (list ranked by priority, goal detail with linked items)
- [x] Goals context provider
- [x] Capture can propose goal links ("this promise serves goal X"). It is part of the `capture` proposal (L2), not a new
      kind: the promise does not exist until confirm. The `goal_link` kind for existing items comes with the Phase 16 agent

**Done when:** I can rank my goals, link promises and people to them, and the context snapshot shows goals first.

## Phase 16 — Weekly review agent (first proof point, `VISION.md §7`)
- [x] Spec first: review inputs, output schema, proposal kinds it may create, schedule, push format
- [x] Scheduled job (default Sunday evening, Europe/Prague): build the context, call Claude through `integrations/claude.py`,
      validate the structured output
- [x] Output: a review page (wins, slips, conflicts, priorities for next week) + proposals in the Inbox (follow-ups,
      promise due dates, goal re-ranking). No direct writes
- [x] ntfy push linking to the review. Idempotent per week
- [x] Tests with a mocked Claude: inputs are masked, output is validated, a rerun in the same week does not duplicate

- [ ] Two real weeks run on the deployed instance (needs the server, Claude and the iPhone)

**Done when:** after two real weeks, the review has surfaced at least one conflict or priority I had not noticed.

## Phase 16b — Subscription-driven review (SPEC §2.18)
- [x] Spec first: `SPEC.md §2.18`
- [x] `prepare` / `submit` / `fail` endpoints behind a review-only token; `generate` split into prepare and finish, same checks
- [x] `scripts/weekly-review.sh` + systemd timer for the VM, no tools for the Claude session, failure push
- [x] `/reviews` without an API key; docs in DEPLOY.md; `CLAUDE.md` rule 5 reworded, `DECISIONS.md` entry
- [x] Tests: prepare idempotency and 204, submit validation and sanitizing, fail and retry, token scope

- [ ] Installed and tried on the VM (Claude Code login, memory), timer enabled (needs the server and my Claude login)

**Done when:** the Sunday review is made by Claude Code on the server with my subscription, with no API key set.

## Phase 17 — Claude worker (SPEC §2.19)
- [x] Spec first: `SPEC.md §2.19`; the open questions were answered with the recommendations
- [x] `ClaudeJob` / `ClaudeWorker` models + migration 0016; queue service (enqueue, claim with priority and lease, submit with validation, fail, expire, dedupe, caps, rerun, prune)
- [x] Worker endpoints behind the worker token; `JobClaudeClient` for capture, drafts, briefs, meeting links, bank classification, with per-kind waits and fast-fail; `CLAUDE_BACKEND` and the reviewer split in `app.py`
- [x] Late capture upgrade of the pending, untouched note-draft proposal; push "Capture ready"
- [x] `scripts/claude_worker.py` (long poll, no tools, self-test, auth and rate-limit detection, heartbeat, per-kind timeouts and models) + `deploy/orbit-claude-worker.service`
- [x] `/ops/claude` page (status, queue, recent jobs, Rerun); worker-down and login-expired pushes; usage rows in `ClaudeCall`; pruning job
- [x] Weekly review as a job kind (claimed by the idle worker; attempts stay on the review row)
- [x] Tests (`test_claude_jobs.py`, `test_claude_worker_api.py`, `test_claude_worker_script.py` with a fake `claude` and a real server); DEPLOY.md §10 (swap file, self-test, unit)
- [ ] Installed on the VM: worker running, `orbit-review.timer` disabled, `ANTHROPIC_API_KEY` removed, a real capture, brief and bank import answered through the worker (needs the server and my Claude login)
- [ ] After one real week on the worker: delete `scripts/weekly-review.sh`, `deploy/orbit-review.*` and the `review-agent` endpoints

**Done when:** with no API key, a dictated capture shows up as a pending proposal with the extracted items, a brief has its summary line,
and an expired login or a rate limit produces one clear push instead of silent fallbacks.

## Phase 18 — School module: SIS feeds (SPEC §2.20)
`SPEC.md §2.20` is accepted (answers recorded there). Autonomy L0/L1, no proposal kinds.
- [x] Confirm the open questions of `SPEC §2.20` with the owner; add the answers and `DECISIONS.md #12` -> accepted (2026-10-06; #13 Owl cookie)
- [x] Settings (`SIS_*`, `SCHOOL_*`), `.env.example`; enums; `domain/school.py` (`Course`, `Lesson`, `SchoolEvent`, `SisFetch`) + migration `0017`
- [x] ReCodEx: `RECODEX_ICAL_TOKEN` feed in `sis_sync` (`SchoolEvent(source = recodex)`, floating time = UTC, cancel guard) and the optional `read-all` API token (`users/<me>/groups` + `groups/<id>/assignments`: exact course link from the SIS ticket, points, second deadline, `CourseScore`), token-expiry push; tests against `tests/fixtures/recodex/` (`SPEC §2.20` "Sources beyond SIS")
- [x] Owl: `OWL_SESSION_COOKIE` GET-only reader of `/c/all` and `/c/<sem>/<course>/` in `sis_sync` (path allow-list with a test), parser for topics, deadlines, points, totals, course description; expired-cookie push; `OwlCourse` links with a picker. Tests use the real saved pages (`all_courses.html`, `course_la1_svobodova.html`); the hand-built `course_NMAT202_handbuilt.html` stays for sections and points until a real graded page exists
- [x] `integrations/sis.py`: `SisSource` Protocol, httpx implementation (user agent, 2 s throttle, token redaction), `parse_feed` (UID regex, DESCRIPTION pieces, UTC), `parse_course_page`, public harmonogram parser; tests against `tests/fixtures/sis/`
- [x] `services/school.py` + `services/school_sync.py`: idempotent sync per feed (upsert by UID, moved lessons, vanished future lesson = cancelled, empty/denied feed never cancels), course enrichment (30-day TTL, 3 tries/day), `SisFetch` rows, manual events, week data
- [x] Scheduler jobs `sis_sync` (every `SIS_POLL_MINUTES`) and `school_pushes`; pushes: tomorrow's lessons, exam 7d/1d, deadline 3d/1d, schedule change, feed problem (dedupe keys in the spec); Sunday digest section
- [x] `services/context/school.py` + `school` field in the snapshot after goals + weekly-review input and one prompt sentence; debug view shows it
- [x] `api/school.py` (`GET lessons|courses|events|status`, `POST events|sync`, `PATCH|DELETE events/{id}` for manual only); full JSON export includes the school tables
- [x] Web `/school` (week, upcoming, courses, course page, sync status, add deadline), Home strip, nav link
- [x] Tests as listed in the spec (parsers, sync idempotency, cancel guard, feed problems, throttle/TTL, redaction, provider, snapshot budget, pushes, pages, migration)
- [x] DEPLOY.md: where to find the token (SIS `harmonogram` -> "Export do kalendáře", or the Rozvrh "Export" tab), `.env` lines, rotation warning
- [ ] A real semester week on the deployed instance with the real token (needs the server; a single try against a real account in a scratch database worked on 2026-10-06, nothing longer); when the first exam is registered, add its real `term_st2` feed sample to `tests/fixtures/sis/` and check the parser

**Done when:** with `SIS_ICAL_TOKEN` set, `/school` shows this week's real lessons, a lesson moved or cancelled in SIS appears as a change push within an hour,
an exam registered in SIS gets its 7-day and 1-day reminders, the weekly review's input has a `school` module, and an expired token produces one push per day instead of silence.

## Phase 18b — School: session-only data through a browser helper (SPEC §2.20, 18b)
Starts only after Phase 18 has run for two weeks, with its own short spec step (open questions are listed in §2.20).
- [ ] Prove the helper route: an iOS Shortcut ("Run JavaScript on Web Page") or bookmarklet that logs in with one click (`index.php?sso`), fetches pages with the session and POSTs to Orbit; if Safari cannot do it, decide the alternative before building
- [ ] `POST /api/v1/school/sis-import` (bearer) + server-side parsers and anonymized real fixtures for: enrolled subjects (credits, state, row colours are not data), waiting list (position, wait-until), enrolment windows (`predm_st2?do=nastaveni`), open exam terms, results, teacher announcements (`omne?do=email`)
- [ ] Credits and enrolment state on `Course`; enrolment-window and waiting-list pushes; `SchoolAnnouncement` model + page
- [ ] Announcements into the capture pipeline as a source (L2 proposals only), after the owner agrees to send teacher mail to Claude
- [ ] Safety: never fetch `prihlastaz`, `stipendia`, `prijimacky` or external hops; never call write actions; `notify_check` only, not `do=notify`

## Phase 18c — School: map and resources (SPEC §2.20)
Built 2026-10-07. Spec accepted 2026-10-06 (answers in SPEC; `DECISIONS.md` #14: data flow map drawn by a small inline script, no library; `pypdf` for PDFs).
- [x] `SchoolResource` + `CourseFact` + migration; suggestions from SIS `Další informace`, Owl descriptions (and 18b ticket notes)
- [x] Add resource: URL, PDF upload, note (web form on a course node) and `POST /api/v1/school/resources` for a share-sheet Shortcut
- [x] Fetcher job `school_resources`: robots.txt, conditional GET, charset, HTML and PDF text (`pypdf`), GitHub via `commits.atom`, hash, diff, `broken` after 7 days
- [x] `SchoolReader` worker kind: diff or new text → deadlines, facts, links; daily budget; `school_deadline` and `course_fact` proposal kinds (payload, apply, dedupe)
- [x] `/school/map`: data flow map as in the approved mockup (layout computed in Python, `GET /school/map.json`, cards in HTML, edges in inline SVG, inline pan/zoom/drag/trace script), states with icon and label, HTMX side panel, list view on phones and without JS; layout unit tests and a browser smoke test
- [x] Provider gains facts and resources; daily "pages changed" push; tests (fetch politeness with a fake transport, extraction, diff, reader validation, proposals, map layout)
- [ ] A real-browser smoke test of the map script and a week of real use (no login is possible in the test setup; the page, JSON, panels and forms are tested)

## Phase 18d — School: Microsoft 365 (Outlook mail and calendar, Teams via mail) (SPEC §2.20)
Blocked on the consent check (checks 3 to 6 in SPEC §2.20). Own spec step after the check.
- [x] Route chosen 2026-10-09: B (published ICS + share-sheet Shortcut / forwarding), because the tenant blocks Graph consent; `DECISIONS.md` #31
- [ ] Confirm in Outlook web that "Publish a calendar" (and forwarding) is offered; find the sender and subject of Teams assignment mails (NINF120, the owner's only Teams course)
- [ ] `SchoolMail` ingest (idempotent by message id), course link without Claude (`X-Owl-*`, ticket ids, course codes), mail to the capture pipeline (L2), sender blocklist
- [ ] Outlook calendar events (Teams meetings included) as `SchoolEvent(source = outlook)`; token-failure push once per day

## Phase 19 — Schedule settings and Run now (SPEC §2.21)
Spec accepted 2026-10-07 (`DECISIONS.md` #15). Done out of order at the owner's request, between Phase 18c and 18d, because it touches no module data.
- [x] `AppSetting`, `AppSettingLog`, `JobRun` + migration `0018`
- [x] Former constants (reminder, digest, review, school, retry and retention values) became env-capable `Settings` fields with the old values as defaults, and service parameters with the old constants as defaults
- [x] `OverrideStore` (DB override > env > default) behind `SettingsDep` and the scheduler's `JobContext`; validation with bounds; change log; reset
- [x] Job registry (`JOBS`), `execute` with status, per-job lock, live interval changes; weekly review and school pushes switchable at run time
- [x] `/settings/schedule` page, API (`/api/v1/settings/schedule`, `/api/v1/jobs`), Run now per job
- [x] Tests: config, parameters, jobs, endpoints (SQLite and Postgres)
- [ ] Run on the server for a week; adjust the list of editable values and Run now buttons from use

## Phase 20 — Custom review report for a date range (SPEC §2.22)
Spec accepted 2026-10-07 (`DECISIONS.md` #16). Read-only (L1): no proposal kinds.
- [x] `ReviewReport` + `ClaudeJob.report_id` + migration `0019`
- [x] Range rules and presets, one report at a time, focus (text, goal, person, course)
- [x] `ReportOutput`, prompt and `ClaudeClient.make_report`; ids checked against the snapshot; numbers counted by Orbit
- [x] Three paths: API client (background), Claude worker (job kind `report`), none (plain report)
- [x] `/reports` pages (form with presets, report with Print and Copy as Markdown, Try again, Delete), API, push when ready
- [x] Tests (service, paths, worker lifecycle, API, pages)
- [ ] Use it for a month; adjust sections and numbers from use

## Phase 21 — Appointments from capture (SPEC §2.23)
Spec accepted 2026-10-07 (`DECISIONS.md` #19). Proposal item inside `capture`; no new kind.
- [x] Prompt rule: a future plan is never a note or interaction
- [x] `AppointmentDraft`, extraction, describe, apply (UTC, past date, end check, duplicate check)
- [x] `Meeting` with `calendar_name = "orbit"`, never cancelled by the CalDAV sync
- [x] ICS feed event, Inbox card (edit, include, "+ Appointment")
- [x] Tests (service, ICS, sync guard, Inbox page)
- [ ] Use it for a few weeks; adjust the prompt from real dictations

## Phase 22 — Navigation "More" menu (SPEC §2.24)
Spec accepted 2026-10-08 (`DECISIONS.md` #20).
- [x] The owner answers the open questions in SPEC §2.24 (all as recommended)
- [x] `/more` page and the desktop dropdown from one list of links; top bar and tab bar fit at 320, 375, 768 and 1024 px
- [x] Template tests

## Phase 23 — Home and Ledger page length (SPEC §2.25)
Spec accepted 2026-10-08 (`DECISIONS.md` #21).
- [x] The owner answers the open questions in SPEC §2.25 (all as recommended)
- [x] Home: new order, Needs attention capped, `/attention` page
- [x] Ledger: per-section cap with "Show all", exact totals
- [x] Tests; Ledger HTML under 150 KB with 450 rows
- [ ] Look at Home on the iPhone with real data (is 5 rows the right number?)

## Phase 24 — Inbox cards and capture feedback (SPEC §2.26)
Spec accepted 2026-10-08 (`DECISIONS.md` #22).
- [x] The owner answers the open questions in SPEC §2.26 (all as recommended)
- [x] `source_text` on `new_person` cards; `group` token; "Reject all N from this capture"
- [x] "Sending to Claude…" waiting state on every capture form
- [x] Tests

## Phase 25 — Capture can update existing items (SPEC §2.27)
Spec accepted 2026-10-08 (`DECISIONS.md` #23).
- [x] The owner answers the open questions in SPEC §2.27 (all as recommended)
- [x] `SettleDraft`, `PromiseUpdateDraft`; open debts and promises of candidate people in the prompt
- [x] Validation, card lines, Edit rows, skip-if-already-settled on Confirm
- [x] Recall: close, recent and today's meeting people after the fuzzy matches
- [x] Tests (service, prompt, web)
- [ ] Try it on real dictations for a week; tune the prompt rules

## Phase 26 — Learn from corrections (SPEC §2.28)
Decided 2026-10-08 (`DECISIONS.md` #24).
- [x] `original_json` next to the edited payload (migration 0021)
- [x] "Add alias X to Y?" proposal when a new name is linked to a person
- [x] Corrections and rejected items in the capture prompt
- [x] Tests
- [ ] Watch for a week: do the same corrections stop coming back?

## Phase 27 — Intake router and school deadlines from capture (SPEC §2.29)
Decided 2026-10-08 (`DECISIONS.md` #25).
- [x] `POST /api/v1/intake`: text, link, PDF, CSV, vCard routed by shape; course by code or name
- [x] Capture extracts school deadlines into `school_deadline` proposals
- [x] Tests; `Orbit Share` documented in `SHORTCUTS.md`
- [ ] Build the `Orbit Share` Shortcut on the iPhone and try it with a PDF, a link and a CSV
- [ ] Spec "a promise to myself" (tasks without a person) before building it

## Phase 28 — Smaller weekly review input with signals (SPEC §2.30)
Decided 2026-10-08 (`DECISIONS.md` #26).
- [x] State modules once; the reviewed week carries only people met, meetings and school
- [x] `signals` computed by Orbit, for the review and for reports
- [x] Size measured before and after (`scripts/measure_review_prompt.py`): 799,268 to 532,330 characters
- [x] Tests
- [ ] Read two real reviews: do the signals help, and is anything missing from `last_week`?
- [ ] Decide whether the review's 400,000-character budget should be lower (`next_week` still fills it)

## Phase 29 — A model per purpose in the API path (SPEC §2.31)
Decided 2026-10-08 (`DECISIONS.md` #27).
- [x] `ANTHROPIC_MODEL_FAST`, `ANTHROPIC_MODEL_DEEP`, `ANTHROPIC_DEEP_EFFORT`; purposes mapped to models
- [x] Adaptive thinking and effort for the review and reports
- [x] Usage names the model that answered; tests
- [ ] If the API path is ever used again: pick the three models and compare a review on each

## Phase 30 — Retry with the error, and checked flags (SPEC §2.32)
Decided 2026-10-08 (`DECISIONS.md` #28).
- [x] API retry sends the validation errors back
- [x] Flags: ambiguous name, new person, amount missing, relative date
- [x] The "Claude wasn't sure" warning replaced by flags; confidence kept, quiet
- [x] Tests
- [ ] Watch real cards: are the flags right, and is one missing (for example a currency guessed from "€")?

## Phase 31 — Capture answers at once (SPEC §2.33)
Decided 2026-10-08 (`DECISIONS.md` #29).
- [x] Save the draft first, ask Claude afterwards, for both backends
- [x] "Processing" on the card, with polling; a give-up path when Claude fails or never answers
- [x] The "Capture ready" push for both backends; tests
- [ ] On the iPhone: dictate with the Action Button; the notification should come back at once, the push after a few seconds
- [ ] Check the worker VM: does answering after the response change how long a capture waits for its slot (see C9)?

## Phase 32 — A worker slot for interactive jobs (SPEC §2.34)
Decided 2026-10-08 (`DECISIONS.md` #30).
- [x] `claim(interactive_only)` and the second worker loop; `WORKER_INTERACTIVE_SLOT`
- [x] Service unit memory limits and `DEPLOY.md` updated; tests
- [ ] On the VM: `systemctl daemon-reload && restart orbit-claude-worker`; run a review and dictate a capture meanwhile; watch `free -m`
- [ ] If memory is tight: `WORKER_INTERACTIVE_SLOT=0`, or a bigger VM

## Phase 33 — Fitness: Liftoff workouts (SPEC §2.35)
Built 2026-10-09 (route A, `DECISIONS.md` #32); the live check on the server is open.
- [x] Route A chosen 2026-10-09 (`DECISIONS.md` #32); Google sign-in, so a captured refresh token. C to F: recommended defaults, unless the owner says otherwise
- [x] `integrations/liftoff.py` client (token refresh, `post.getMyPosts`, raw JSON out), settings, `.env.example`, `tests/test_liftoff_client.py`
- [x] Refresh token captured 2026-10-09 (proxy on the iPhone); a first real call worked while developing (not run continuously). Still open: how long the token lives; rotate it (sign out and in) after the first sync works
- [x] Synthetic response in `tests/fixtures/liftoff/post_getMyPosts.json`, shaped like the real one (4 workouts: distance, assisted, drop sets, bodyweight)
- [x] Models and migration 0022, parser, idempotent sync job (`liftoff_sync`), `fitness` context provider (also in the last-week review snapshot), `/fitness` pages, tests
- [x] `DECISIONS.md` entry for the source route (#32)
- [ ] On the server: set `LIFTOFF_REFRESH_TOKEN` in `.env`, `alembic upgrade head`, restart; press Read now on `/fitness`; check that workouts arrive
- [ ] Rotate the token: sign out and in with Google in Liftoff and capture the new token straight into `.env` (the first one was pasted in a chat). Remove the iPhone proxy and the mitmproxy profile
- [ ] Watch how long the refresh token lives; add an ntfy push (once a day) when the token is rejected or the API host is retired
- [ ] Later: goal links, sessions planned vs done (needs a planning module), charts, the CLI-push fallback

## Phase 34 — Health: Garmin via Apple Health (SPEC §2.36)
Built 2026-10-09 (route H1; weight from Health). The Shortcut is the open part.
- [x] Answers to SPEC §2.36 (A: H1, D: weight from Health, the rest as recommended)
- [x] `HealthDay` model and migration 0023, `POST /api/v1/health/daily` and `GET /api/v1/health/days`, `health` context provider (also in the last-week review snapshot), `/health` page, `docs/SHORTCUTS.md` recipe, tests
- [x] `docs/shortcuts/build_orbit_health.py` generates the Shortcut (structure copied from a real exported probe; the signed file is not committed)
- [ ] Import it on the iPhone (address and token asked), check each Health step shows its type, run once, check `/health` (sleep especially); compare a real payload with `tests/fixtures/health/` (synthetic); then the 07:00 automation
- [ ] On the server: `alembic upgrade head`, restart
- [ ] Check which metrics the watch really writes to Health, and whether sleep is recorded every night
- [x] Heart rate range (`min_hr`, `max_hr`, migration 0024) added 2026-10-09
- [x] Sleep with stages from the shortcut's raw sample columns (`sleep_parse.py`, migration 0025), written against one real phone result (the committed fixture is synthetic); the shortcut runs in the evening and uses "is today"
- [ ] Later: a push when no post arrives for 3 days; HRV, Body Battery and readiness (H2) if worth it

## Phase 35 — Nutrition (no spec: the source is not chosen)
- [ ] The owner picks how food gets in (a food app that writes to Apple Health, an app export, or dictated logs); then a spec section

## Phase 36 — Places, transit times and the school calendar feed (SPEC §2.37)
Specified 2026-10-09; waiting for the owner's OK and the open questions (transit source first).
- [ ] Answers to SPEC §2.37 open questions; if Google: a Cloud project with billing, a Routes API key in `.env`, a real response saved as a fixture
- [ ] `Place`, `TravelLeg`, place links on `Lesson`, `SchoolEvent`, `Meeting` (next free migration number, 0026 is taken); school places from lesson addresses
- [ ] `integrations/transit.py` (`TransitSource`, Google source), cache, monthly cap, failure push
- [ ] Lessons, exams, deadlines and travel blocks in the ICS feed; tight-transfer notes; home never shown as an address
- [ ] `travel` in the calendar provider, `place` in the school provider; `/places` page; tests
- [ ] One real week in Apple Calendar: are the times right, are blocks where they should be?

**Done when:** a week of lessons shows in Apple Calendar with travel blocks whose times match what PID Lítačka says.

## Phase 37 — Training coach (SPEC §2.38)
Specified 2026-10-09; needs Phase 36 (travel) and the Phase 33 and 34 live checks. Nutrition stays out until Phase 35 has a source.
- [ ] Answers to SPEC §2.38 open questions; training profile filled in
- [ ] Muscle enum, `ExerciseMuscle` with the `exercise_muscles` proposal kind; Claude maps the existing exercises (one card)
- [ ] Deterministic numbers: hard sets per muscle, hours since, recovery left, Epley trend per exercise
- [ ] Liftoff sync every 30 minutes in the day; post-workout analysis (L1) with push; recovery fit with its log
- [ ] Workout builder (L2): evening proposal and Plan now; Orbit's duration estimate; slot check against lessons, travel and gym hours; confirm makes a meeting and travel blocks
- [ ] `fitness` provider gains recovery, plans and analyses; `/fitness/today`, `/fitness/profile`; tests
- [ ] Three real weeks: are the workouts sensible, do durations match, does the recovery fit move in the right direction?

**Done when:** each evening a workout for tomorrow arrives that fits the day, takes about the time it says, and after it is logged the analysis says which muscles were recovered.

## Phase 38 — Claude Code MCP server (SPEC §2.39)
- [x] `orbit/mcp_server.py` with `get_context`, tests with a mocked HTTP transport
- [x] `.mcp.json` entry; token from the shell environment
- [ ] Try it: ask Claude Code for workout ideas from the real context
- [ ] Decide open questions of §2.39 (raw workouts endpoint, health days tool)

**Done when:** a Claude Code chat answers "how was my week of training" from live Orbit data.

## Phase 18e — School agents (later, unspecified)
Study-planning agent (workload vs goals, exam prep blocks as proposals); teachers as people; SIS4/Moodle if a source is found.

## Later domains (unordered; each one starts with a spec)
- Personal finance: spending categories and monthly budget from the bank rows already imported
- Fitness & health: Apple Health metrics via a Shortcut (sleep, steps, workouts) → API, with its own provider
- Habits: definitions, check-ins from Shortcuts or Health, streaks
- School & learning: specified as Phases 18 to 18e above (`SPEC §2.20`)
- Nutrition: source to be decided before spec
- Agent autonomy above L2: one capability at a time, each with a `DECISIONS.md` entry

---

## Backlog (after v1)
See `SPEC.md §5`. Suggested order: PAY by square / QR Platba → iMessage/call-history last-contact → MCP server → IG export import → PSD2 auto-feed.
