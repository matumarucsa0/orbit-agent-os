# Orbit — Decision log

Orbit is built only through AI coding sessions. Each session starts without memory of earlier sessions, so this file
keeps the *why* behind decisions that the code and `SPEC.md` do not explain. Before reversing anything listed here, ask
me first.

Add an entry when a decision:
- changes direction, scope or architecture,
- raises an agent capability on the autonomy ladder (`VISION.md §3`),
- adds a dependency, a data source, or a new kind of personal data sent to Claude,
- chooses one option over a real alternative, and the reason would not be obvious later.

Format: newest at the bottom, numbered, never renumbered. If a decision is replaced, keep it and mark it
`Superseded by #N`.

```
## N. Title (YYYY-MM-DD)
Status: accepted | superseded by #M
Context: what made this necessary
Decision: what we do
Consequences: what follows, what we give up
```

---

## 1. Single-user, self-hosted, Apple-first interface (2026-09)
Status: accepted
Context: Orbit is a personal tool for one person who uses an iPhone and a Mac.
Decision: Orbit is one user with no tenancy. It runs on my own server. Its interfaces are iOS Shortcuts, ntfy push, an
ICS feed and a server-rendered mobile-first web UI (FastAPI + Jinja2 + HTMX, no SPA, no JS build step).
Consequences: no account system and no per-user data scoping. A native app is out of scope.

## 2. Claude proposes, I confirm (2026-09)
Status: accepted
Context: LLM output and automatic matching are sometimes wrong, and wrong debts or notes damage trust in the data.
Decision: everything produced by Claude or by automatic matching becomes a `Proposal` in the Inbox. Only the documented
contact-sync exception (`SPEC.md §2.13`) writes without a proposal.
Consequences: more taps for me, but the data stays trustworthy. This is level L2 of the autonomy ladder and the default
for all future agents.

## 3. Orbit grows into a personal operating system (2026-10-05)
Status: accepted
Context: I want one connected system for fitness, school, nutrition, habits, goals, calendar, finances and people, with
AI agents that understand my life and help manage it, instead of separate trackers.
Decision: grow this codebase instead of starting a new one. The CRM becomes the People, Ledger and Promises modules.
`docs/VISION.md` is the north star. New domains are added one roadmap phase at a time, each with a spec first.
The shared context layer and Goals come before any new tracking domain, and a weekly review agent is the first proof
point.
Consequences: the name stays "Orbit" and the package stays `orbit`. Every new module must give agents a context
provider and link to shared entities. Breadth is limited on purpose until the weekly review proves useful.

## 4. Autonomy ladder; L2 is the default (2026-10-05)
Status: accepted
Context: the long-term goal includes agents that act for me, which needs a safe way to give them more autonomy.
Decision: use the five levels in `VISION.md §3`. Any capability above L2 needs its own entry in this file, scoped to one
kind of action, with Undo or an audit trail.
Consequences: no global "auto mode". Autonomy grows one capability at a time.

## 5. Proposal kind registry and context providers as seams (2026-10-05)
Status: accepted
Context: Phase 13 prepares the code for more modules and agents without changing behaviour. `services/inbox.py` chose
what to do per kind with `if/elif`, and nothing gave agents a bounded, masked view of a module.
Decision: (1) A `ProposalHandler` per kind (payload model, apply, optional summary and reopen check), defined by the
module that owns the kind and registered in `inbox.py`. (2) One context provider per module in `orbit/services/context/`,
returning a frozen Pydantic model for a `ContextWindow` and optional `ContextFocus`; all free text goes through one
`text()` helper (mask, then clip) and every list has a cap. Provider models carry no handle values, IBANs, phone numbers or
emails. (3) The brief and capture prompts are not rewired to the providers in this phase (see ROADMAP Phase 13).
Consequences: a new kind or module adds its own file plus one line. Provider caps are conservative guesses; Phase 14
sets the overall size budget. Free text longer than 200 characters is clipped for agents that read providers, which is a
choice about what Claude will see and may be revisited with the context layer.

## 6. Context snapshot: size budget, no stored copy (2026-10-05)
Status: accepted
Context: Phase 14 combines the module providers into one snapshot for agents (`SPEC.md §2.15`). It had to fit a size
limit, decide where masking is enforced, and decide whether to keep what an agent saw.
Decision: the snapshot is built on demand, never stored. The limit is 12,000 characters of JSON (`CONTEXT_BUDGET_CHARS`).
Over the limit, items are cut from the end of the biggest module's list, one at a time, never totals, and the cut is
reported in `budget.trimmed`. The layer masks every string again after the providers. The debug view (`/context`) and
`GET /api/v1/context` call the same `build_context` an agent will. Person names are not excluded in any way.
Consequences: no audit trail of past agent inputs; the debug view shows today's data, not what an agent saw last Sunday.
If Phase 16 needs "why did the review say that", store the review's input then, with its own entry. The 12,000 limit is
a guess to tune from a real week; the first sign it is wrong is `budget.trimmed` being non-empty in the debug view.

## 7. Generous context for agents; privacy is not the limit (2026-10-05)
Status: accepted. Replaces the first sentence of `CLAUDE.md` rule 5 ("send the minimum personal data needed") and the
"minimum" wording in `VISION.md` principle 6. `DECISIONS.md` #6's 12,000-character budget is superseded by this entry.
Context: I want agents to make the best decisions they can. A small, heavily clipped context hides information that
changes a decision, and Claude's context window is far larger than anything Orbit builds. I do not mind Claude seeing my
health, money, school or relationship data.
Decision: the context layer sends rich data. Default `CONTEXT_BUDGET_CHARS` is 400,000 (about 100,000 tokens; settable up to
4,000,000). Provider caps only stop runaway queries: 300 people (50 notes and 20 interactions each), 500 open debts, 100
balances, 100 recently settled debts (90 days), 300 meetings, 500 open promises, 100 recently closed promises (30 days).
Free text is clipped at 2,000 characters, names at 200. People who matter to a window include everyone with a meeting
or something open between us, not only birthdays and due contacts. Goals (Phase 15) send title and `why`, and capture
sees goal titles. Still masked: IBANs, account numbers, full phone numbers and emails, because they carry no signal for a
decision and are the only real fraud risk. Handle values are left out for the same reason.
Consequences: more tokens per agent call (cost and latency go up with the data). Claude sees my whole life, so the Anthropic
API terms and my API key are the trust boundary. Reversible: lower `CONTEXT_BUDGET_CHARS` or the caps. This does not
change who confirms: agents still only propose (L2).

## 8. Goals: text targets, links through the capture draft, review by hand (2026-10-05)
Status: accepted
Context: Phase 15 adds Goals (`SPEC.md §2.16`) as the spine agents weigh everything against, and the roadmap said capture
links to goals are "a new proposal kind".
Decision: (1) A goal's `target` is free text. Progress numbers wait for the modules that measure them from a passive source.
(2) Only active goals have a rank, kept contiguous by the service and by a database check (`status = 'active'` iff `rank IS
NOT NULL`). (3) A capture's promise draft carries an optional `goal`; Claude may suggest one of the listed active goals, I
confirm or change it in the Inbox card. No new proposal kind now: the promise does not exist before confirm, so there is
nothing to link. A `goal_link` kind for existing promises and people is added when the Phase 16 review agent first produces
it. (4) The weekly re-rank is manual (`/goals/review`); the agent will propose a re-ranking into the Inbox. (5) The context
layer never trims goals; the other modules lose items first. (6) Goals live in the top bar, with a Home strip and one
Sunday-digest line when nothing was reviewed for 7 days.
Consequences: goal titles and `why` are sent to Claude by capture and by the context layer (`DECISIONS.md` #7). A goal can
exist without any link, so nothing forces me to maintain links; the review agent is expected to suggest them. Deleting a goal
clears the link on its promises and keeps the promises.

## 9. Weekly review: keep its input, four proposal kinds, no autonomy above L2 (2026-10-05)
Status: accepted. Revisits #6 (no stored snapshots) for the review only.
Context: Phase 16 is the first agent (`SPEC.md §2.17`) and the proof point in `VISION.md §7`. #6 said to store the review's input if
"why did it say that" mattered.
Decision: each review stores the two context snapshots, the previous review's memory and the Inbox lists Claude was given
(`weekly_review.input_json`), so any claim can be checked. The review is read-only (L1); everything it wants changed is a proposal
(L2) of four kinds: `follow_up` (creates an open promise), `promise_due`, `goal_rerank`, `goal_link`. They are checked against the
ids in what Claude was shown, capped at 15, and each confirm re-checks the current state. Nothing it proposes contacts anyone, moves
money or settles a debt. One review per planned week, a unique key, claimed before Claude is asked; a ready review is final.
Consequences: the database holds the data twice for each week reviewed (about the size of the snapshots; cheap to prune later). A
review can cost up to about 200,000 input tokens (two snapshots at `CONTEXT_BUDGET_CHARS`) once a week. No capability above L2 was added.

## 10. The weekly review may be made by Claude Code on the server, on a subscription (2026-10-05)
Status: accepted
Context: the review needs Claude, and the API is billed per token while the owner has a Claude subscription. They checked that running Claude Code
on their own server with their subscription is allowed for them; Orbit does not borrow a login or run Claude Code itself.
Decision: the review is split into `prepare` and `submit` endpoints (`SPEC.md §2.18`) behind a separate, review-only token. A script on the VM
host, started by a systemd timer, calls `prepare`, runs `claude -p` with **every tool disabled** from an empty directory, and calls `submit`;
Orbit checks the answer exactly as it checks an API answer (ids against the stored input, caps, no duplicates). The API path stays and shares the
code. `CLAUDE.md` rule 5 is reworded: the external session is a client of Orbit, not a call Orbit makes.
Update 2026-10-06: the owner will run production on Claude Code only, with no API key. The API path stays in the code but is unused; capture, briefs,
drafts and bank classification run in their fallback modes. Adding Claude to them later needs the same prepare/submit shape or a new entry.
Consequences: no per-token cost for the review, but it counts against the subscription's usage limits and depends on a long-lived login on the
VM. Capture, briefs and drafts still need an API key to be smart (they run inside a request); without one they fall back to a note draft, no
summary line, a plain wish, and rules-only bank classification. The text the session reads includes notes and titles written by other people,
which is why it has no tools: a hostile note can at worst skew the review, and every proposal still needs a tap. L2 stays the ceiling.

## 11. One Claude Code worker behind a job queue; no API key in production (2026-10-06)
Status: accepted (the open questions of `SPEC.md §2.19` were answered with the recommendations)
Context: the owner runs production on their Claude subscription through Claude Code, no API key (#10 did this for the review). The other Claude
features run inside requests and fall back to dumb modes without an API client.
Decision: every Claude call goes through a queue (`claude_job`) served by one worker on the VM that runs `claude -p` with no tools. A
`JobClaudeClient` implements the existing Protocols, waits a bounded time and falls back as today, so services do not change. The review moves
onto the same worker. `CLAUDE_BACKEND=api` keeps the API path as a switch.
Alternatives: a separate script per feature (rejected: several processes would compete for 1 GB of RAM and each needs its own monitoring); a
full async rewrite of capture and briefs (rejected: larger change, the bounded wait plus the late upgrade gives the same result).
Consequences: Claude features answer in seconds to a minute, not instantly, and share the subscription's limits. In return: no per-token bill,
one place that shows auth, rate-limit and queue state, and a one-line switch back to the API. Stored prompts hold personal data and are cleared
after 7 days. L2 stays the ceiling.

## 12. School data comes from SIS token feeds and public pages; Orbit never holds a SIS login (2026-10-06)
Status: accepted (2026-10-06; answers in `SPEC.md §2.20`)
Context: the school domain needs the timetable, exams and deadlines of the owner's university study (the student information system, SIS; `sis.example.edu/studium` stands for its address). SIS is a legacy
server-rendered site with no API. Login is CAS single sign-on; the session is a URL parameter; **SIS allows one session per user, so any automated
login logs the owner out of their own browser**, and some modules (`prihlastaz`) end the session when opened.
Decision: Phase 18 reads only (a) the calendar feeds SIS itself offers behind a long-lived account token (`ical.php?typ=...&token=...`: timetable,
registered exam terms, personal SIS events), which work without a session or cookie, and (b) public pages (`predmety` course pages, anonymous
`harmonogram`). The token lives in `.env`. Orbit never stores CAS credentials or a session id, never logs in, never calls a SIS write action
(enrol, register for an exam, rotate the token). Data that only a logged-in session shows (enrolled subjects and credits, waiting list, enrolment
windows, grades, teacher announcements) is Phase 18b and comes through a helper running in the owner's own browser that POSTs the pages to Orbit;
Orbit parses them server-side.
Alternatives: storing the CAS password or scripting the CAS login (rejected: credentials for the whole university account, 2FA, and it would
end their browser session); a user-pasted session id polled by Orbit (rejected as the first step: dies after 1 h idle or at the next login anywhere,
8 h hard cap); scraping the timetable HTML pages (rejected: `robots.txt` disallows `/studium/rozvrhng/` and the feeds already carry the same data);
the structured xCal feed (kept as an option: separate key, timetable only).
Consequences: no credentials at risk and no surprise logouts, and the feed is the sanctioned machine interface. In return: the feeds do not carry
credits, enrolment state, waiting-list position or grades until 18b; the DESCRIPTION text is parsed (teachers cannot be split reliably); the
`term_st2` and `stev` feeds were empty during research, so their shape is verified only when the first exam is registered; if the owner rotates the
token on SIS, Orbit stops until `.env` is updated (a daily push says so). Lessons are source data (L0), not proposals; autonomy for the whole
module stays at L1 in Phase 18. School data goes to Claude in full (#7), without SIS identifiers; the national birth number, address, bank account and
student numbers shown on SIS profile pages are never read or stored.

## 13. Owl is read with a session cookie the owner copies from their browser (2026-10-06)
Status: accepted
Context: the Postal Owl (`owl.example.edu`, homework submission and grading) has no student API or feed: API keys are per course and teacher-only,
and access-key logins are made by an admin. Login is CAS. Unlike SIS, Owl has no one-session-per-user rule, so a second session does not log the owner out.
Decision: the owner copies the `owl_session` cookie into `.env` (`OWL_SESSION_COOKIE`). Orbit uses it for `GET` of the home page and the course pages only,
never a POST, never topic pages (they mark threads read), never account pages. An expired cookie produces one push per day; the owner pastes a new one.
Alternatives: the 18b browser helper posting the HTML (the recommended option: Orbit holds no session; rejected by the owner as more friction for the same
data); scripting the CAS login (rejected: university credentials and 2FA).
Consequences: Owl data arrives in Phase 18 with no helper and no daily click. In return Orbit holds a live session for the owner's whole Owl account, which
could submit or post if misused: the code allows only the listed GET paths (a test enforces it), and the cookie is a secret like the other tokens.
Its lifetime is unverified (Flask default 31 days from login), so a manual paste is needed now and then.

## 14. The school map is an interactive data flow map drawn by a small inline script, no graph library (2026-10-06)
Status: accepted (revised the same day: the first version picked a vendored Cytoscape.js)
Context: the owner wants the school map (`SPEC.md §2.20`, Phase 18c) interactive: pan, zoom, drag, click to trace. Orbit's UI is server-rendered Jinja +
HTMX with no SPA and no JS build step (`CLAUDE.md` stack), and new dependencies need their OK (rule 8). They approved a data-flow-map mockup
(sources → processing → courses → outputs, Bézier edges with width by volume and moving dots, lineage tracing) that was drawn without any library.
Decision: Orbit computes the layout in Python and serves it as JSON; the map page draws cards as HTML and edges as inline SVG with one inline
script (about 300 lines) for pan, zoom, drag, edge redraw and tracing. No graph library, no CDN, no npm, no bundler. Every other page stays HTMX.
`pypdf` is added in the same phase for PDF text (the owner's OK).
Alternatives: vendored Cytoscape.js (the first choice: a dependency, and its node labels cannot show the card layout of the mockup); server-side
SVG with hover only (not interactive enough for the owner); a full SPA framework (against the stack).
Consequences: no third-party JS to update; the map's script is Orbit's own code and needs its own tests (layout in Python with unit tests; the
script kept small and checked by a browser smoke test). The map needs JavaScript; the list view is the fallback on phones and without JS. This
is an exception for the map page, not a precedent for other pages.

## 15. Schedule values are edited in the app and stored as overrides in the database; Run now re-checks, never forces a push (2026-10-07)
Status: accepted
Context: every schedule value was an env setting or a constant in a service file, so changing the 09:00 reminders, the Sunday digest or a poll interval meant editing `.env` and restarting. The owner wants one place in the app to edit them, and a way to run a job at once. `SPEC.md §2.21` listed eight open questions; they accepted all recommendations.
Decision: a stored override (`AppSetting`) beats the env value, which beats the code default. All former constants became `Settings` fields so there is one way to read a value, and `OverrideStore` lays the overrides over `Settings`. Edits apply live (no restart). Secrets are never editable. Run now calls the same function the scheduler calls: the check jobs (reminders, school, meetings) only record what is already due, dedupe keys still hold, and quiet hours still apply, so nothing is sent early or twice. Backup time and the systemd timers stay read-only rows because the web process cannot change them. Last run, result and next run are kept per job (`JobRun`).
Alternatives: write changes back to `.env` (needs a restart, and a web app that edits its own secrets file is risky); a "send the digest now" or "send tomorrow's lessons now" button (a forced early push is a separate decision, later); no job status (hides failures); a section on the Claude ops page (the schedule is not about Claude).
Consequences: `.env` values are now defaults that an edit can shadow, and the page shows both ("Edited. From .env: X", Reset). A value typed equal to the env value removes the override. A stored value that stops validating after a later release is ignored with a log line. The weekly review's Run now exists only when Orbit itself holds the Claude API client; with the worker the worker starts it. The one-process rule of the scheduler (`SCHEDULER_ENABLED`) is unchanged.

## 16. Custom review reports are read-only, on demand, and always produce something (2026-10-07)
Status: accepted
Context: the weekly review (`SPEC.md §2.17`) is fixed to the last and next week. The owner wants a report for a date range they set ("how did October go", "the two weeks before the exam"). `SPEC.md §2.22` listed nine open questions; they accepted all recommendations.
Decision: a separate agent, `ReviewReport`, next to the weekly review (not a change to it). Any range up to 92 days, past or up to 30 days ahead, with optional focus text and one goal, person or course. It reads the same masked context through `build_context` and writes a read-only report: autonomy L1, no proposals, no domain writes. It goes through the same Claude paths as every other feature (`CLAUDE.md` rule 5): the API client, the Claude worker as job kind `report`, or, with neither, a plain report of counted numbers and facts. The numbers are counted by Orbit from the snapshot, not written by Claude. Reports are kept until deleted, one is made at a time, and a push says when one is ready.
Alternatives: extend `WeeklyReview` with a range (its identity is the planned week and it creates proposals); refuse without Claude (the page would be empty on the no-key production setup when the worker is away); proposals from a report (the weekly review stays the one that proposes); a longer range with a coarser snapshot (it would say little and fill the context budget).
Consequences: goals, open promises and debts are current state, so the report says "open now", not "open then", and states that on the page. A report stores a copy of personal context (`input_json`), which Delete removes; the stored input is what "What Claude saw" shows and what the answer is checked against. A heavy range can take minutes, so the page refreshes itself and the push is the way to leave and come back.

## 17. School resources: Orbit fetches, Claude only reads text, uploads stay out of the database backup (2026-10-07)
Status: accepted
Context: `SPEC.md §2.20` Phase 18c lets the owner add course pages, PDFs, repositories and notes that Claude reads for deadlines and rules. `DECISIONS.md` #11 says the worker runs `claude -p` without tools, and #14 chose the map's rendering. Questions E to G were not asked: the recommendations stood.
Decision: Orbit does all fetching (GET only, polite, `robots.txt`, no local addresses, no login pages), extracts text (`pypdf` for PDFs, no OCR) and keeps old text for a diff. Claude gets text only, through the usual paths, within a daily read budget (20, editable). Its answers become `school_deadline` and `course_fact` proposals (L2) and suggested resources, never direct writes. A suggestion is fetched only after the owner keeps it. Uploaded PDFs are stored by hash in `SCHOOL_FILES_DIR` (its own Docker volume); the database backup holds their extracted text, not the file.
Alternatives: store the PDF bytes in the database (would grow the nightly dump with files nobody reads again); let Claude browse (not allowed by #11); fetch suggestions automatically (a page the owner never chose would be read by Claude and could carry instructions).
Consequences: a lost upload loses only the original file; its text, rules and proposals stay. Page text is data, never instructions: the reader's output is validated and confirmed by the owner before anything is written. The map's pointer and zoom code is Orbit's own script and has no browser smoke test yet (ROADMAP 18c).


## 18. CI runs only the tests a change can reach; `main` runs everything (2026-10-07)
Status: accepted
Context: the Postgres job takes about 15 minutes and the whole pipeline runs on every push and pull request, mostly for changes (docs, one template, one service) that touch a small part of 1500 tests. The owner asked for a way to run only the tests for changed code.
Decision: `scripts/select_tests.py` (standard library only) follows the static imports of every test file and selects the tests that reach a changed module. Endpoint tests (those taking the `client`, `api`, `web` or `app` fixture) also reach the routers named like the test file, because the app fixture hides that import. A changed template or static file selects all endpoint tests; docs and deploy files select none. Anything shared or unknown (`pyproject.toml`, `uv.lock`, `conftest.py`, `orbit/domain/`, migrations, `.github/`, settings, a deleted module, an unrecognised file) selects everything. A new `select` job feeds both `checks` and `postgres`; ruff, mypy and the Docker job always run. A push to `main` and tags always run the full suite.
Alternatives: `pytest-testmon` (a new dependency and a cache to keep between runs; its coverage-based map is more exact, and it stays an option if this proves too coarse); a hand-kept map of folders to tests (needs upkeep and silently goes stale).
Consequences: a pull request can be green while a test it did not select would fail, for example through a dynamic import or a template used by a non-endpoint test. The full run on `main` catches that after the merge. When a miss happens, add the rule to `RUN_ALL` or the selector, with a test in `tests/test_select_tests.py`.

## 19. A future appointment from capture is a `Meeting` made by Orbit, not a note (2026-10-07)
Status: accepted
Context: dictating "haircut on Friday at 15:00" produced a `note` on the barber's page: wrong (nothing happened yet), never shown at the right time, nothing in the calendar. `SPEC.md §2.23`.
Decision: capture gets an `appointment` item (inside the `capture` proposal, confirmed in the Inbox). Confirming creates a `Meeting` with `calendar_name = "orbit"` (reusing briefs, the Meetings page and the ICS feed). Orbit does not write to iCloud. The extractor is told a future plan is never a note.
Alternatives: a promise to myself with a due date (no time of day, wrong wording); a new `Appointment` model (a second meeting object to keep in step with briefs and the feed); writing the event to iCloud over CalDAV (a new outward-facing write); dropping such text silently.
Consequences: an appointment can be edited or cancelled only in Orbit, not from Apple Calendar (the feed is read-only there). One more Claude-only item: without Claude the text stays a note draft, as before.

## 20. Extra navigation links live in a "More" menu: a `/more` page on phones, a dropdown on desktop (2026-10-08)
Status: accepted
Context: the top bar held nine extra links next to five tabs. At 375 px the page was 621 px wide and the phone tab bar fell off screen.
Decision: five tabs stay (Home, Inbox, People, Meetings, Ledger). Every other link and Log out go into one list (`MORE_LINKS`), shown as a 6th "More" tab that opens `/more` under 768 px and as a `<details>` dropdown from 768 px. School and Goals are the first rows; no tab is replaced.
Alternatives: School replaces Ledger during the semester (one tap less for School, Ledger one tap deeper); seven tabs (too tight at 375 px); a slide-up sheet (needs JS, no back button).
Consequences: School, Goals and Review are one extra tap. If School proves to be daily, give it a tab and revisit this entry.

## 21. Home shows 5 attention rows in total; the Ledger shows 20 rows per section (2026-10-08)
Status: accepted
Context: Home "Needs attention" listed every due promise and lent item (188 rows on the audit data) and pushed meetings and school below it; the Ledger drew every open debt and promise (846 KB).
Decision: Home order is capture, search, meetings today, school, Inbox, weekly review, Needs attention, goals, money, birthdays, feed. Needs attention shows at most 5 rows across promises, items and catch-ups, most urgent first, with "…and N more" to a new `/attention` page. Each Ledger section shows 20 rows with "Show all N" (`/ledger?all=<section>`); totals use every row.
Alternatives: 5 per kind on Home (up to 15 rows); expanding in place with `/?all=1`; collapsing sections with JS; paginating the Ledger.
Consequences: a sixth urgent item is one tap away. `/attention` is one more page. If 5 or 20 feels wrong on the phone, change `HOME_ATTENTION` or `LEDGER_PAGE`.

## 22. A dictation's cards share a `group` token, and `new_person` cards carry the dictated text (2026-10-08)
Status: accepted
Context: a `new_person` card showed only a name, and its dictated text was stored only on a sibling `capture` card that does not exist when the dictation mentions nobody but new people. Five unknown names meant five cards and five rejects.
Decision: `NewPersonPayload` gets `source_text` (the dictation, up to 2,000 characters) and `group`; `CapturePayload` gets `group`. One random token per dictation. "Reject all N from this capture" rejects every pending card with the token (reversible). Capture forms show "Sending to Claude…" on the submit button while they wait.
Alternatives: look the text up from a sibling `capture` card (breaks when none exists); group by `source_id` (empty for non-meeting dictations); a confirm dialog on reject-all (reject is already undoable).
Consequences: the dictation text is stored twice when both cards exist. Older cards have no text and no button. When C8 makes capture answer at once, the waiting label is rarely seen.

## 23. Capture proposes settles and promise closes for the debts and promises it was shown (2026-10-08)
Status: accepted
Context: "Paťo mi vrátil 12 €" is an update to an existing debt, but capture could only create things, so it made a note or a wrong new debt.
Decision: inside the existing `capture` proposal (L2, editable, never applied without Confirm) two new item kinds: `settle` and `complete_promise`. Claude is shown the open debts and promises of its candidate people (10 per person, 40 in all, masked) and may only refer to those; Orbit drops a ref that was not shown, a debt in the other direction, and a part bigger than the debt (the last with a warning). On Confirm an item that was settled in the meantime is skipped with a message, the rest applies. Candidates are the text's matches plus people tagged `close`, talked to in the last 14 days or in a meeting today.
Alternatives: a separate proposal kind per update (more cards); let Claude name a debt by amount (ambiguous); show every open debt (large prompt, more wrong matches).
Consequences: more people and rows go to Claude on every capture (`DECISIONS.md` #7 allows it). A wrong ref can still be confirmed by mistake, so the card shows what it settles and the amount. Item and favor settles use the same kind.

## 24. Keep the proposed payload next to the confirmed one, and tell Claude about corrections and rejections (2026-10-08)
Status: accepted
Context: the owner repeats the same fixes on capture cards, and a name they link to a person is forgotten, so the next dictation proposes a duplicate.
Decision: `Proposal.original_json` stores what Claude proposed; the capture prompt gets up to 6 correction lines and 6 rejected-item lines from the last 60 days; linking a new name to a person proposes `add_alias` (a normal Inbox proposal, never automatic).
Alternatives: write the alias automatically on link (breaks rule 1); a separate corrections table (a second source of truth); fine-tuning or a memory file (not available, not needed for a few lines).
Consequences: the prompt grows by up to 12 short lines. The difference check is simple (same index, same field), so a card edited by adding or removing items teaches nothing. Old rows teach nothing.

## 25. One intake endpoint routes by the shape of what was shared, without Claude (2026-10-08)
Status: accepted
Context: dictation, a bank CSV, a course PDF and a course link each had their own Shortcut, and capture knew nothing about school deadlines.
Decision: `POST /api/v1/intake` decides by content (PDF magic, vCard header, a bank parser's header, a link, else text) and calls the existing capture, bank import, school resource and contacts import. A course is attached only when exactly one current course matches by code or name; otherwise the resource waits in Unsorted. Capture gains `school_deadlines`, made as separate `school_deadline` proposals. A file of an unknown kind is refused. Tasks without a person are not built.
Alternatives: ask Claude to classify every share (slower, costs a call, and the shape already says what it is); guess a course when several match (silent mis-filing); make `Promise.person_id` optional now (touches the Ledger, reminders, digest and review).
Consequences: one Shortcut can replace several. A new kind of file needs a new rule in `decide`. A CSV from an unknown bank gives a clear 422 instead of a guess (`CLAUDE.md` rule 9).

## 26. The weekly review sends state once and counts collisions itself (2026-10-08)
Status: accepted
Context: the review sent two full snapshots (about 200,000 tokens on audit-size data), with goals, people, money and promises twice, and left Claude to find overlaps and overdue items inside them.
Decision: the planned week's snapshot is the full state; the reviewed week carries only people met, meetings and school. Orbit computes `signals` (clashes, overdue, people past cadence, net money, bunched deadlines, goals with nothing linked) and Claude is told to start from them. Reports get signals too. The character budget stays as it is.
Alternatives: one snapshot with a window spanning both weeks (loses which week things belong to); a smaller budget (a product choice about how much Claude sees, `DECISIONS.md` #7); letting Claude compute the signals (slower, and wrong more often than a query).
Consequences: about a third less input on audit-size data, and the facts Claude must not miss are short and exact. A new kind of collision is a new function in `signals.py`. Claude cannot see last week's goal state, only the current one.

## 27. Fast, default and deep models in the API path, chosen by env (2026-10-08)
Status: accepted
Context: the API path used one model for a one-line brief and for the weekly review. Model names must come from env (`CLAUDE.md` rule 5).
Decision: `ANTHROPIC_MODEL_FAST` for meeting links, bank row sorting and briefs; `ANTHROPIC_MODEL` for capture, drafts and the school reader; `ANTHROPIC_MODEL_DEEP` for the weekly review and reports, with adaptive thinking at `ANTHROPIC_DEEP_EFFORT` (default `high`, `off` to skip). Unset models fall back to the default one. The worker path is unchanged.
Alternatives: one model with effort per purpose (cannot make a brief cheaper than the model's floor); a thinking token budget (removed on current models); a settings page for models (an env file is enough for one user).
Consequences: three names to keep current when models change. A deep model without adaptive thinking needs `ANTHROPIC_DEEP_EFFORT=off`. Nothing changes until the settings are used.

## 28. A retry tells Claude what was wrong; cards show flags Orbit computed, not Claude's confidence (2026-10-08)
Status: accepted
Context: a retry repeated the same question, and the "Claude wasn't sure" warning followed Claude's own poorly calibrated `confidence`.
Decision: the API retry adds the failed fields to the prompt (the worker already did). Capture cards show flags Orbit checks itself (a name that fits two people, a new person, a missing amount, a date from a relative word), recomputed on every draw; `confidence` stays stored and appears only as a quiet line below 60 %.
Alternatives: keep the confidence warning (noise); store flags in the payload (go stale after an edit); send the failed text back (the SDK does not return it).
Consequences: a flag is a rule that can be wrong or missing, and a new one is a small function in `capture_flags.py`. The ambiguity check reads all non-archived people on every card draw (cheap at this size).

## 29. A capture saves its draft first and asks Claude afterwards, on both backends (2026-10-08)
Status: accepted
Context: the capture request waited for Claude (up to 45 s with the worker, up to two minutes with the API), the Shortcut hung, and a worker answer that came late was the exception path.
Decision: every capture with a Claude client saves the note draft marked "processing" and returns at once; the worker queue or a background task asks Claude and replaces the draft; a push says when. A failed, declined, expired or unanswered capture stays a note draft and stops saying "processing" (and never after 10 minutes on screen).
Alternatives: keep waiting but with a shorter limit (still slow, and the fallback is worse); a spinner page that waits for the answer (holds a request open); skip the draft and show nothing until the answer (loses the text if Claude never answers).
Consequences: `/api/v1/capture` no longer returns the extracted items, only the draft; the Inbox shows them a few seconds later and the push tells him. The API backend's background task is lost if the server restarts mid-answer, which leaves a plain note draft.

## 30. The worker keeps a second slot for captures and drafts (2026-10-08)
Status: accepted
Context: one job at a time meant a long school read, report or the weekly review delayed a capture someone was waiting for.
Decision: two claim loops in the worker; the second takes only interactive jobs and never starts the review. `WORKER_INTERACTIVE_SLOT=0` returns to one job at a time. The service unit allows up to 900 MB with a 600 MB soft limit.
Alternatives: a kind filter on one loop (still one process at a time); a worker pool (more memory than the VM has); running reviews only at night (does not help school reads).
Consequences: two Claude Code processes can run at once on a small VM, and subscription usage in a window is denser. If the VM struggles, turn the slot off or give it more memory.

## 31. School Microsoft 365 uses route B: published calendar ICS plus mail shared by the owner (2026-10-09)
Status: accepted (route chosen; the Outlook web checks that confirm it are still open)
Context: `SPEC.md §2.20` Phase 18d offered route A (Microsoft Graph, device-code sign-in) and route B (published ICS, share-sheet Shortcut or forwarding). On 2026-10-09 the owner registered an Entra app (multi-tenant, public client, `Mail.Read Calendars.Read offline_access`) and signed in with the school account. The university tenant answered "Orbit needs permission to access resources in your organization that only an admin can grant". User consent is off, so route A needs a university admin. The owner does not want to ask the university IT office.
Decision: route B. Calendar: a published-calendar ICS URL in `.env` (`OUTLOOK_ICS_URL`), read like the SIS feed (Teams meetings are in it). Mail: an iOS share-sheet Shortcut posts a mail to `POST /api/v1/school/mail`; a forwarding rule is used instead only if Outlook web offers it. Teams has no API access: the owner uses Teams only for assignments of NINF120 (Principy počítačů), and Teams assignment notifications arrive as mail, matched to the course by code and name. Orbit never stores a Microsoft password or token. Mail handling, `SchoolMail`, Claude access and the "never" list stay as in `SPEC.md §2.20` 18d.
Alternatives: route A with an admin approval request to the university's IT department (rejected: unlikely to be granted, slow); Teams channel and chat access (needs admin consent even with Graph); skipping school mail (loses the NINF120 news and assignment mails).
Consequences: no live Teams or mail polling. Mail reaches Orbit only when the owner shares it, or automatically if forwarding is allowed. Chat and channel posts are visible only as notification snippets. Still open before building: does Outlook web offer "Publish a calendar" and forwarding, and what sender and subject do Teams assignment mails use. The Entra app registration is unused and can be deleted. If the university ever allows user consent, route A can be added without removing route B.

## 32. Liftoff workouts: Orbit calls the app's private API with a captured refresh token (2026-10-09)
Status: accepted (route chosen; parser and models wait for a real response)
Context: `SPEC.md §2.35`. Liftoff has no public API and no export. The owner signs in with Google, so the account has no password and `user.signIn` cannot be used. An unofficial CLI (`quantcli/liftoff-export-cli`, MIT) shows the private tRPC API, a refresh-token path and a `post.getMyPosts` call that returns every workout.
Decision: route A. Orbit refreshes an access token from `LIFTOFF_REFRESH_TOKEN` (captured once from the app's traffic with an HTTPS proxy) and reads workouts read-only, once a day and on "Sync now". The token lives in `.env` only, never in the database, a log, a prompt or an error message. The API host (version in its name) is `LIFTOFF_API_BASE`.
Alternatives: run the CLI on the Mac and push JSON to Orbit (no credentials on the server, but manual); Apple Health only (no sets or reps).
Consequences: the API is private and can change or retire without notice ("server is deprecated" is reported with the fix); a dead refresh token means a new capture; third-party terms may forbid this use. If it proves too fragile, fall back to the CLI push route without changing the models.

## 33. Lessons, exams and travel blocks go into Orbit's own ICS feed (2026-10-09)
Status: accepted (reverses `SPEC.md §2.20` open question 3)
Context: Phase 18 kept lessons out of `/calendar/{ICS_TOKEN}.ics` so that a SIS subscription and Orbit's feed would not show every lesson twice. The owner is not subscribed to the SIS calendar, and they want lessons in their calendar together with travel times from a transit API and planned workouts (`SPEC.md §2.37`, `§2.38`).
Decision: Orbit's feed carries lessons (cancelled ones removed), exams and deadlines, travel blocks between places, and confirmed workouts. The SIS feed stays a source for Orbit only.
Alternatives: subscribe the SIS feed directly (no travel blocks, cancelled lessons stay until SIS drops them, two calendars to look at).
Consequences: if the owner ever subscribes the SIS feed as well, lessons show twice. Home is written as "Home" in event text, never as an address, because the feed URL is a bearer secret that Apple may cache.

## 34. Claude Code reads Orbit through a local MCP server over the HTTP API (2026-10-15)
Status: accepted
Context: `SPEC.md §2.39`. The owner wants to chat about their Orbit data in Claude Code. The data is private and masked by the server's context provider.
Decision: a stdio MCP server inside the `orbit` package (`mcp` added to the dev group; it runs only on the Mac) that calls `GET /api/v1/context` with the bearer token. Read-only; it never opens the database.
Alternatives: pasted curl output (manual); an MCP server that imports services and the database (bypasses the masked provider, couples to the schema); a separate package (more setup for one tool).
Consequences: the chat sees exactly what an agent sees, no more. Any new tool needs a matching API route first. The token sits in the shell environment of the Mac.

## 35. A demo mode: `seed-demo` and an offline FakeClaude, chosen by `ORBIT_CLAUDE_MODE` (2026-11-24)
Status: accepted
Context: the app holds personal data, so it could not be shown or screenshotted, and the Inbox, capture and weekly review need Claude to do anything. A clean checkout had no way to see them. About 1750 tests assume that with no key and no worker, Claude is `none` and a capture stays a note draft, so that default must not change.
Decision: (1) `python -m orbit.cli seed-demo` fills an empty database with a fictional persona (Alex Novak, a student and developer in Prague): people, debts (some overdue), goals, promises, meetings, a month of bank rows written as real Tatra (cp1250) and Revolut CSV text and imported through the normal import path, workouts through the Liftoff parser, sleep days, dictations through the normal capture service, and a weekly review. It is idempotent (a second run changes nothing), refuses a database that holds other data, and `--force` empties the database first. A seeded RNG and dates relative to today make two runs on one day equal. Names, merchants and e-mail are invented, phone numbers are in the UK drama range, IBANs are all zeros. (2) `FakeClaude` is a `ClaudeClient` subclass with no SDK client: each purpose (capture, classification, meeting link, brief, draft, weekly review, report, school read) is a function of the request built from regular expressions and the lists in the request, so the answer is deterministic and always a valid Pydantic output. Text in a request is only matched against those rules, so an instruction inside it cannot change an answer's shape. Calls are reported like real ones (model "fake", 0 tokens). (3) `ORBIT_CLAUDE_MODE` is `auto` (default, nothing changes), `fake`, `api` or `worker`; it overrides `CLAUDE_BACKEND`. `ORBIT_DEMO=1` makes `auto` pick the fake only when there is no API key and no worker. `seed-demo` prints these settings; it does not edit `.env`.
Alternatives: pick the fake whenever no key is set (breaks the note-draft default and the tests that rely on it); a global test double only in `tests/` (nothing to run a demo with); record real Claude answers and replay them (needs real data).
Consequences: a demo instance needs no key and no network, and CI can boot one. The canned answers are rules, not intelligence: the demo review says so in its text. The `/ops/claude` page shows for the fake backend and lists usage per month and purpose.

## 36. Cost in euros, a monthly budget, tracing, prompt versions and a scoped MCP token (2026-11-24)
Status: accepted
Context: the audit asked for cost control and observability on the Claude path, versioned prompts and a least-privilege MCP token.
Decision: (1) Each `claude_call` stores `cost_micro_eur` (integer millionths of a euro), `latency_ms` and `prompt_version`. Prices are list prices in dollars per model family converted with a fixed `USD_EUR_RATE`, overridable per exact model name with `CLAUDE_PRICE_OVERRIDES` (euros). They are estimates; unknown models are priced like the middle family so a budget errs high; cache tokens are not tracked. The worker and the fake backend cost 0. (2) `CLAUDE_MONTHLY_BUDGET_EUR` (0 = off): a guard before each API request raises `BudgetExceeded` (a `ClaudeError`, so every caller already falls back as without a key) once the calendar month's spend, including calls still buffered, reaches it; the guard fails open if the database cannot be read. A push at 80 and at 100 percent, once per month (the notification dedupe key carries the month), is sent after each usage flush. The worker and the fake are never blocked. (3) OpenTelemetry GenAI spans around each model request and each finished worker job, off unless `ORBIT_OTEL_EXPORTER` is `console` or `otlp` (the latter needs the `otlp` extra); no prompt or answer text goes on a span. (4) Prompt versions live in `orbit/integrations/prompts_registry.py`, with a hash snapshot test (docs/PROMPTS.md). (5) `ORBIT_MCP_TOKEN` opens only `GET /api/v1/context`; the MCP server validates arguments and writes an audit log (docs/MCP.md).
Alternatives: prices from a live exchange rate or the pricing page (a network dependency for a bookkeeping number); a database table for the MCP audit log (the MCP process does not open the database); a hard stop that also blocks the worker (it runs on a subscription, so it costs no per-token money).
Consequences: euro figures can drift from the invoice; check the rate and prices when the price list changes. A request in flight when the limit is crossed still completes. The MCP audit log is a file next to the client, not tamper-proof.

