# Orbit — Specification

## 1. Goal
One place for the people I know, what's between us (money, items, favors, promises), and the dates that matter
(birthdays, Slovak and Czech name days, meetings), plus automation that keeps it current with minimal typing.

**Core principle:** automation and Claude *propose*; I *confirm*. Every automated change goes through the Inbox.

Orbit is growing from this CRM into a personal operating system (`VISION.md`). This spec covers what is built and
what is specified next. New modules get their own section here before any code is written.

---

## 2. Features

### 2.1 People
- A person needs only a display name. Everything else is optional; a person can exist with just an Instagram handle.
- **Handles** (many per person): `phone`, `email`, `instagram`, `whatsapp`, `discord`, `linkedin`, `telegram`,
  `iban`, `bank_name` (the name as it appears on bank statements), `other`.
- **Aliases**: nicknames and diminutives (Patrik / Paťo / Pato) — used by all matching.
- **Tags / circles**: family, Uni, Work, gym, Košice, Prague…
- **Fields**: first name, last name, birthday (month/day, optional year), name-day country (`SK` | `CZ` | none;
  default `SK`), country (ISO 3166-1 alpha-2; the default region for phone numbers without a `+`/`00` prefix),
  city, how we met, `met_through` (link to another person), photo URL, archived flag.
- **Notes**: timestamped free text, optionally linked to a meeting.
- **Interactions**: `(person, date, kind [meet|call|message|other], summary)` — "last talked" is derived from this.
- **Search**: diacritics-insensitive across names, aliases, handles, tags, notes.
- **Import**: a vCard (`.vcf`) upload exported from Apple Contacts, or a read-only sync from iCloud Contacts
  (CardDAV, same app-specific password as the calendar). Both are one-way and idempotent: match by vCard UID, then
  phone/email/social handle, then a unique exact name. Existing people only get empty fields filled; nothing is
  overwritten or deleted. Imported people get name-day country `SK`; `country` comes from the address, else `SK`.
  The review-based two-way sync that replaces this for iCloud is described in §2.13.
- **Duplicates**: handles are unique, so two people can never share a phone/email/IG handle. Adding a handle
  that already belongs to someone else offers a merge instead. The candidate finder is name-based (same or
  similar name, name matches an alias) → merge tool. Pairs can be dismissed as "not a duplicate".

### 2.2 Dates & calendar feed
- Birthdays from person fields.
- Name days: static datasets `data/namedays_sk.json`, `data/namedays_cz.json` (`{"MM-DD": ["Name", ...]}`).
  Match on the person's normalized first name + their `nameday_country`. Some names map to several dates — include all.
  For nicknames ("Majo"), the person page lets me search the calendar and link the official name
  (`nameday_name`, e.g. Marián). The linked name overrides the first name, is stored in the calendar's spelling,
  and must exist in the person's calendar. Contact import never sets it.
- **ICS feed** `/calendar/{ICS_TOKEN}.ics`, subscribed in Apple Calendar:
  - 🎂 birthdays (all-day, yearly), 🎉 name days (all-day), ✅ promise due dates, 📦 lent-item reminders,
    🗓 appointments made by capture (timed, §2.23); planned: 🎓 lessons, 📝 exams and deadlines, 🚋 travel blocks (§2.37), 🏋️ confirmed workouts (§2.38)
  - Stable UIDs so Apple Calendar updates rather than duplicates.
- Reminder pushes: birthday / name day → push at 09:00 on the day, plus 5 days before for people tagged `close`
  ("🎂 Paťo: birthday today" / "Turns 30", with the last interaction). Not sent once it could only arrive the
  next day (the server was down until quiet hours); a push missed at 09:00 goes out later that day.

### 2.3 Ledger — money, items, favors
`Debt` has a **direction** (`they_owe_me` | `i_owe_them`) and a **type**:

| type | required fields | shown as |
|---|---|---|
| `money` | amount_minor, currency | per-currency balance: "Pato owes you 24.00 €" |
| `item` | description | "Pato has: charger (since 3 Oct)" |
| `favor` | description | "You owe Terka: 1 favor (helped me move)" |

- Items and favors never affect money balances.
- Settling: full, or partial for money (partial settles the original, which keeps its amount as history, and creates
  an open remainder debt linked to it, with its own reference code). A settlement can be undone (a mis-tap);
  only open debts can be edited or deleted.
- A person with open debts or promises can't be deleted (archive instead); merging moves their debts and promises.
- `reference_code` (e.g. `D-0142`) auto-generated for money debts; I can put it in payment notes for exact matching.
- Lent items older than `ITEM_REMINDER_DAYS` (default 21, overridable per item) → gentle push reminder, once, from
  09:00 on the day it's due (changing the item's reminder days re-arms it). Home lists them under "Needs attention".
- Views: per-person balance, global "who owes me / whom I owe", open items, open favors.

### 2.4 Inbox (proposals)
Everything automated lands here as a `Proposal` with a `kind`, a JSON payload, a human-readable summary, source info
and a confidence value. Actions: **Confirm**, **Edit then confirm**, **Reject**.

Proposal kinds:
- `split_transaction` — a bank row that looks shared → choose people and split → creates money debts
- `payment_match` — an incoming payment matched to open debt(s) → settle chosen debts
- `capture` — output of dictation: any mix of notes, interactions, debts, promises, appointments (§2.23), new people
- `new_person` — an unknown name mentioned in a capture
- `follow_up`, `promise_due`, `goal_rerank`, `goal_link` — made by the weekly review agent (§2.17); each applies one small change
- `school_deadline`, `course_fact` — made by the school reader (§2.20, Phase 18c): confirming adds a `SchoolEvent(source = proposal)` or a `CourseFact`

Confirming a proposal applies it in a single DB transaction: the payload is validated first, and on any error nothing
is written. "Edit then confirm" replaces the stored payload with the edited one before applying it; in the web UI, edits
are saved even if the confirm then fails (e.g. an amount is still missing), so nothing typed is lost. Rejected
proposals are kept (for dedupe/learning) and leave the Inbox; the ones rejected in the last 30 days are listed under
"Recently rejected", where one can be **reopened** (a mis-tapped "Not a debt"): it goes back to pending exactly as it
was, with `reopened_at` set. A split or payment match can't be reopened while its bank row already has another pending or
confirmed proposal of that kind. A proposal can only be confirmed or rejected while it is pending.

**Kind registry** (`orbit/services/proposal_kinds.py`). Each kind is one `ProposalHandler`: its payload model, an
`apply(session, payload, ApplyContext) -> Applied(message, person_id?)`, and optionally `summary` (rebuilds the summary
after an edit) and `check_reopen` (refuses a reopen). The module that owns a kind defines its handler
(`capture.CAPTURE_HANDLER`, `capture.NEW_PERSON_HANDLER`, `split.HANDLER`, `payments.HANDLER`); `services/inbox.py`
registers each with one `register(...)` line and dispatches by kind. A test checks that every `ProposalKind` has a
handler. The Inbox web cards (`web/inbox.py`) are still per kind; they are UI, not dispatch.

Push action buttons (`http` actions) POST to `/a/{token}`: a token signed with `ORBIT_SECRET_KEY` carrying
`{proposal_id, action (confirm|reject), exp}` (7 days). It is single-use because an action only applies to a
pending proposal; once the proposal is resolved, all of its tokens answer "Already confirmed/rejected" (409). Tokens made
before a reopen stay spent (409 "Reopened since"), so an old push can't act on a reopened proposal.
If the action can't be applied (e.g. an amount is missing), it answers 422 and the proposal stays pending.

### 2.5 Quick capture (dictation → Claude → proposals)
- `POST /api/v1/capture {text, meeting_id?}` — from an iOS Shortcut (dictate text) or the web UI.
- Claude gets: the text, a compact list of candidate people (names + aliases only, resolved by fuzzy pre-match), today's
  date, the meeting context if any. Returns structured output:
  ```
  { notes:[{person_ref, text}], interactions:[{person_refs, kind, summary, occurred_on?}],
    debts:[{person_ref, direction, type, amount?, currency?, description}],
    promises:[{person_ref, direction, text, due?, goal_ref?}],
    appointments:[{person_ref?, title, start, end?, location?}],   (§2.23)
    unknown_people:[{name, context}], confidence }
  ```
- `person_ref` must be an existing person ID (`"12"`) from the candidate list or an index into `unknown_people`
  (`"new:0"`); anything else is left unassigned and the edit form asks me to pick a person. Amounts come back as
  decimal strings in major units and are converted to minor units by Orbit. If the amount is ambiguous
  ("paid for my coffee"), the debt is proposed with `amount: null` and the UI asks me for it. The interaction date
  date is `occurred_on` (null = the day it's confirmed). `confidence` (0–1) is stored on the proposal; below 0.6
  the card asks me to double-check.
- Goals: the request also lists the active goals (`id`, rank, title, why; at most 50). `goal_ref` on a promise names one of
  them when it clearly serves it. An id that was not listed is dropped. The promise draft keeps it as `goal`; the Inbox card shows a
  "Serves" picker, and confirming sets `promise.goal_id` (a goal deleted since makes the confirm fail, nothing written). See §2.16.
- Fuzzy pre-match: every word of the text against the normalized names and aliases of non-archived people, tolerant of
  Slovak/Czech case endings ("Paťovi", "s Paťom", "Terke", "Petrovi", "Markom") and close spellings, plus `@handles`;
  at most 25 candidates, best first. Only candidates' names and aliases are sent; IBANs and phone numbers in the text
  are masked.
- Unknown people that an item refers to stay inside the `capture` proposal; on confirm each is created as a new person
  (default), linked to an existing person, or skipped together with their items. Unknown people no item refers to
  become separate `new_person` proposals (confirming creates the person, with the context as a note).
- If Claude isn't configured, fails, or finds nothing, the dictated text is kept as a note draft in a `capture`
  proposal (assigned when exactly one person matched), so nothing dictated is lost.
- Result → one `capture` proposal (+ `new_person` proposals). Response to the Shortcut:
  `{summary, proposal_ids, items}`, where `summary` is short text ("2 items waiting in Inbox: …").
  `meeting_id` (the Shortcut may send `""`; an unknown id is ignored) ties the capture to a meeting, see 2.9.

### 2.6 Bank CSV import & split flagging
- `POST /api/v1/imports/bank` (multipart: file + `bank` = `revolut` | `tatra` | `auto`: detected by header), or the
  web Import page.
- Parsers in `integrations/bank_parsers/`, one per bank, all producing a normalized `Transaction`.
  **Built from real anonymized sample files** (`tests/fixtures/bank/`) — formats must not be guessed.
  - Revolut: "Account statement" CSV, UTF-8; only `COMPLETED` rows are imported (pending ones may still change).
    Booking date = completed date; `occurred_at` = started time.
  - Tatra banka: internet-banking CSV, Windows-1250 (UTF-8 copies also work). Booking date = "Dátum zúčtovania".
    Card payments carry card, place, local time, [original amount,] merchant in "Informácia pre príjemcu";
    transfers have no counterparty name, only the IBAN and a message.
  - Times in the files are read as local time (`TIMEZONE`) and stored in UTC.
- Dedupe: `hash = sha256(bank, booking_date, amount_minor, currency, direction, counterparty_norm, bank_reference,
  counterparty_iban)`. Amounts are unsigned, so the direction is part of the hash. Rows identical in all of these
  within one file (two same-day coffees) get an occurrence number appended, so both are kept and re-importing still
  matches them.
- Classification (rules first, Claude for the rest, batch of new rows per import):
  `personal` | `likely_shared` | `incoming_from_person` | `ignore`.
  - Rule hints for `likely_shared`: MCC/description keywords — restaurants, bars, groceries and taxi/Bolt/Uber
    over `SHARED_MIN_AMOUNT`; tickets and Airbnb regardless of amount.
  - `SHARED_MIN_AMOUNT` is set once, in `DEFAULT_CURRENCY`. For transactions in another currency it is converted
    with ECB daily reference rates (cached daily). The conversion is used only for this check and never touches
    stored amounts or balances. If no rate is available, the amount rule is skipped and Claude classifies the row.
  - Claude sees merchant/description, amount, date, and whether a calendar meeting overlapped (with whom).
  - Rules also settle the obvious rows: top-ups, exchanges, ATM, fees → `ignore`; incoming transfers →
    `incoming_from_person`; card refunds → `ignore`; outgoing transfers → `personal`. Claude gets merchant /
    description, amount, date and time, direction and kind — never IBANs, account or card numbers — in batches
    of 100; rows it can't classify (or with Claude unconfigured) stay unclassified.
- `likely_shared` (outgoing only) → `split_transaction` proposal: "Lokál, 48.00 € — split?". One push per import:
  the proposal itself when one payment was flagged, else "3 payments to split?" listing a few (action: open in web UI).
- The split screen: pick people (search, recent people). While nobody is picked yet, the people of linked meetings
  going on within an hour of the payment time are pre-selected (shown, not saved until I save or confirm);
  **even** split between them and me (my share is never a debt and I keep any leftover cent), or between them only
  ("I paid only for them"), or **custom** amounts (together at most the payment). Confirm creates one
  `they_owe_me` money debt per person in the payment's currency, linked via `source_transaction_id`.
  Any outgoing payment can be split by hand from the Import page, flagged or not.
- Import summary returned: new / duplicates / flagged / payments received / unclassified counts.
- **Transactions** page (`/transactions`, linked from Import): every bank row across all imports, newest first, with the
  same filters as an import (all / shared? / received / unclassified) and the same Split / Match buttons. Search is
  diacritics-insensitive; every word must match the counterparty or the payment message, or the query is an amount
  ("39,10").

### 2.7 Incoming payment → notification → manual settle
- For each new incoming transaction classified `incoming_from_person`:
  1. Identify the payer: IBAN handle → `bank_name` handle (the statement's name, normalized) → reference codes in the
     payment whose open debts all belong to one person → fuzzy name match against non-archived people. The fuzzy
     match needs first + last name (in any order, aliases count as first names, close spellings allowed) or initial +
     last name ("P. VESELY"), and exactly one best person; otherwise the payer is unknown. Nothing is guessed from a
     surname alone.
  2. Find candidate debts (`they_owe_me`, money, same currency, open), oldest first: the debts named by
     `reference_code` in the message / name / reference ("D-0142", "d0142", "D 0142"; a code partly paid since points
     to its open remainder) → one debt of the exact amount → subset-sum of 2–4 open debts (the oldest 20 are tried)
     → otherwise all of them in order: `partial` (amount < open total: whole debts while it lasts, the next in part),
     `all`, or `overpaid` (at most double what's owed; a larger payment, e.g. rent, gets no option). At most 5 options.
  3. Create a `payment_match` proposal with the ranked options; the top one is pre-selected. A known payer with no
     open money debts at all gets no proposal (nothing to settle). Any incoming payment can be matched by hand from
     the Import page ("Match"), whoever sent it.
- Push (ntfy) per payment from a known payer, with the payment info and buttons:
  "💶 Received 24.00 € from Patrik Veselý" / "“pizza + kino” · Matches: pizza 12.00 € + kino 12.00 €"
  **[Settle] [Choose…] [Not a debt]** — `Settle` applies the selected (top) option; `Choose…` opens the proposal in the
  web UI; `Not a debt` rejects it. Without a match (e.g. a currency mismatch) there's no Settle button. When the payer
  was found by name or reference code, the body also shows how the bank named them, so I can check the match.
- **Never auto-settles.** Unanswered matches stay in the Inbox. Confirming re-checks every debt (still open, still
  theirs); if anything changed, nothing is applied and the push button answers "Open the Inbox" (422).
- Settling: each chosen debt is settled (fully, or in part → remainder, see 2.3) with `settled_by_transaction_id` set to
  the payment; undoing the settlement clears it. Debts in another currency can be settled only in full. In "Choose…"
  I can pick a ranked option or tick debts myself (with part amounts in the payment's currency).
- Unidentified incoming payments also create a `payment_match` proposal without a person ("Who sent 24.00 €?
  “J. NOVAK”"). One push per payment when an import has a single one, else one push listing a few ("3 payments from
  unknown senders"). Picking the person in "Choose…" shows their options before anything is settled. With
  "Recognize … next time" (default on), confirming saves the payment's IBAN and bank name as that person's `iban` /
  `bank_name` handles (only those nobody has yet), so next time it matches, and re-matches other pending payments
  from unknown senders. Confirming with nothing to settle just links the person.

### 2.8 Calendar sync & pre-meeting brief
- Poll iCloud Calendar via CalDAV every `CALDAV_POLL_MINUTES` (default 10; also "Sync now" on the Meetings page and
  `POST /api/v1/meetings/sync`), window: 1 day back → +14 days, only the calendars named in `CALDAV_CALENDARS`
  (case- and diacritics-insensitive). Recurring events are expanded: each occurrence is its own meeting.
  All-day events are skipped (not meetings).
- Upsert `Meeting` by event UID (`<UID>#<original start, UTC>` for an occurrence of a recurring event). A move updates
  it; a meeting moved to a later time gets a new brief and capture prompt. `STATUS:CANCELLED`, or vanishing from a
  calendar that was read in full, marks it `cancelled` (never deleted); it comes back if it reappears.
- Link meeting → people: attendee/organizer emails → email handles; then title/location words vs names + aliases
  (diacritics-insensitive, tolerant of Slovak/Czech case endings and close spellings, e.g. "kafe s Paťom"). A word
  only one person matches best links them; a word several people match is ambiguous unless another word settles it
  ("Paťo Veselý"). Claude is asked only about the ambiguous ones (names + aliases of at most 10 candidates); no clear
  answer → unlinked. Unlinked meetings are ignored, never guessed. Archived people aren't linked.
- Automatic links are redone only when the title, location or attendees change, so a manual link or unlink sticks
  (and Claude isn't asked again every poll). I can link/unlink manually in the UI or API; manual links always stay.
- `BRIEF_MINUTES_BEFORE` (default 30) → push per linked meeting (dedupe per meeting and start time), sent even during
  quiet hours:
  - last interaction (date + summary), the latest note (the page shows the last 3)
  - open money balance, items and favors, both directions
  - open promises, both directions
  - birthday / name day within 14 days of the meeting
  - optional one-line Claude summary ("what's new with them") from the last notes, written when the brief goes out —
    toggle `BRIEF_USE_CLAUDE`; skipped without notes or when Claude fails
- Brief is also viewable at `/meetings/{id}`, which also offers a dictation box (the same as 2.9).

### 2.9 Post-meeting capture
- `CAPTURE_MINUTES_AFTER` (default 15) after a linked meeting ends → push: "Anything to note about Pato?"
  with action **[Dictate]** → opens the iOS Shortcut `Orbit Capture` with the meeting ID as input
  (`shortcuts://run-shortcut?name=Orbit%20Capture&input=text&text=<meeting_id>`), plus **[Open]** (the meeting page).
  A prompt more than 3 hours late (the server was down) isn't sent.
- The Shortcut dictates → `POST /api/v1/capture {text, meeting_id}` → `capture` proposal (see 2.5), plus an auto
  `interaction(kind=meet)` with everyone linked to the meeting inside the same proposal. The meeting's people are
  Claude candidates even when the text doesn't name them, and Claude is told about the meeting. A `meet` interaction
  Claude found for that day becomes the meeting's interaction (people merged) instead of a second one. Without
  Claude, the note draft goes to the person met when there was exactly one.
- On confirm, the interaction, notes and promises of the capture get `meeting_id`.
- If ignored, nothing happens: no nagging, and no interaction is logged. The post-meeting capture is the only
  source of a meeting's interaction, so each meeting yields at most one (a later capture for the same meeting adds
  notes etc. but no second interaction).
- This push is sent even during quiet hours (like pre-meeting briefs), while the meeting is fresh.

### 2.10 Promises tracker
- `Promise(person, direction [i_promised|they_promised], text, due, status [open|done|dropped], meeting?)`.
- Created from captures (via Inbox) or manually. Due default: +7 days if unspecified.
- Shown in the pre-meeting brief, on the person page, and in the ICS feed on the due date.
- Overdue `i_promised` → push once on the due date morning (09:00; later if it's already overdue when created or
  the server was down). Changing the due date re-arms it.

### 2.11 Notifications & digest
- Transport: self-hosted or ntfy.sh topic (private, access token). Actions: `view` (URL) and `http` (POST to API with
  bearer token).
- Every sent notification is recorded (`Notification` table) with a dedupe key → never sent twice. The row is written
  before sending and holds the full push, so a queued one can go out later.
- Quiet hours `QUIET_HOURS` (default 22:00–08:00): queued until the end, except pre-meeting briefs and
  post-meeting capture prompts (kinds `meeting_brief`, `meeting_capture`).
- A failed send is retried every 5 minutes, at most 5 attempts; a push that couldn't be delivered within 24 hours is
  dropped as stale. The in-process scheduler (`SCHEDULER_ENABLED`, on in exactly one app process) sends due pushes
  every minute. `uv run python -m orbit.cli test-push` sends a one-off test push to check the ntfy setup.
- **Sunday 18:00 digest**: pending Inbox items (and how many are new this week), open balances per currency,
  overdue promises (both directions), promises due in the coming week, lent items, next week's (Mon–Sun)
  birthdays/name days and meetings; at most 5 entries per section, the rest counted. Sent even when everything is
  empty ("A quiet week"). Held by quiet hours like any other push. One last line, "🎯 Review your goals: last reviewed N
  days ago", when goals exist and none was reviewed for 7 days (§2.16).
- All the times and counts on this page (quiet hours, retry rules, reminder and digest times) are editable on `/settings/schedule` (§2.21).
- Reminders (`birthday`, `nameday`, `lent_item`, `promise_due`, `digest`) are checked every minute by the scheduler;
  the dedupe keys (`birthday:<person>:<date>:day|ahead`, `lent_item:<debt>:<remind date>`,
  `promise_due:<promise>:<due>`, `digest:<Sunday>`) make each go out once.

### 2.12 Web UI (mobile-first, HTMX)
Pages: Home (search, Inbox count, "Needs attention" capped at 5 rows with a link to `/attention` (§2.25): due or overdue promises and lent items to ask back, today's
meetings, money totals, the next 7 days' birthdays and name days, the top 3 goals), People (search, tags), Person detail (fields,
handles, timeline of notes/interactions, ledger, promises), Inbox, Ledger, Goals (ranked list, detail, weekly review), Context (debug view, §2.15), Import, Transactions, Meetings,
Settings.
Must be comfortable on an iPhone screen; add to Home Screen as a PWA (manifest + icon; no offline mode needed).
The icon is drawn by `scripts/build_icons.py` (standard library only) into `orbit/web/static/icons/`.
Templates link static files with `static_url('css/app.css')`, which adds `?v=<content hash>`; `/static` answers a
request with `v` with `Cache-Control: public, max-age=31536000, immutable`, so a file is fetched once per change.

### 2.13 Contact sync (iCloud CardDAV, two-way, review first)
Pressing **Sync** on the Contacts page (or `POST /api/v1/contacts/sync`) never changes a person or an Apple card by itself. It fetches
all cards, diffs them against Orbit and shows a review screen; only **Apply** writes. There is no scheduled sync.

**Three-way diff.** For every linked person and every synced field Orbit keeps `base`, the value both sides had at the
last sync. With `orbit` = current DB value and `apple` = current card value:
- `apple != base`, `orbit == base` → Apple changed → **pull** into Orbit.
- `orbit != base`, `apple == base` → Orbit changed → **push** to Apple.
- both changed and differ → **conflict**: both values are shown and I pick one (Apple's or Orbit's, or skip). A `.vcf` source has no
  card to write to, so there only Apple's value or skip is offered.
- both changed to the same value → nothing to do, `base` is updated silently.
- a person without a link: matched as in §2.1 (UID, handle, unique exact name); a card with no match is a
  `new_contact` change; a fuzzy name match is offered as `possible_match` (link or create new).
- a card gone upstream → `removed_upstream`; the only action offered is **archive**. Nothing is ever deleted.

**Synced fields:** name, birthday (month/day, optional year; both vCard `BDAY` forms, incl. iCloud's year-less
`--MM-DD`), phones, emails, and the Instagram/other social handles Orbit already imports. Phones and emails are
sets: each value is added or removed on its own. Orbit-only data (notes, debts, promises, tags, aliases, name days,
interactions) is never pushed.

**Auto-fill (the one exception to "I confirm").** A field that is empty on the target side is filled without a
proposal: Apple → Orbit in Phase A, and Orbit → Apple from Phase B. These changes are pre-checked in the review, so
nothing is written that I did not see, and each is logged in the sync run with Undo. Everything else is unchecked and
needs a tick: overwrites, removals, new contacts, merges. A **name** is never auto-applied in either direction; an
Orbit name edit appears in the Apple section as an unchecked change.

**Review screen** has two sections: *Into Orbit* and *Into Apple contacts*. Each change is one row `person · field ·
old → new` with a checkbox; groups have "select all". The Apple section shows, per contact, the exact field diff that
will be written. Apply runs the checked Orbit changes in one DB transaction, then the Apple writes contact by contact.

**Write-back safety (Phase B).**
- The card is parsed and only the target properties are changed; unknown properties (`X-*`, photo, groups,
  other-app fields) are kept as they are. The card is never regenerated from Orbit data.
- `PUT` carries `If-Match: <etag>`. If the card changed since the preview, that contact is skipped and reported as failed; the next Sync diffs it again.
- The vCard text before each write is stored in `contact_backup`; a write can be reverted from the sync run page.
  Backups are pruned after 90 days.
- Only changed contacts are written. A failed contact does not stop the others; the run page lists failures.

**Dedupe and noise.**
- Syncing twice with no changes creates no new changes.
- A change I reject is remembered by `(person, field, direction, value)` hash and not proposed again until the value
  changes.
- The first sync can be hundreds of `new_contact` rows: the review has search, a filter (has birthday / phone / email)
  and bulk select.
- A `.vcf` upload runs the same pipeline with the file as the source. It has an Orbit section only (no server, so
  nothing to write back to).

### 2.14 Context providers (agents read modules through these)
Each module that agents may read has a provider in `orbit/services/context/`: `build(session, window, focus) ->` a
Pydantic `ModuleContext` subclass. `PROVIDERS` lists them; Phase 14 adds `build_context` that combines them.
Nothing is written, and nothing here is sent to Claude yet (the existing prompts are unchanged).
- `ContextWindow(start, end, tz)`: whole local days, both included; `start` is "today" (open items due before it are
  `overdue`). `ContextFocus(person_id?)`: narrows a provider to one person; goals come with Phase 15.
- **Bounded, generously** (`DECISIONS.md` #7): every list has a cap and `truncated` says when it was hit. The caps only stop
  a runaway query: people 300 (50 notes and 20 interactions each), open debts 500, balances 100, settled debts 100,
  meetings 300, open promises 500, closed promises 100. Totals still cover everything.
- **Masked:** every free-text value goes through `context.base.text()`: one line, IBANs and phone numbers masked, then
  clipped to 2,000 characters (names 200). Account and contact identifiers are not in the models: people carry
  `handle_types` (channels), never handle values; no IBANs, phone numbers or emails. They have no decision value.
- `people`: without focus, non-archived people with a birthday or name day in the window, a keep-in-touch due by its
  end, an interaction or a meeting in it, or an open debt or promise; with focus, that person. Per person: aliases, tags,
  city, country, how we met, birthday, handle types, last contact, up to 20 interactions, up to 50 notes (newest first, each
  with the day it was written),
  dates in the window, keep-in-touch due date.
- `ledger`: per-currency totals, balances (largest first), open debts (due soonest first) and debts settled in the last 90
  days (newest first); money amounts in minor units, never converted. Focus: that person's.
- `meetings`: not-cancelled meetings starting in the window (local times) with their linked people. Focus: that
  person's meetings.
- `promises`: open promises, soonest due first, with `overdue` / `due_in_window`, and promises done or dropped in the last
  30 days. Focus: that person's.

### 2.15 Shared context layer (Phase 14)
`orbit/services/context/snapshot.py` has `build_context(session, window, focus, budget_chars=...) -> ContextSnapshot`
(re-exported by `orbit.services.context`): it runs every provider of §2.14 and returns one snapshot. Agents read the snapshot, never the providers or the tables.
It is read-only, it needs no new table, and nothing here is sent to Claude yet. Phase 16 is the first agent that does.
`PROVIDERS` in `snapshot.py` is the one list of providers; a new module adds its provider there and its field to the snapshot.

**Snapshot** (frozen Pydantic model, JSON-serializable):
- `generated_at` (UTC), `window` (`start`, `end`, `tz`), `focus` (`person_id`, `person_name`, or none)
- `people`, `ledger`, `meetings`, `promises`: the provider models of §2.14, one field per module. Phase 15 adds `goals`
  and every later module adds its own field, listed first in the order agents should weigh them.
- `budget`: `limit_chars`, `used_chars`, `trimmed` (per module: items dropped by the budget). `used_chars` is the JSON of the
  modules without the `budget` report itself; it is above the limit only when nothing was left to cut.

**Windows.** The caller passes a `ContextWindow`. Presets (`windows.py`, `today` in `Europe/Prague`): `this_week` =
Monday to Sunday of the current week (the default); `next_7_days` = today to today + 6; custom = any two dates, at
most 92 days.

**Focus kinds.** `none` (the whole window) and `person` (everything that touches one person). `goal` arrives with
Phase 15. A focus that points at a missing entity gives an empty snapshot with the focus shown as not found, never an error.

**Size budget.** The snapshot's JSON must fit `limit_chars` (default 400,000, about 100,000 tokens; one setting,
`CONTEXT_BUDGET_CHARS`, 1,000 to 4,000,000). Over the limit, the layer removes items from the end of lists, one at a time, always from the
module whose JSON is currently largest (ties: module order). Each provider already orders its lists most important
first, so the tail is the least important part. A module that lost items gets `truncated = true`, and `budget.trimmed`
says how many. Totals (ledger) are never trimmed. A snapshot is never cut in the middle of an item. When nothing is left to cut and it is still over, it is returned as it is
and `used_chars` shows it.

**Masking in one place.** Providers mask their own text (§2.14). The layer then runs every string in the snapshot through
`mask_sensitive` once more before the size check (dates and times pass through unchanged). This is a safety net: a provider that forgets still cannot leak an IBAN
or phone number. The pass is idempotent, and a test checks it.

**Debug view.** `GET /context` (web login; "Context" link in the top bar) shows exactly what an agent would receive. Controls: window (preset or two
dates) and focus (none or a person picker). It shows the size against the budget with what was trimmed, one collapsible
section per module, and the raw JSON in a block to copy. It is read-only and stores nothing. The JSON is built the same
way an agent's input is, so the page cannot differ from what is sent. `GET /api/v1/context?preset=|start=&end=&person_id=`
returns the same snapshot for scripts and tests.

**Decided** (2026-10-05, the defaults above): the debug view opens on `this_week`; the budget was first 12,000 characters, and `DECISIONS.md` #7 raised it to 400,000; snapshots are not stored (the debug view rebuilds them); providers clip free text to 2,000
characters; names go as they are, with no per-person exclusion rule yet.

### 2.16 Goals & priorities (Phase 15)
Goals are the spine (`VISION.md` principle 3): an agent can only weigh tradeoffs against explicit, ranked priorities. A
goal is something I want, in my words, with an order. Orbit never invents or changes a goal on its own: goals are written
and ranked only by me (autonomy L0 for the goal itself). Capture may *propose* attaching a new promise to a goal (L2, below).

**Goal.** `title` (required, "Run a half marathon"), `why` (optional, my reason in a sentence or two; what an agent reads to
judge a tradeoff), `horizon` (`week | month | quarter | year | ongoing`), `target` (optional free text for what "done" looks
like: "under 2:00", "5,000 € saved"), `target_date` (optional), `status` (`active | paused | done | dropped`), `rank`.
- **Rank.** Only active goals have a rank: 1 = most important, contiguous, no ties. Pausing, finishing or dropping a goal
  removes it from the order and closes the gap; making it active again puts it last. Reordering is by whole-list order
  (`POST .../rank` with the active ids), so two goals can never share a rank.
- **No measuring here.** `target` is text. Progress numbers come from the module that measures them (fitness, finance, habits),
  from a passive source, when that module exists (`VISION.md` principle 2). A manual progress log would be another
  tracker that stops after two weeks.
- **Review date.** `reviewed_at` is set when I confirm a goal in the weekly flow or move it. Nothing else uses it yet except
  the nudge below and the context's `days_since_review`.

**Links** (real foreign keys, never names in text):
- people: `goal_person(goal_id, person_id)`, many to many ("get closer to Peter"; "help Terka move").
- promises: `promise.goal_id` (nullable), a promise serves at most one goal. Deleting a goal clears it on its promises.
- meetings, debts and later domains get their own foreign key when a real consumer needs it (a meeting brief that names
  the goals of its people reads them through the people link first). Merging two people moves their goal links
  (`services/duplicates.py`, with undo like everything else it moves).

**Weekly re-rank flow** (`/goals/review`, manual in this phase; the Phase 16 agent will propose a re-ranking into the Inbox).
It walks the active goals in rank order. Each card shows the goal, why, target, target date, open linked promises, linked
people, and days since the last review, with **Still right**, **Move up**, **Move down**, **Pause**, **Done**. It ends on the
final order and sets `reviewed_at` on every goal I touched. Nothing is sent anywhere.

**Capture links** (changes to `SPEC §2.5`). The capture request gets the active goals as `{id, rank, title, why}` (masked,
at most 50). Claude may set `goal_ref` on a promise draft to one of those ids, or leave it empty; an id that is not in the list
is dropped. The Inbox card shows "serves: *goal*" with a picker (none / any active goal), and confirming sets
`promise.goal_id`. This is part of the `capture` proposal, not a new proposal kind: the promise does not exist until
confirm, so there is nothing to link before then. Goal titles and `why` go to Claude by decision (`DECISIONS.md` #7).

**Context provider** (`goals`, `§2.14`). Active goals in rank order, at most 50: `id`, `rank`, `title`, `why` (clipped to 2,000
characters), `horizon`, `target`, `target_date` with `overdue` / `due_in_window` against the window, `days_since_review`, the
linked people (id and name, at most 20), and the open linked promises (id, text, due, at most 50). Focus on a person: only goals
linked to that person. In the snapshot (`§2.15`) `goals` is the first field. The budget layer never trims goals: if the
snapshot is still too large, the other modules lose items first (goals are the spine).

**Web UI.**
- `/goals`: active goals ranked, each a row with rank, title, horizon, target date, counts of open promises and people,
  Move up / Move down / To top (plain forms, no JS); below it a collapsed list of paused, done and dropped goals. A form
  at the top adds a goal (title only works).
- `/goals/{id}`: edit all fields; linked people (add with the usual person picker, remove); linked promises (open ones, an
  "attach a promise" picker over my open promises, detach); status buttons; delete (asks first).
- Promise forms (person page, ledger) get an optional "serves" goal picker. A person page lists the goals it is linked to.
- Home: a short strip with the top 3 goals, and "Review your goals" when no goal has been reviewed for 7 days.
- Nav: see open question 6.

**Rules.** Titles are unique among active goals (diacritics-insensitive, like tags) to stop accidental doubles. Rank, status
and links change only through the service, which keeps the rank invariants and is tested for every transition. Everything is
in the full JSON export.

**Decided** (2026-10-05): `target` is free text (no number to track until a module measures it); `horizon` is optional, with the
set `week | month | quarter | year | ongoing`; `why` goes to agents in full and capture sees the active goals (`DECISIONS.md`
#7); capture links a goal through the promise draft, and the `goal_link` proposal kind waits for the Phase 16 review agent that
first produces it (`DECISIONS.md` #8); Goals is a top-bar link plus a Home strip, not a tab; the Sunday digest gets one line
("🎯 Review your goals: last reviewed N days ago") only when no active goal was reviewed for 7 days, with no separate push.

**Built as** `orbit/domain/goals.py`, `services/goals.py`, `services/context/goals.py`, `api/goals.py`, `web/goals.py`
(templates `goals/`), migration `0014`. Field limits: title 200 characters, `why` 5,000, `target` 1,000. `/goals/review` is
one page with a card per active goal, each tap saved at once ("Still right" sets `reviewed_at`; Move up / Move down move and
set it; Pause and Done change the status). The snapshot's `goals` module shows up to 50 goals, each with up to 20 people and 50
open promises. Moving a goal marks every goal whose rank changed as reviewed, since the order was looked at.

### 2.17 Weekly review agent (Phase 16)
The first proof point (`VISION.md §7`): an agent reads goals, people, money, meetings and promises together and tells me what I
had not noticed: what went well, what slipped, what conflicts with what, and what next week's priorities should be. It is an
agent in the sense of `VISION.md §4`: it builds context through `build_context` (§2.15), calls Claude through
`integrations/claude.py`, validates the structured answer, and writes **a read-only review (L1)** plus **proposals in the Inbox
(L2)**. It never writes to a domain table. Raising any of that needs its own `DECISIONS.md` entry.

**Which weeks.** The *reviewed week* is the Monday-to-Sunday week that contains the run day; the *planned week* is the next one.
On Sunday evening that is "the week I just lived" and "the week ahead". A review is identified by its planned week's Monday
(`plan_start`), and there is at most one per planned week.

**Schedule.** A job checks every minute (like the reminders). From **Sunday 19:00** (`Europe/Prague`; after the 18:00 digest) a
planned week with no review gets one. If the server was down, it is still generated until the following Wednesday 23:59; after that
that week is skipped. `WEEKLY_REVIEW_ENABLED=true` turns the job off. It needs Claude configured: without it nothing runs and
`/reviews` says so. Quiet hours hold the push, not the review. A manual **Generate now** button on `/reviews` makes the review for
the current week on any day (a midweek one covers "so far").

**What Claude is given** (all of it masked as in §2.14; sent in full by `DECISIONS.md` #7):
- `last_week` and `next_week`: two context snapshots (§2.15) for the reviewed and the planned week, each at `CONTEXT_BUDGET_CHARS`.
  Goals come first in both.
- the previous review's summary and priorities (so it can say what happened to them), if there is one;
- the summaries of pending Inbox proposals and of proposals I rejected in the last 30 days (so it does not repeat them);
- today's date, the weekday, and the allowed proposal kinds with their rules.
Change to the people provider: a note carries the day it was written (`{on, text}`), so "notes added this week" can be seen.
Not available to it yet: bank rows, health, school (no module yet); money is only what the ledger holds.

**Output** (`ReviewOutput`, validated with Pydantic; English, quoting Slovak or Czech content as it is):
- `summary`: three or four sentences, the honest headline of the week.
- `wins`, `slips`: lists of `{text, goal_ids[], person_ids[]}`.
- `conflicts`: `{title, detail, suggestion, goal_ids[], person_ids[], meeting_ids[], promise_ids[]}`: two things in the next week
  that cannot both happen, or something that works against a higher-ranked goal (exam Thursday, five gym sessions, budget tight,
  Peter's birthday Saturday). This is the part the proof point is judged on.
- `priorities`: up to 7 `{text, why, goal_id?}` in order for the planned week.
- `proposals`: below. Every id in the answer must exist in the snapshots Claude was shown; an unknown id is dropped, and an item
  whose only reference is unknown is dropped with it. Anything beyond the proposal cap (15) is dropped, lowest first.

**Proposal kinds it may create** (all L2: `Proposal`, source `weekly_review`, source id = the review; each has a payload model,
an `apply`, and registers through the kind registry, §2.4). None of them contacts anyone or moves money.
1. `follow_up`: `{person_id, text, due?, goal_id?}`. Confirming creates an open promise "I promised" with that text (due
   default: 7 days). It is how a to-do about a person enters Orbit.
2. `promise_due`: `{promise_id, due, reason}`. Confirming changes that open promise's due date; refused if it is no longer open.
3. `goal_rerank`: `{goal_ids (every active goal once), reason}`. Confirming applies the order; refused with "Goals changed since"
   when the set of active goals is no longer the one it was made for.
4. `goal_link`: `{goal_id, promise_id | person_id}`. Confirming attaches that open promise (§2.16: a promise serves one goal) or
   links that person. This is the kind `DECISIONS.md` #8 deferred to this phase.
A proposal that repeats a pending one (same kind and same target) is not created again.

**The review page** (`/reviews`, `/reviews/{id}`; read-only except Generate now): the summary, wins, slips, conflicts (with links
to the goals, people and meetings involved), priorities, and the proposals it made with their status and a link to each Inbox card.
A footer shows the model, tokens in and out, and "What Claude saw" (the stored input as JSON), because `DECISIONS.md` #6 said to store
the input when this phase needs "why did it say that". A failed review shows the error and a Retry button.
`/reviews` lists past reviews; Home links to the latest one.

**Push.** When a review is ready: title "🗓 Weekly review ready", body = the summary plus "N proposals in your Inbox", click opens
`/reviews/{id}`, one **Open** button. Dedupe key `weekly_review:<plan_start>`, so it goes out once. The Sunday digest stays as it is.

**Failure and idempotency.** Generating takes a database lock on the planned week's row (insert-first, unique `plan_start`), so two
runs cannot both write one. A Claude error or invalid answer after the client's own retries marks the row `failed` with the error;
the job tries again on its next tick up to 3 attempts, then stops (the page shows Retry). A ready review is final for its week: a rerun
of the same week does nothing in this phase, and never duplicates proposals or the push.

**Claude call.** `integrations/claude.py` gets `weekly_review(request) -> ReviewOutput` behind a small `WeeklyReviewer` protocol so tests
use a fake. Purpose `weekly_review` in the usage log. It needs a larger output limit (up to 16,000 tokens, as today) and a longer
timeout than the 30 seconds the other calls use (300 seconds for this one).

**Decided** (2026-10-05, the defaults above): Sunday 19:00 with catch-up until Wednesday; the reviewed week is the one you are in and
the planned week the next; the input snapshots are stored with each review (`DECISIONS.md` #9, which revisits #6); no rerun of a ready
week, only Retry after a failure; the four proposal kinds; at most 15 proposals; a separate push with the digest unchanged; English
output; a "Review" link in the top bar; the review reads `CONTEXT_BUDGET_CHARS` per snapshot, so a heavy week can be about 200,000
input tokens once a week.

**Built as** `orbit/services/review.py` (weeks, `claim`, `generate`, `sanitize`, proposals, push, `run_now`), `domain/review.py`,
`integrations/claude.py` (`ReviewOutput`, `ReviewRequest`, `WeeklyReviewer`, `ClaudeClient.weekly_review`), proposal handlers in
`services/promises.py` (`follow_up`, `promise_due`) and `services/goals.py` (`goal_rerank`, `goal_link`), the job `weekly_review` in
`jobs/scheduler.py`, `api/reviews.py`, `web/reviews.py`, migration `0015`. Details that the text above leaves out:
- `status` is `pending | ready | failed`. `pending` is the claimed row while Claude is being asked; one older than 15 minutes (a stopped
  server) can be taken again. Up to 3 attempts per week; Generate now or the API retries a failed week from zero.
- Every id in the answer is checked against the ids in the two snapshots. A person is only "known" when the snapshot lists them (a
  meeting, an open debt or promise, a birthday, a keep-in-touch, a goal link): a follow-up for someone who is not in it is dropped.
  A `goal_rerank` is dropped unless it holds every active goal once and changes the order. Proposals are created in the order
  rerank, new due dates, follow-ups, goal links, and the cap cuts from the end.
- A proposal is skipped when a pending one asks for the same thing (same promise, same person and text, same link, any rerank).
- Each proposal card is the generic Inbox card: the summary (with Claude's reason) and Confirm / Reject. A confirm that no longer
  fits (promise closed, goal paused, goals changed) fails with a message and writes nothing.
- The review row keeps `input_json`, `input_chars`, the model and the tokens (the "What Claude saw" footer). The review's Claude call
  uses a 300-second timeout.
- API: `GET /api/v1/reviews`, `GET /api/v1/reviews/{id}`, `GET /api/v1/reviews/{id}/input`, `POST /api/v1/reviews/run` (422 when Claude
  is off or the week's review is ready or being made).
- Nothing is sent when Claude is not configured or `WEEKLY_REVIEW_ENABLED=false`.

**Done when** (yours to judge): after two real weeks the review has surfaced at least one conflict or priority I had not noticed.

### 2.18 Subscription-driven review (Phase 16b)
The weekly review (§2.17) can run on `ANTHROPIC_API_KEY` (pay per token), but production does not use the API: the owner uses their Claude subscription, through Claude Code
running on the server (a headless `claude -p` run). Orbit does not borrow a login and does not run Claude Code inside its container. Instead the
review is split into two HTTP calls that any external Claude session can make, and a small script on the VM makes them on a timer.
Everything that makes a review safe stays in Orbit and is shared with §2.17: the claim, the id checks, the caps, the stored input, the proposals,
the push. Claude still only proposes (L2); this adds no autonomy.

**Endpoints** (a separate bearer token, `ORBIT_REVIEW_TOKEN`, that can call only these three and nothing else; the main API token still works too):
- `POST /api/v1/review-agent/prepare` (`?force=1` to ignore the schedule window). Claims the planned week that is due now (`due_plan_start`,
  §2.17) and answers `{review_id, plan_start, system, prompt, schema}`: the review instructions, the full prompt (both snapshots, the previous
  review, the pending and rejected proposals), and the JSON schema of `ReviewOutput`. Answers `204` when nothing is due or the week is
  already ready or being made, so a timer can run every hour harmlessly. The claim uses the same unique row and the same attempt limit as §2.17.
- `POST /api/v1/review-agent/{id}/submit` with a `ReviewOutput` as JSON (and optional `model`, `input_tokens`, `output_tokens`). Validated
  with the same Pydantic model (`422` with the errors, so the session can fix and resend once), checked with `sanitize` against the snapshots
  stored at prepare time, stored, proposals created, push sent. `409` unless the review is `pending`.
- `POST /api/v1/review-agent/{id}/fail` with `{error}`: marks the review failed (the attempt count and retry rules of §2.17 apply).
The code change in `services/review.py` is a split of `generate` into `prepare` (claim, build request, store input) and `finish` (validate,
sanitize, store, proposals); `generate` becomes the two in a row around the API call.

**The script** (`scripts/weekly-review.sh`, runs on the VM host, not in a container; needs `curl`, `jq` and `claude`):
1. `prepare`; on 204 exit 0.
2. Run `claude -p` with `system` as the instructions and `prompt` as the input, asking for one JSON object that matches `schema`, with **no
   tools allowed**: the snapshots contain text written by people (notes, meeting titles), and a session with tools could be talked into
   acting on it. Without tools the worst a hostile note can do is skew the review, and every proposal still waits for a tap.
3. Extract the JSON, `submit`; on `422` resend once with the errors; on any failure call `fail` with a short reason and exit non-zero.
4. Log to the journal. The exact `claude` flags (headless, model, permissions) are confirmed against `claude --help` on the VM when this is built.
Timer: a systemd timer `orbit-review.timer`, hourly from Sunday 19:00 to Wednesday (`OnCalendar`, `Persistent=true`), so a missed Sunday is
caught up; `prepare` decides whether anything is due. The review token lives in a root-only file the unit reads, not in `.env` of the app.
One-time setup: log Claude Code in on the VM as an unprivileged user (a long-lived token for headless use, if the CLI offers it).

**Orbit with no API key.** The server job (§2.17) runs only when a Claude client is configured. Without `ANTHROPIC_API_KEY` capture saves the text
as a note draft, briefs skip the summary line, message drafts use the plain wish, and bank classification uses rules only (all already built).
`/reviews` says "reviews are made by the server script" instead of showing Generate now; a failed review shows the error and the command to rerun
(`scripts/weekly-review.sh --force`). With both an API key and the script, whoever claims the week first makes it.

**Rule change.** `CLAUDE.md` rule 5 says Claude calls go through `integrations/claude.py` only. The external session is a Claude *client of Orbit*,
not a call Orbit makes: it receives the same masked context and returns data Orbit validates. The rule is reworded to say so, with a
`DECISIONS.md` entry when this is built.

**Decided** (2026-10-05, the defaults above): the script runs on the VM (the Mac is the fallback); the model is the subscription default with an
optional `REVIEW_CLAUDE_MODEL`; no tools at all; a separate review token; the API path stays; a failure push at most once a day; no special
handling of Pro limits.

**Built as** `api/review_agent.py` (mounted outside the main API router, so the review token opens nothing else; the main token also works),
`services/review.py` (`generate` is now `prepare` + the API call + `finish`), `scripts/weekly-review.sh`, `deploy/orbit-review.service` and
`.timer`, `DEPLOY.md §10`, setting `ORBIT_REVIEW_TOKEN`. Differences from the text above:
- `submit` takes `{output, model?, input_tokens?, output_tokens?}` (the answer wrapped in an envelope), not a bare `ReviewOutput`.
- The script calls `claude -p --output-format json --json-schema <schema> --tools "" --no-session-persistence --strict-mcp-config
  --disable-slash-commands --system-prompt <system>` from an empty temporary directory, with the prompt on stdin. The envelope's
  `structured_output` is the answer; tokens and model come from its `usage` and `modelUsage`. A `422` from `submit` is fed back once.
- The empty directory and the two `--strict-mcp-config` / `--disable-slash-commands` flags matter: on a machine with many skills and MCP servers
  the same tiny review read about 115,000 tokens of Claude Code's own setup instead of about 8,000.
- Checked end to end on a scratch Orbit with a real Claude Code run on a Pro login: the review came back valid in about 30 seconds with
  sensible proposals. The 1 GB VM and the headless login (`claude setup-token`) are still to be tried on the server.

---

### 2.19 Claude worker: one Claude Code worker for every Claude call (Phase 17)
Production has no API key (`DECISIONS.md` #11). §2.18 made the weekly review work that way; capture, briefs, meeting linking, message drafts
and bank classification still fall back to their no-Claude modes. This section gives them all one path: Orbit puts the request in a queue,
a worker on the VM runs it through Claude Code (`claude -p`, no tools), and Orbit validates the answer exactly as it validates an API answer.
Claude still only proposes (L2). No autonomy is added.

**Design in one line.** `JobClaudeClient` implements the same Protocols as `ClaudeClient` (`CaptureExtractor`, `TransactionClassifier`,
`MeetingLinker`, `BriefSummarizer`, `MessageDrafter`). A call enqueues a `claude_job`, waits up to a per-kind deadline for the worker's
answer, and raises `ClaudeError` on timeout, so every existing fallback keeps working and **no service code changes**. Only the client
factory in `deps.py` picks the backend (`CLAUDE_BACKEND`, below).

**Queue and worker.**
- `ClaudeJob` (§3): kind, status `queued|running|done|failed|expired|cancelled`, priority, `system`, `prompt`, `schema`, caller deadline
  (`wait_until`), `attempts`, result, error, usage. The prompt is the one the existing `build_*_prompt` builders make, already masked
  (rule 5). The caller's deadline is stored so a job nobody waits for any more is not run (`expired`), unless it has a late-result handler.
- Worker (`scripts/claude_worker.py`, standard library only, runs on the VM host as `orbit-review`, a systemd service that restarts on
  failure). Loop: `claim` (long poll, up to 25 s, so a job is picked up in about a second and an idle worker costs one request per 25 s) →
  run `claude -p` exactly as `weekly-review.sh` does (no tools, empty working directory, `--json-schema`, `--no-session-persistence`) →
  `submit` or `fail`. One job at a time (the 1 GB VM, the 600 MB cap of §2.18).
- Priority: capture and drafts (someone is waiting, 1) before brief, linking and classification (background, 2), before the review (3).
- Lease: a `running` job whose worker went silent for 10 minutes (30 for the review) is requeued (max 2 attempts; a review job fails and the review row decides the retry), so a killed worker loses nothing.
- Validation stays in Orbit: `submit` parses with the kind's Pydantic model; `422` carries the errors and the worker retries once with
  them, as §2.18 does. The id checks and caps of each service run after, as for an API answer.

**Wait deadlines** (settings, per kind): capture 45 s, message draft 45 s, brief 120 s, meeting link 120 s, bank classification 120 s
(background callers: the scheduler and the import). On a timeout the job is cancelled if still queued, and the caller falls back as it
does today. Capture has one extra rule below.

**Late capture upgrade.** A Shortcut cannot wait minutes. When a capture's wait runs out, the request answers at once with the note
draft (as today) and the job stays queued (`late_json` holds the text, the currency and the goal ids that were sent). Orbit finds the draft by its text. When the answer arrives and that proposal is still `pending` and unedited, Orbit
replaces its payload with the extraction (the same code that builds a capture proposal), and sends the usual push ("2 items waiting in
Inbox"). If the proposal was already confirmed, rejected or edited, the result is dropped and logged. Nothing is ever written to people,
ledger or promises: it is still the same pending proposal (rule 1).

**Mitigations built into the infrastructure** (each is a limit of the CLI route):
| Limit | What Orbit does |
|---|---|
| Login token expires or is revoked | The worker checks `claude -p` with a tiny prompt at start and every 6 h, and reports `auth_ok` in its heartbeat. On `false`, one push a day: "Claude login expired on the server: run `claude setup-token`". While false, the client fails fast (no waiting). |
| Subscription rate limit | The worker detects the limit error, reports `rate_limited_until` and sleeps. While that time is in the future, interactive kinds fail fast to their fallbacks and background kinds wait in the queue. Review runs last. |
| CLI flags or output change | `claude --version` is part of the heartbeat; the worker's self-test (`claude_worker.py --selftest`) runs the real invocation against a tiny schema; the install notes pin the CLI version; a changed version since the last good self-test triggers a self-test before the next job. |
| Worker down | Every claim (the long poll, at most 25 s apart) is a heartbeat. If jobs are queued and the worker was last seen more than 10 min ago, one push a day. Meanwhile callers time out into their fallbacks, nothing breaks. |
| Queue grows | Cap on queued jobs per priority (default 10 interactive, 50 background); beyond it a call fails fast. The same input (hash of kind + prompt) enqueued within 60 s reuses the first job. |
| Free-text JSON | `--json-schema`, one retry on `422`, attempt cap, a failed job is kept with its error and has a Rerun button. |
| Memory on the 1 GB VM | One job at a time, `MemoryMax=600M`, swap file in the install notes. |
| Cost and usage blind spot | The worker sends tokens, model and duration from the CLI's JSON envelope; `submit` writes a `ClaudeCall` row (the table the API path uses), so the usage page covers both. |
| Visibility | `/ops/claude` page: worker last seen, CLI version, auth and rate-limit state, queue depth, last 50 jobs with status, duration, tokens, error and Rerun. |
| Terms or limits change | `CLAUDE_BACKEND=api` switches every kind back to `ClaudeClient` with no code change. |
| Prompt text is people-written | No tools, same as §2.18; output validated; every result is a proposal. |
| Stored prompts hold personal data | The prompt and result of a finished job are cleared after 7 days (a pruning job, like the 90-day backup pruning); the row, status and usage stay. |

**Review on the same worker.** The weekly review is a job kind `review` (`review_id` on the job). When the queue is empty, `claim` asks
whether a week is due and, if so, claims it (the same row and attempt limit as §2.17/§2.18) and hands the worker a job that is already running
(lease 30 minutes). Nothing starts while the login is bad or the worker is rate limited, and a rate limit or login failure does not use up a
review attempt. `submit` runs the same `review.finish` as the other paths and the push goes out once. A failed job marks the review failed, and
the schedule retries it. `orbit-review.timer` is turned off when the worker is installed; `scripts/weekly-review.sh --force` and the three
`review-agent` endpoints stay for a manual run and are retired after one real week on the worker. The worker has a reserved slot for captures and drafts (§2.34); before that it ran one job at a time, so a
review (a few minutes) delays a capture; that capture falls back and is upgraded later.

**Settings.** `CLAUDE_BACKEND` = `none|api|worker`; default `api` if `ANTHROPIC_API_KEY` is set, else `worker` if `ORBIT_REVIEW_TOKEN` is set
(it becomes the worker token, same scope plus the worker endpoints), else `none`. `WORKER_CLAUDE_MODEL` (optional, passed as `--model`),
wait deadlines, queue caps, `CLAUDE_JOB_RETENTION_DAYS` (7).

**Endpoints** (worker token; the main API token works too): `POST /api/v1/claude-worker/claim?wait=25`, `POST .../jobs/{id}/submit`,
`POST .../jobs/{id}/fail` (`{error, kind: auth|rate_limit|other}`), `POST .../heartbeat` (`{cli_version, auth_ok, rate_limited_until?}`),
`GET /api/v1/claude-worker/status`, `POST /api/v1/claude-worker/jobs/{id}/rerun`. Web: `/ops/claude`.

**Tests.** Fake worker in-process: enqueue → claim → submit round trip for each kind; timeout fallback; late capture upgrade (pending,
edited, confirmed); lease requeue; `422` retry; fast-fail while rate-limited or auth is bad; dedupe; queue cap; priority order; review
claimed through the worker; masking in stored prompts; token scope; pruning. The worker script has a test that runs it against a fake
`claude` executable and a fake Orbit.

**Decisions** (the owner, 2026-10-06, all as recommended): the late answer replaces the note draft in place; waits 45 s / 120 s; the review
moves onto the worker and the old script and timer are retired after one real week; prompts and results are kept 7 days; the worker runs
on the same 1 GB VM with swap; a cheaper model can be set for briefs, linking and classification (`WORKER_CLAUDE_MODEL_BACKGROUND`).

### 2.20 School module (Phase 18, university SIS)
**Status: Phase 18 built (2026-10-06; developed against saved feed samples and tried once against a real account's feeds in a scratch database, not run for a full semester on a deployed instance).
The owner's answers are recorded under "Open questions".** Phases 18b to 18d are not built; 18c and 18d still get a short spec step first; 18d waits for the
Microsoft 365 checks. "Built details" at the end of the Phase 18 part records the engineering choices made while building.
The research notes behind this section (endpoints, feed formats, risks) are not part of this repository.

**Fit with `VISION.md`.** School is a Later domain. Its value to the shared context is the **time and workload side of the week**:
lessons, exams, deadlines and enrolment windows next to people, money and goals ("exam Thursday, lab Friday, 5 gym sessions
planned"). The source is passive (a SIS calendar feed, `VISION.md` principle 2), it has a context provider and a snapshot field, and
the weekly review reads it, so it is not an isolated tracker. Autonomy: **L0 (observe) and L1 (inform)** only. No proposal kind is
added in Phase 18, because ingest from a calendar feed is deterministic source data, like the `Meeting` sync (§2.8). Proposals
arrive in Phases 18b to 18e, where Claude interprets free text (teacher announcements, course websites, school mail) or plans study time.

**Never ingested or stored** (decided by design, `CLAUDE.md` rules 4 and 5): CAS credentials, the SIS session id, national birth number,
home address, bank account, person/student numbers (UKČO, study id), card barcode, phone, e-mail. SIS shows them on its profile and on the
`stipendia` app; Orbit never reads those pages. Teacher names are stored as text exactly as SIS prints them (public catalogue data).

#### Sources (what SIS gives, how)
| Source | Needs | Phase | Gives |
|---|---|---|---|
| `GET <SIS_BASE_URL>/rozvrhng/roz_externi.php?format=ical&auth_key=<SIS_TIMETABLE_KEY>` | the timetable key (SIS Rozvrh → Export; `robots.txt` allows this path) | **18** | lessons with **separate teacher lines**; used for lessons when set (verified live 2026-10-06, 129 events, 11 tickets; fixture `roz_externi_ical.ics`) |
| `GET <SIS_BASE_URL>/ical.php?typ=<type>&token=<SIS_ICAL_TOKEN>` | the account-level token (the user generated it on SIS `harmonogram?do=exporty`) | **18** | `rozvrhng` lessons; `term_st2` registered exam terms; `stev` personal SIS events; `komise`, `esc`, `term_uc2` unused |
| Public course page `GET <SIS_BASE_URL>/predmety/index.php?do=predmet&kod=<code>&skr=<year>` | nothing | **18** | name, credits, hours, completion type, semester, guarantor, teachers, language, course web URL |
| Public harmonogram `GET .../harmonogram/index.php?do=filtr&fak=<fak>&skr=<year>&platnaod=<d>&platnado=<d>` | nothing | 18 (small) | only events SIS publishes to everyone, e.g. "Akademický rok - výuka" (teaching period) |
| Session-only pages (`predm_st2` enrolled, waiting list and enrolment windows, `term_st2` open terms, `zkous_st` results, `omne?do=email` teacher announcements) | a logged-in browser | **18b** | credits and enrolment state, waiting-list position and deadline, enrolment windows, grades, announcements |
| ReCodEx, Owl, course websites, school Microsoft 365 | see "Sources beyond SIS" below | 18 to 18d | assignment deadlines and points, announced deadlines, school mail and meetings |

Why not the session: SIS allows one session per user, a new login kills the user's own browser session, login is CAS SSO that Orbit must not
automate, and several modules end the session when fetched. The token feeds work without a session or cookie (verified). Findings that shape
the code:
- **robots.txt** disallows `/studium/rozvrhng/`, `/studium/stev/`, `/studium/lib/` for crawlers (only `rozvrhng/roz_externi.php` is allowed). Orbit
  therefore fetches `ical.php`, `predmety/` and `harmonogram/` only, never ticket detail pages, never `rozvrhng/` pages other than the
  token feed if the xCal option is ever chosen. A fixed `User-Agent` (`Orbit/<version> personal-assistant`), at most one request per 2 seconds, no parallel requests.
- The feed token is a secret in a URL. It lives in `.env` (`SIS_ICAL_TOKEN`), is never logged, never shown in the UI (show the last 4 characters at most) and never put in
  an exception message (httpx messages contain the URL: redact before logging, with a test).
- Rotating the token on SIS (`do=ical_reset`) breaks the feed: see "Feed problems" below.

#### Feed format (verified; real samples in `tests/fixtures/sis/`)
`text/calendar; charset=utf-8`, filename `siscal.ics`, `PRODID:-//Erudio//SIS//EN`, calendar `NAME:IS Studium`. An empty feed is a 125-byte VCALENDAR with
no VEVENT. A `rozvrhng` VEVENT (parse with the existing `icalendar` dependency; the non-standard `ALL_DAY:0` property is ignored):
```
UID:RNG26aNMAT101x04_2461313@10000@sis.example.edu     RNG<ticket>_<julianDayNumber>@<faculty>@sis.example.edu
DTSTART:20261006T102000Z  DTEND:20261006T115000Z   UTC
SUMMARY:Lineární algebra 1                          subject name (no teacher)
DESCRIPTION:Rozvrhový lístek: Lineární algebra 1; <teachers>; N7 12:20 1. - 13. týden NMAT101
LOCATION:N7 - Building A                                <room> - <building nickname>
STATUS:CONFIRMED   TRANSP:OPAQUE
```
- **UID prefix** (verified live 2026-10-06): `ical.php` UIDs start with a source tag, `RNG` for timetable lessons (`RNG26aNMAT101x04_2461313@10000@sis.example.edu`);
  the `roz_externi` feed has none (`26aNMAT101x04_2461313@10000`). The prefixes of `term_st2` and `stev` events are unverified (feeds empty). The feed **without**
  `typ` returns all sources selected on SIS in one calendar (today identical to `typ=rozvrhng` except `DTSTAMP`); Phase 18 still fetches per `typ` so each
  source gets its own `SisFetch` and empty-feed guard. **`Lesson.uid` is normalized** to `<ticket>_<julianDay>` (prefix and `@...` suffix dropped), so a lesson
  is the same row whichever feed delivered it; the raw UID goes to `raw_json`.
- **Ticket id** `26aNMAT101x04`: `26` year 2026, `a` winter (`b` summer), `NMAT101` course code, `p<n>` lecture parallel or `x<nn>` seminar parallel. Regex:
  `^(?P<src>[A-Z]+)?(?P<yy>\d{2})(?P<term>[ab])(?P<code>[A-Z]{3,5}\d{3}[A-Z]?)(?P<kind>[px])(?P<n>\d+)_(?P<jdn>\d+)@(?P<fak>\d+)`. A UID that does not match is still stored
  (a lesson with no ticket and no course), never dropped and never an error.
- **DESCRIPTION** is `;`-separated: `Rozvrhový lístek: <subject>; <teachers>; <room> <HH:MM> <weeks> týden <code>`. Teachers contain `,` (titles), so the
  separator is `; ` (semicolon + space) and the pieces are taken from the ends: first = label, last = room line, middle = teachers. If the shape differs, keep the whole text in
  `teachers_text = None` and `title`/`room` from SUMMARY/LOCATION. Multiple teachers are joined in the middle piece by `, ` and cannot be split reliably: store as one string.
- The feed holds the **whole semester** (129 events for 11 tickets in this semester). Times are UTC; Prague local time only for display (`CLAUDE.md` rule 3).
- **No cancellation marker** has been seen (`STATUS` was always `CONFIRMED`). A lesson that disappears from the feed is treated as cancelled (below).
- `term_st2`, `stev` feeds were **empty** during research: their event shape is unverified. Phase 18 parses them with the same generic VEVENT reader (title, start, end,
  location, description kept as text, `raw_json` for the full property set) and **a real sample must be added to `tests/fixtures/sis/` when the first exam is registered**
  (`CLAUDE.md` rule 9: stop and ask the owner for it; until then tests use a hand-built event and the UI labels these items "from SIS feed").

#### Sync (deterministic, idempotent)
- Scheduler job `sis_sync` every `SIS_POLL_MINUTES` (default 60) and "Sync now" (`POST /api/v1/school/sync`, UI button). One run per feed type in `SIS_FEED_TYPES` (default `rozvrhng,term_st2,stev`), sequential.
- The response body hash is stored in `SisFetch`; an unchanged body ends the run early (still a successful fetch).
- **Lessons** upsert by `uid` (the VEVENT UID; unique): a changed time or room updates the row and records the change for the "schedule changed" push. A lesson not in the feed any more, starting in the future, is marked `cancelled` (never deleted; it comes back if it reappears).
  Past lessons are never touched. **Guard:** only when the feed parsed with at least one event; an empty or failed feed never cancels anything (it counts as a feed problem).
- **Exam terms and SIS events** (`term_st2`, `stev`) upsert by `uid` into `SchoolEvent` the same way; vanished = `cancelled`.
- **Public harmonogram** (once per day): the teaching period and any other event SIS publishes anonymously for `SIS_FACULTY_ID` in the school year of the newest lesson become `SchoolEvent` rows (`source = harmonogram`, `kind = term`, `uid` = hash of faculty + title + start, all-day style `00:00` to `23:59` Prague). Enrolment windows are **not** public (anonymous answer: "nebyl zveřejněn"), so they wait for Phase 18b. Fixture: `harmonogram_mff_2026_public.html` (rows are `<tr class="row1|row2">` under `head2` month headers; columns `Od | Do | Fakulta | Název | Poznámka`, `dd.mm.yyyy hh:mm`).
- Course link: `Lesson.course_id` is found or created by the ticket's course code (`Course.code` unique). New courses are queued for the public course page (below).
- Courses are enriched at most once per 30 days per course, one at a time, 2 s apart, in the same job. A failed course page never fails the sync (log, retry next run, at most 3 tries per day).
- **Feed problems:** HTTP 401/403/404 (token rotated or disabled), a non-calendar body, a timeout, or an empty feed while future lessons exist set `SisFetch.status` to `denied | error | empty` and send **one** push per day ("SIS calendar feed stopped working: re-enable the export on SIS and update `SIS_ICAL_TOKEN`") with dedupe key `school:feed:<date>`. Never retry in a loop; the next poll tries again. Network errors are retried with the normal poll.

#### Course page parser (public HTML, fixture `tests/fixtures/sis/course_NINF030.html`)
The detail is a table of `<tr><th>Label:</th><td>value</td></tr>`. Czech labels used: `Anglický název`, `Zajišťuje` (department with code `32-KINF`), `Fakulta`, `Platnost`, `Semestr` (`zimní`/`letní`),
`E-Kredity` (int), `Rozsah, examinace` (`zimní s.:2/2, Z [HT]`: lecture hours/seminar hours per week, completion `Z` credit, `Zk` exam, `Z+Zk` both), `Stav předmětu`, `Jazyk výuky`, `Další informace` (web URL),
`Garant`, `Vyučující`. Header text `<name> - <code>`. Parse by label, tolerate missing labels, ignore unknown ones. The page is UTF-8. Annotation and syllabus blocks are not stored in Phase 18 (a later agent may need the annotation: then fetch on demand).
A missing page or a changed layout leaves the course with what the lessons gave (code, name from SUMMARY); the English mirror (`/studium/eng/`) is never used.

#### Models (`SPEC §3`, migration `0017`)
`Course`, `Lesson`, `SchoolEvent`, `SisFetch` (fields in §3). Lessons are **not** `Meeting`s: a meeting is a CRM object with people links, briefs and capture prompts; a lesson has none of that. Lessons are not added to Orbit's own ICS feed
(`/calendar/{ICS_TOKEN}.ics`): the owner can subscribe the SIS feed directly in Apple Calendar, and a second copy would show every lesson twice (open question 3).

#### Manual items
SIS does not know assignments, project deadlines or Moodle quizzes. `SchoolEvent` rows with `source = manual` cover them: a form on `/school` (title, kind `deadline | exam | other`, date and optional time, optional course, note) and `POST /api/v1/school/events`. Manual items are edited and
deleted by the owner only; the sync never touches them. Capture of deadlines from dictation, websites and mail (the `school_deadline` proposal kind) is Phase 18c.

#### Context provider `school` (`orbit/services/context/school.py`, `§2.14` rules)
`build(session, window, focus) -> SchoolContext`. Masked and bounded like every provider; free text through `context.base.text()`; no tokens, ids or URLs of SIS.
- `lessons`: not-cancelled lessons starting in the window, soonest first, at most 300: `id`, `course` (code, name), `kind`, `title`, `start`/`end` (local times), `room`, `teachers`.
- `cancelled_lessons`: lessons in the window cancelled since the last 14 days (so a review can say "Tuesday's lab was cancelled").
- `exams_and_deadlines`: `SchoolEvent`s of kind `exam | deadline` from today to 60 days past the window end, soonest first, at most 100, with `days_until` and `in_window`; plus the ones that passed in the last 14 days.
- `workload`: per window day `lesson_minutes`, `first_start`, `last_end`; totals `lesson_hours_in_window`, `exams_next_14_days`, `deadlines_next_14_days`.
- `courses`: courses with a lesson in the current semester: code, name, credits, completion, hours, `next_lesson`; and `credits_total` (sum of known credits; a course without credits is counted in `credits_missing`).
- Focus: none only (no person or goal link yet). A person or goal focus returns an empty `SchoolContext` (not an error). The snapshot (`§2.15`) gets a `school` field after `goals`; the budget layer trims lessons before it trims exams and deadlines.
- The weekly review input gains `school`; **this changes what Claude sees**, which `DECISIONS.md` #7 already allows (school data goes to Claude in full). The review prompt gets one added sentence ("school: lessons, exams, deadlines and workload"); the output schema does not change.

#### Notifications (L1; `§2.11` transport, quiet hours and dedupe rules apply; kinds `school_*`)
| Push | When | Dedupe key | Text |
|---|---|---|---|
| Tomorrow's lessons | `SCHOOL_EVENING_TIME` (default 20:00 Prague), only when there is at least one lesson tomorrow | `school_tomorrow:<date>` | "Tomorrow: 4 lessons, first 9:00 N8 (Algoritmizace)", up to 6 lines, one **Open** button to `/school` |
| Exam reminder | 7 days and 1 day before an exam (`SchoolEvent.kind = exam`), at 18:00 | `school_exam:<id>:<start>:7d` / `:1d` | "Exam in 7 days: Databázové systémy, Tue 12.1. 08:00, room" |
| Deadline reminder | 3 days and 1 day before a deadline | `school_deadline:<id>:<start>:3d` / `:1d` | |
| Schedule changed | after a sync that moved, added or cancelled lessons in the next 14 days; at most one per hour, batched | `school_change:<hash of the change list>` | "Schedule change: Programování 1 Thu 12:20 moved to Fri 9:50", up to 5 lines |
| Feed problem | see "Feed problems" | `school:feed:<date>` | |
The Sunday digest (`§2.11`) gets one section "School next week": number of lessons, exams and deadlines, at most 5 entries. Nothing is pushed for a plain lesson starting soon.

#### Web UI (`/school`, login required)
- **Week** view (default current week, prev/next, "this week"): one block per day listing lessons (time, course, kind, room, teachers), exams and deadlines of that day marked; cancelled lessons struck through. Mobile first, no JS (plain forms and links).
- **Upcoming**: exams and deadlines for the next 60 days with a countdown; the "add deadline" form; delete for manual ones.
- **Courses**: this semester's courses (code, name, credits, completion, hours, guarantor, web link). `/school/courses/{code}`: details, the lessons, a link to the SIS course page (public URL).
- **Sync status** (`/school/status`): last fetch per feed (status, items, time, error text without the token), "Sync now", and the Owl courses with a course
  picker ("Not one of my courses" or a course; the pick is stored and kept).
- Home: a strip with the next lesson and the nearest exam/deadline; nav link "School". Everything is in the full JSON export (§2.12). No data is entered here for lessons: they only come from the feed.

#### Settings (`orbit/settings.py`, `.env.example`)
`SIS_BASE_URL` (default `https://sis.example.edu/studium`), `SIS_TIMETABLE_KEY` (`SecretStr`, optional, `roz_externi` lessons), `SIS_ICAL_TOKEN` (`SecretStr`, optional: exams and SIS events; lessons too when there is no timetable key; with neither the module shows "SIS feed not set up"), `SIS_FEED_TYPES` (comma list, default `rozvrhng,term_st2,stev`), `SIS_POLL_MINUTES` (default 60, min 15), `SIS_FACULTY_ID` (default `10000`, for the public harmonogram), `SIS_COURSE_TTL_DAYS` (default 30), `SCHOOL_EVENING_TIME` (default `20:00`), `SCHOOL_PUSHES_ENABLED` (default true), `RECODEX_BASE_URL` (default `https://recodex.example.edu/api/v1`), `RECODEX_ICAL_TOKEN` and `RECODEX_API_TOKEN` (`SecretStr`, both optional, redacted like the SIS token), `OWL_BASE_URL` (default `https://owl.example.edu`), `OWL_SESSION_COOKIE` (`SecretStr`, optional). Without the token the page shows "SIS feed not set up" and manual deadlines still work.

#### Code layout (follows `meetings`)
`orbit/domain/school.py` (models; enums `SchoolEventKind`, `SchoolEventSource`, `LessonKind`, `FetchSource`, `FetchStatus`, `ScoreSource`, `Semester` in `domain/enums.py`),
`orbit/integrations/sis.py` (`PoliteClient`: GET only, one request at a time, `Throttle` of 2 s, fixed user agent, every secret redacted from errors; `SisSource` Protocol with
`feeds`, `fetch_feed(feed) -> FeedFetch`, `fetch_course(code, year) -> CoursePage | None`, `fetch_harmonogram`; parsers `parse_feed`, `lesson_from_event`, `parse_course_page`, `parse_harmonogram`),
`orbit/integrations/recodex.py` (feed, read-only API, JWT claims) and `orbit/integrations/owl.py` (path allow-list, course list and course page parsers), all sharing one `PoliteClient`
(built in `app.build_school_sources`), `orbit/services/school_sync.py` (ingest of every source, feed-problem pushes), `orbit/services/school.py` (queries, week view data, manual events,
push builders, digest section, Home strip), `orbit/services/context/school.py`, `orbit/api/school.py`, `orbit/web/school.py` + `templates/school/`, scheduler jobs in
`orbit/jobs/scheduler.py` (`sis_sync`, `school_pushes`), migration `0017_school.py`.

#### Tests (`CLAUDE.md` rule 7)
Fixtures (already in the repo): `tests/fixtures/sis/ical_rozvrhng.ics` (real feed of 2026-10-06, one lesson per ticket, 11 tickets, teacher names replaced), `roz_externi_ical.ics` (same for the timetable-key feed), `ical_empty.ics` (the real 125-byte empty feed), `course_NINF030.html` (real public page), `harmonogram_mff_2026_public.html` (real anonymous page). Needed tests: feed parser (UID regex including a non-matching UID, DESCRIPTION pieces, UTC, `ALL_DAY` ignored, an empty feed); sync upsert idempotency (second run changes nothing), a moved lesson, a vanished future lesson becomes cancelled and comes back, a past lesson is never touched, an empty feed never cancels and sets `empty`, a 403 sets `denied` and sends one push per day; course parser against the fixture (credits 5, `2/2`, `Z`, winter) and a page with missing labels; enrichment throttle and 30-day TTL (fake clock); token redaction in logs and in `SisFetch.error`; provider output for a fixed week, masking, cap, focus returns empty; snapshot budget trims lessons first; each push builder with its dedupe key and quiet hours; web pages (week navigation, add and delete a manual deadline, unauthenticated redirect); migration up and down. Mocks: `SisSource` is faked, no test touches the network.

#### Built details (Phase 18, decided while building)
- **Unchanged body:** the hash that ends a run early ignores `DTSTAMP` lines (SIS stamps every download), so an unchanged timetable really is "unchanged". An empty fetch is never "unchanged": it is looked at (and reported) every time.
- **Lesson changes:** a sync stores what changed on the lesson (`changed_at`, `change_note`: "moved from Thu 1 Oct 9:50, room N8 → N9", "new", "cancelled",
  "back on"). The first import of an empty database is not a change. The `school_pushes` job sends one push for lessons of the next 14 days changed since the last
  change push (and in the last 2 days), at most one per hour; the dedupe key is `school_change:<hash of the lines>`.
- **Empty feeds:** an empty `term_st2`, `stev` or ReCodEx feed is normal (nothing registered) and sends no push; only an empty lesson feed while future lessons
  exist is a feed problem. Problem push keys: `school:feed:<date>` (SIS), `school:recodex:<date>` (ReCodEx denied), `school:owl:<date>` (Owl cookie).
- **Course pages:** one `SisFetch(source = course)` row per run for all courses fetched in it; failures are counted on the course (`detail_tries`, reset each day).
  The page year is the school year of the course's newest ticket. Multiple guarantors are joined with "; " (names contain commas).
- **Exam and SIS-event rows** are linked to a course when exactly one known course code appears in their summary or description.
- **ReCodEx:** API rows only fill the title, location and URL of a row the feed made (no language flip-flop); points, max points and the second deadline always
  come from the API. One `CourseScore` per course sums its groups.
- **Owl:** courses are kept in `OwlCourse` (path, name, link, `link_manual`, the course description). Fetched: the current school year's semesters, tickets of this
  year, misfiled courses whose name is one of this year's courses, and courses the owner linked by hand. Topics without a deadline are not stored as rows (they count
  in the course total). A `CourseScore(source = owl)` exists only for a linked course. A vanished topic is cancelled only within its own Owl course.
- **Snapshot:** `school` comes right after `goals`; the budget layer trims a module with `trim_in_order` from its first non-empty list (lessons, then cancelled
  lessons, then courses, then exams and deadlines), every other module from its biggest list as before.
- **Export:** decimals (points) are exported as strings.

#### Phase 18b: session-only data through a browser helper (separate spec step, after Phase 18 runs for two weeks)
Goal: enrolled subjects with credits and state, waiting list (position and "wait until" date), the enrolment windows of the faculty, open exam terms, results and **teacher announcements** (`omne?do=email`, "Sdělení studentům rozvrhového lístku <ticket>").
Mechanism: Orbit never logs in and never holds a session. A helper that runs **in the owner's own logged-in browser** reads the pages and POSTs them to Orbit: `POST /api/v1/school/sis-import` (bearer token) with `{kind: enrolled | waiting | windows | exam_terms | results | announcements, html: "<page html>"}`; Orbit parses the HTML on the server (parsers and fixtures live in the repo, so a SIS layout change shows up as a failing parse, not a broken helper). The helper is an iOS Shortcut ("Run JavaScript on Web Page" in Safari) or a bookmarklet that opens `index.php?sso` first (one click, CAS cookie, no password), then `fetch`es the pages with the session `id` from the address bar. Whether the Shortcut route can run `fetch` and POST cross-origin to Orbit is **unverified**: test before committing to it.
Facts to respect (from the research notes, not in this repository): the one-session rule (the helper must reuse the page's session and never start another login), `KEY` is not needed, English mirror is never used, `do=notify` consumes the unread-mail item (use `notify_check` only), and the modules that end the session (`prihlastaz`, `stipendia`, `prijimacky`, external hops) are never fetched. Write actions (enrol, cancel, register for an exam) are never called. Waiting-list and enrolment-window pushes use the same L1 push machinery.
Announcements: the teacher's text is stored on a `SchoolAnnouncement` row (ticket, course, sender text, sent time, body) and may be given to Claude by the existing **capture** pipeline as a source (L2: it produces the usual `capture` proposal with promises and deadlines; nothing is created until confirm). That sends teacher e-mails to Claude, which `DECISIONS.md` #7 allows, but the first run asks the owner. Synthetic HTML fixtures for each page kind are written at the start of 18b.

#### Sources beyond SIS (ReCodEx, Owl, course websites, Microsoft 365)
The owner's courses also live outside SIS (Microsoft 365, Moodle); the checks that decide what is possible are listed under 18d below.
Every source below follows the module contract: it feeds the same `Course` / `SchoolEvent` tables and the same `school` provider (no
separate tracker per site), deterministic data is source data (L0), and anything Claude reads out of free text becomes a proposal (L2).
Orbit never stores a CAS or Microsoft password, never logs in by itself and never calls a write action on any of these systems.

| Source | Mechanism | Phase | Gives | Status |
|---|---|---|---|---|
| ReCodEx iCal | `GET https://recodex.example.edu/api/v1/users/ical/<RECODEX_ICAL_TOKEN>` (token made once on the ReCodEx profile, no expiry) | **18** | first deadline of every assignment in the owner's groups, group name, assignment URL | verified live, fixture in repo |
| ReCodEx API (optional) | `read-all` bearer token (`RECODEX_API_TOKEN`, made on the profile, at most 356 days) | **18** | exact course link (SIS ticket), second deadline, max points, own points, pass threshold | verified live, fixtures in repo |
| Owl course pages | `GET` with the copied `owl_session` cookie (`OWL_SESSION_COOKIE`) | **18** | topics with deadline, own points, max, course total and pass threshold, course rules text | structure verified live; real-page fixture needed |
| Owl notification mail | mail ingest (18d) recognises `X-Owl-*` headers | 18d | new teacher comments and grading on a topic | verified in source |
| Course and teacher websites | polite daily fetch of known URLs, change detection, Claude reads the change | **18c** | announced deadlines, changed rules, new materials | per page |
| Outlook mail and calendar, Teams | Microsoft Graph (delegated) if the tenant allows user consent; else published-calendar ICS and a share-sheet Shortcut | **18d** | teacher mails, Teams notification mails, Teams/Outlook meetings | the consent check decides the route |

**ReCodEx (Phase 18, same machinery as the SIS feeds).** Verified live 2026-10-06; fixtures `tests/fixtures/recodex/` (`ical.ics`, `user_groups.json`,
`group_assignments.json`). The `sis_sync` job fetches it as one more source (same throttle, user agent, token redaction and "feed problem" push).
- **Feed** (`SisFetch.source = recodex`; HTTP 400 "expired" = `denied`): each VEVENT becomes `SchoolEvent(source = recodex, kind = deadline,
  uid = VEVENT UID "recodex/<user>/<assignment>", title = SUMMARY without the "ReCodEx deadline: " prefix and with runs of spaces collapsed, url = URL)`.
  `DTSTART` is a **floating time that is really UTC** (verified against the API): parse it as UTC. Vanished future assignments are `cancelled` with the same guard as lessons.
- **API** (`RECODEX_API_TOKEN`, optional, `SisFetch.source = recodex_api`): the user id is the JWT `sub`. `GET users/<me>/groups` gives the student groups and the
  stats in one call; `GET groups/<id>/assignments` per group gives both deadlines and max points. Course link is **exact**: the group description names the SIS
  ticket (`` `26aNINF030x03` ``) and course code, so `course_id` comes from the code (the feed row gets it through the shared assignment id; a feed-only setup
  falls back to matching the group name against this semester's lesson day, time and room, else no course). The API fills `points` (gained), `max_points`,
  `second_deadline_utc` on the rows, and creates rows the feed has not delivered yet (same uid shape). Per course one `CourseScore(source = recodex)`:
  gained points, total, pass threshold (`privateData.threshold` × total, or `pointsLimit`), `passes`. Shadow assignments (points outside ReCodEx) count to the total only.
- Read-only: Orbit calls only these GETs. The token cannot be extended (`refresh` keeps its expiry, 2027-09-27 for the current one): a push 14 days before
  `exp` (decoded from the JWT, not verified) says "make a new ReCodEx read-only token", dedupe `school:recodex_exp:<exp>`.
- Pushes: deadlines use the existing 3-day and 1-day reminder; the text adds "0/1 points so far" when known, and the 1-day reminder is skipped when the
  assignment has full points.

**Owl (Phase 18, with a copied session cookie; the owner's choice, question 13, `DECISIONS.md` #13).** Owl has no student API and no feed: its API keys are
per course and teacher-only, and the "access key" login is made by an admin for people without a university account. The owner copies the `owl_session` cookie
from their logged-in browser (Chrome DevTools → Application → Cookies → `owl.example.edu`) into `.env` as `OWL_SESSION_COOKIE` (`SecretStr`). The
`sis_sync` job then reads, with that cookie and nothing else: `GET /c/all` (every course the owner is enrolled in, in every semester; the home page `/` hides
semesters Owl does not advertise, and a teacher may file a course under the wrong one, e.g. a Linear Algebra course filed under last summer: `/c/ls2526/la1-svobodova/`)
and `GET /c/<sem>/<course>/` per course of the current school year
(`SisFetch.source = owl`, same throttle and user agent). Rules: **GET only**, never a topic or post page that marks threads read (`/c/<sem>/<ticket>/<topic>/` is
never fetched, so Owl's "new" markers stay the owner's), never `/acct/*`, `/join/*`, `/settings/*`, never a POST (the "Leave the course" link on a course page is never followed). The cookie is a secret: never logged,
never in an exception, never shown. **Expiry:** a redirect to `/acct/login/` (or HTTP 401/403) means the cookie died; `SisFetch.status = denied` and one
push per day "Owl login expired: copy a new `owl_session` cookie into `.env`", dedupe `school:owl:<date>`. How long a cookie lives is unverified (Flask
default 31 days from login; record the real lifetime after the first expiry). The browser helper of 18b is not needed for Owl. Owl course ids are **usually** SIS
ticket ids (`26aNMAT202x06`): then the course link is exact. Otherwise (`la1-svobodova`) the course name (`Lineární algebra 1`) is matched
diacritics-insensitively against this year's `Course` names, a teacher surname in the id breaks a tie between parallels, and with no single match the
Owl course is shown unlinked with a "link to course" select (the owner's choice is stored; never guessed). Parsed rows (verified structure, fixture `tests/fixtures/owl/course_NMAT202_handbuilt.html`: `table.topics` rows, deadline text
`2026-10-15 15:40 (in 8 days)` read as Prague time with the relative part dropped, `pts` / `pts-binary` cells, `h3` section headings kept as the
topic group) become `SchoolEvent(source = owl, kind = deadline, uid = "<sem>/<ticket>/<topic-ident>")` with points and max points (topics without a
deadline are stored only for their points), and one `CourseScore(source = owl)` row per course from `#totalpoints` and `#totalthresh`. The course
description text is kept (it holds the grading rules, useful context for Claude) and its external links are offered as `CourseSite` URLs in 18c. Owl notification mails are ingested only through 18d mail
(they carry `X-Owl-Course` and `X-Owl-Topic`, so they link to the same row without Claude).

#### Phase 18c: school map and resources (accepted 2026-10-06, built 2026-10-07; the questions below are answered)
The owner wants one visual map of all their classes and every source behind them, where they can **add resources** (a teacher's page, a PDF, a GitHub repo,
a note) that Claude then reads for deadlines and course rules. This replaces the earlier "course and teacher websites" plan: a website is one kind of resource.

**Fit with `VISION.md`.** Resources are ingest from real sources, idempotent by content hash; what Claude reads becomes proposals (L2); the `school`
provider gains course facts, so the weekly review sees grading rules and test dates. Claude never fetches anything itself: the worker runs `claude -p`
without tools (`DECISIONS.md` #11), so **Orbit fetches and extracts text, Claude only reads text Orbit gives it**.

**The map (`/school/map`, login required): a data flow map.** Approved design: the mockup
a private mockup that is not part of the repo. Four columns, left to right:
1. **Sources**: systems (SIS, ReCodEx, Owl, Outlook + Teams) on top, then the resources grouped by course. Card = kind icon, label, course-colour
   dot, state pill.
2. **Processing**: Sync (SIS, ReCodEx, Owl), Page watcher, Mail reader (18d), Claude reader (reads used / budget), Inbox (proposals waiting).
3. **Courses**: one card per course in its week-view colour, with code, credits, completion and a progress bar toward credit.
4. **In Orbit**: Week view, Deadlines + pushes, Credit progress, Weekly review.

Edges are horizontal cubic Béziers from a card's right port to the next card's left port. **Width grows with volume** (log of the item count). **Moving
dots** animate along edges that carry data now (off under `prefers-reduced-motion`). Dashed = suggested or queued, red dashed = blocked, blue dotted =
"after you confirm" (Inbox → course). Edges into and out of a course take the course colour. Edge labels ("129 lessons", "5 proposals") show on
hover or while tracing. **Tracing:** clicking a card highlights its whole lineage and fades the rest; for a course only its own sources,
processing steps and outputs (not the other courses that share a processing step). States are shown in form and words, never colour alone
(connected, changed today, suggested, login only, stale, blocked, queued). Interaction: drag the background to pan, pinch or Ctrl/⌘ + wheel to
zoom (plain wheel scrolls the page), drag a card to move it (per session only), a button fits and resets. The map fits its width; long columns
are panned. The side panel (HTMX partials) shows the selected card: overview when nothing is selected; for a course its rules with quotes and
the "Add resource" form (link, PDF file, note; the kind is detected from the URL); for a resource its URL, origin, last check, diff, and Check
now / Remove, or Keep / Dismiss for a suggestion. A **List** toggle shows the same data grouped by course, the default under 700 px.

**Rendering (`DECISIONS.md` #14):** no graph library. Orbit computes the layout in Python (columns, resources stacked per course, courses
spread over the height, processing cards placed next to what they read) and serves it as JSON (`GET /school/map.json`: nodes with position,
size, kind, state and labels; edges with kind, volume, course and label). The page draws cards as HTML and edges as one inline SVG, with one
inline script of about 300 lines for pan, zoom, drag, edge redraw and tracing. No CDN, no npm, no build step.

**Resources.** `SchoolResource` rows (replaces `CourseSite`):
- **Kinds** (detected from the URL and the response `Content-Type`): `page` (HTML), `pdf` (a PDF by URL), `file` (an uploaded PDF), `github` (a repo, read
  through its `commits.atom` and README), `note` (text the owner pastes, e.g. rules said in class), `login` (Moodle and other pages behind a login:
  listed, never fetched).
- **Where they come from** (`origin`): `sis` (course `Další informace`), `owl` (links in an Owl course description), `ticket` (SIS ticket note `Info:`,
  through the 18b helper), `claude` (a link Claude found in a resource it read), `manual`. Everything not `manual` starts as a **suggestion**
  (dashed node on the map) and is fetched only after the owner keeps it.
- **Adding:** "Add resource" on a course node (URL, PDF upload up to 20 MB, or a note); `POST /api/v1/school/resources` (bearer) for an iOS share-sheet
  Shortcut "Add to Orbit school" (URL or file, optional course code). A resource without a course is shown in an "Unsorted" column until linked.
- **Fetch** (job `school_resources`, daily at `SCHOOL_SITES_HOUR`, default 07:00, and "Check now"): one host at a time, 5 s apart, fixed user agent,
  `robots.txt` respected, conditional GET (`ETag`, `Last-Modified`), body cap 20 MB, charset from the HTTP header or `<meta>` (some teacher pages are
  windows-1250). Text extraction: HTML to text with the standard library parser (scripts, styles, navigation dropped); PDF to text with **`pypdf`**
  (new dependency, question B); a PDF with no text layer is marked "no text" (no OCR). The text hash decides "changed"; the old text is kept so the
  diff can be shown. New links found on a page are not followed; they can become suggestions only through Claude (below).
- **Files** live under `data/school/files/<sha256>.pdf` (not in git, in the server backup); the JSON export has their metadata, not their bytes.
- Failing 7 days in a row = `broken`, shown red on the map, still retried daily.

**The reader agent (`SchoolReader`, worker job kind `school_read`, L2).** Runs when a kept resource is new or changed, at most
`SCHOOL_READS_PER_DAY` (default 20) per day to protect the subscription; the rest wait for the next day and the map says so. Input: the course
(name, code, teachers, lesson times), its known deadlines and facts, and the resource text: the **diff** for a changed page, the whole text for a new
resource, capped at 60 000 characters (longer PDFs are read in chunks, one job per chunk). Output, validated by Pydantic:
`{deadlines: [{title, kind exam|deadline|test|other, due, all_day, points?, quote}], facts: [{topic grading|attendance|exam|other, text, quote}],
links: [{url, why}]}`. Every item carries a quote from the text, so the proposal shows where it came from.
- deadlines become **`school_deadline`** proposals (below); facts become **`course_fact`** proposals (new: apply writes a `CourseFact` row the
  provider and the course page show, e.g. "credit: 120 of 240 points, two tests of 60"); links become suggested resources (no proposal, the map shows
  them dashed with Claude's reason). Nothing is written without the owner's confirm. The first run asks the owner once (it sends course pages and PDFs to Claude;
  `DECISIONS.md` #7 covers school data).
- **`school_deadline` proposal kind** (new; also used by 18b announcements, 18d mail and dictation): payload `{course_id?, title, kind, start_utc, end_utc?,
  all_day, note, source_kind site|announcement|mail|dictation, source_ref, quote}`. Apply creates a `SchoolEvent(source = proposal)` that the owner edits like a
  manual one. Dedupe: no proposal when an event of the same course with a similar title (normalized, diacritics-insensitive) exists within ±1 day, or
  when a pending/rejected proposal has the same `(course, normalized title, date)`.
- **Provider:** the `school` context gains `facts` per course (confirmed `CourseFact`s) and `resources` (count, last change) so the weekly review can
  say "LA test 1 is in 5 days, it is 60 of the 120 points you need".
- **Pushes:** one per day for kept resources that changed ("Course pages changed: NMAT101 (Svobodová), NMAT202"), dedupe `school_resources:<date>`;
  the proposals land in the Inbox as usual.

**Built as** `domain/school.py` (`SchoolResource`, `CourseFact`), `integrations/resources.py` (the polite fetcher, text extraction, `pypdf`), `services/school_resources.py` (add, keep, dismiss, check, due, suggestions), `services/school_reader.py` (the reader, the two proposal kinds and their handlers, the daily push, `check_now`), `services/school_map.py` (layout and states), `jobs/scheduler.py` (`school_resources` in the registry, Run now), `api/school_resources.py`, `web/school_map.py` with `templates/school/map.html` and `_map_panel.html`, `integrations/claude.py` (`SchoolReadOutput`, `read_school`), worker job kind `school_read`, migration `0020`. Details the text above leaves out:
- **Job.** `school_resources` ticks every 15 minutes (a Run now button and an entry on `/settings/schedule`); each kept page, PDF or repository is fetched once a day from `SCHOOL_SITES_HOUR`, at most 60 per tick. The hour and `SCHOOL_READS_PER_DAY` (default 20) are editable on `/settings/schedule` (§2.21). One tick: suggest links (the SIS course page, links in Owl texts), check what is due, let Claude read what is new or changed within today's budget, then at most one push a day.
- **Reads** count from the usage log (`ClaudeCall` purpose `school_read`), so the API path and the worker path share one budget. A resource is read whole or not at all today (a long text is read in parts of 60,000 characters, one read each); one that does not fit waits for tomorrow. A Claude error leaves it unread for the next tick. A changed page is read as a diff, a new one whole.
- **Proposals.** A `test` becomes an exam event (reminded 7 and 1 days ahead); a `deadline` a deadline event; `other` an event of kind other. Dates in the past are dropped. A fact needs a course (a resource without one gets no facts). Links are never proposals: they become suggested resources (origin `claude`) with the reason shown.
- **Suggestions** are not fetched until kept, and a dismissed one is never suggested again (the row stays). A login-only page (Moodle, Teams, CAS hosts) is listed and never fetched.
- **Safety.** Only `http` and `https`, no credentials in the address, no localhost, `.local` or private addresses (also after a redirect), at most 5 redirects, `robots.txt` respected, one host at a time 5 s apart, 20 MB cap. Failures count once a day; seven days in a row make a resource `broken`.
- **Files** are checked to start with `%PDF`, stored as `<sha256>.pdf` under `SCHOOL_FILES_DIR` (Docker: the `/data/school/files` volume) and not in the nightly database backup: their extracted text is in the database, so a lost file loses only the original. A PDF without a text layer is kept as `no_text` (no OCR).
- **Map.** Four columns computed in Python; edges run left to right, except the processing steps, which connect top to bottom inside their column. The page script is inline (no library, no CDN): drag to pan, Ctrl or ⌘ and the wheel or two fingers to zoom, drag a card, click to trace, Esc to clear, Fit and Reset. The List view is the default under 700 px and without JS. Colour is never the only signal: every card names its state. The page breaks out of the 640 px column (up to 1600 px); Fit scales the map to the stage (text never below 0.4×, 0.6× on phones, which pan instead) and shrinks the stage to a short map; zoom buttons, pinch and a resize refit until the user touches the map; the side panel sits beside the map above 1000 px and below it otherwise.
- **Not built / not verified:** a real-browser smoke test of the map script (no login is possible in the test setup: the page, the JSON, the panels and the forms are tested, the pointer and zoom code is not); the Claude-found-link flow has run only against fakes.

**Models (§3):** `SchoolResource(id, course_id?, kind, origin, url?, file_sha256?, label, kept, status[ok|unchanged|changed|no_text|error|broken|login],
etag?, last_modified?, content_hash?, text?, prev_text?, last_diff?, checked_at?, changed_at?, read_at?, fail_count, error?, suggested_reason?)`,
`CourseFact(id, course_id, topic, text, quote, resource_id?, created_at)`; proposal kinds `school_deadline`, `course_fact`.

**Answers (the owner, 2026-10-06; E, F, G confirmed 2026-10-07 by building as recommended):** A: **interactive data flow map** as in the approved mockup, drawn without a library (`DECISIONS.md` #14). B: add `pypdf`. C: deadlines and course rules. D: Claude may suggest
links. E, F, G not asked: the recommendations stand as defaults (20 reads a day, 20 MB uploads under `data/school/files/`, share-sheet Shortcut) until the owner says otherwise.

**Open questions for 18c** (recommendation first):
- A. **Map rendering:** server-side SVG made by Orbit (no dependency, static layout, hover only)? *Recommended.* Alternative: a JS graph library
  from a CDN (drag, zoom; a new dependency and against the no-JS-build spirit).
- B. **PDF text:** add `pypdf` (pure Python) for PDFs? *Recommended: yes.* Without it, PDFs are listed but not read.
- C. **What Claude extracts:** deadlines **and** course rules (`course_fact`)? *Recommended: both.*
- D. **Suggested links:** may Claude suggest new resources from links it sees (shown dashed, fetched only after you keep them)? *Recommended: yes.*
- E. **Daily read budget:** 20 Claude reads per day? *Recommended.*
- F. **Uploads:** PDFs up to 20 MB stored on the server under `data/school/files/`, metadata in the export? *Recommended.*
- G. **Share-sheet Shortcut** "Add to Orbit school" for URLs and files from the phone? *Recommended: yes.*

#### Phase 18d: school Microsoft 365 (Outlook mail and calendar, Teams) (route B chosen 2026-10-09, `DECISIONS.md` #31: the tenant blocks Graph consent; route A text kept for reference)
The university's Microsoft tenant decides what is possible. **Check first** (checks 3 to 6): can the owner's own
app registration get user consent for `Mail.Read Calendars.Read offline_access`; can a calendar be published as ICS; can mail be forwarded outside.
- **Route A, Graph (if consent works).** Device-code sign-in started from `/school/settings` (the owner enters the code on their phone; Orbit never sees
  the password or 2FA). The refresh token is stored encrypted with `ORBIT_SECRET_KEY` in a `MsAccount` row (it rotates, so it cannot live in `.env`),
  never logged, never in exports. Poll every 30 min with delta queries: mail in the folders the owner picks (default Inbox only), calendar events of the
  next 60 days. Teams needs no Teams API in this phase: Teams assignment, post and missed-activity notifications arrive as mail, and Teams meetings are
  calendar events. A failed refresh sends one push per day, like the SIS feed.
- **Route B, no consent.** Calendar: a published-calendar ICS URL in `.env` (`OUTLOOK_ICS_URL`), read like the SIS feed. Mail: an iOS share-sheet
  Shortcut "Send to Orbit" in the Outlook app posts one mail (sender, subject, date, body) to `POST /api/v1/school/mail`; or a forwarding rule to a
  mailbox Orbit reads, if the tenant allows it.
- **What happens to a mail.** Stored as `SchoolMail` (message id unique, received, sender text, subject, body text, course link). Course link without
  Claude where possible: `X-Owl-Course` header, a SIS ticket id or course code in the subject. Then the mail goes to Claude (capture pipeline) and may
  produce `school_deadline` and `capture` proposals (L2). **All school Inbox mail** goes to Claude (the owner's choice, question 15) except senders on a "never send to Claude" list, which are stored only. Attachments are not read.
- **Calendar events** become `SchoolEvent(source = outlook, kind = event)`, upsert by event id; Teams meetings keep their join URL.
- Never: sending mail, replying, marking read, moving mail, accepting invites, reading Teams channels (`ChannelMessage.Read.All` needs admin consent).

#### Phase 18e (later, not specified here)
A study-planning agent (workload against goals, exam prep blocks as proposals, never writing to the calendar); person links for teachers; SIS4/Moodle data if a source is found (not mapped: not mapped); xCal feed for structured teacher and room data if the text parsing proves too fragile.

**Answers (the owner, 2026-10-06):** 1-11 as recommended. 12: ReCodEx feed and read-only API in Phase 18. 13: **Owl through a copied session cookie**,
not the browser helper (so Owl moves into Phase 18). 14: Claude reads changed course pages and proposes deadlines. 15: Graph first, fallback after the
checks; **all school Inbox mail** may go to Claude, minus a blocklist. 16: not asked yet (default: Teams only through mail and calendar; confirm at 18d).

**Open questions** (recommendation first; the build session asks the owner and records the answers here, as for Phase 17):
1. **Scope of Phase 18.** Feeds and public pages only, session data later (18b)? *Recommended: yes.* Alternative: start with the browser helper.
2. **Which feed.** (Update 2026-10-06: the owner gave the `roz_externi` timetable key first. Phase 18 reads lessons from `roz_externi` when `SIS_TIMETABLE_KEY` is set and from `ical.php?typ=rozvrhng` otherwise; exams and SIS events need `SIS_ICAL_TOKEN` either way. Same lesson table, `uid` = ticket + Julian day, so switching sources does not duplicate lessons.) `ical.php` (one token, all six feeds, exams arrive in the same format) or the structured `roz_externi.php?format=xcal` (separate key, teacher and room fields, timetable only)? *Recommended: `ical.php`; xCal only if teacher/room parsing fails.*
3. **Lessons in Orbit's own ICS feed?** *Recommended: no* (subscribe the SIS feed in Apple Calendar directly). **Reversed 2026-10-09** (`DECISIONS.md` #33): the owner is not subscribed to the SIS feed; lessons, exams and travel blocks go into Orbit's feed (§2.37).
4. **Pushes.** Evening "tomorrow's lessons" at 20:00, exam reminders at 7 days and 1 day, deadline reminders at 3 days and 1 day, schedule-change push, feed-problem push, Sunday digest section? *Recommended: as in the table.* Alternative: morning push instead of evening, or no lesson push at all.
5. **Manual deadlines/exams in Phase 18?** *Recommended: yes* (SIS knows no assignments).
6. **Course enrichment from the public course page** (credits, hours, completion)? *Recommended: yes, throttled, 30-day TTL.* Needed for the credits total in the workload.
7. **Teachers as `Person`s?** *Recommended: no* (text only; they are not contacts).
8. **School data to Claude.** Lessons, exams, courses, deadlines go to Claude in full through the snapshot (`DECISIONS.md` #7), no SIS identifiers. *Recommended: yes.*
9. **Nav.** "School" as a top-bar link plus a Home strip (like Goals). *Recommended: yes.*
10. **Faculty.** `SIS_FACULTY_ID` default Uni `10000`. *Recommended: yes.*
11. **Token storage.** Only `.env` (`SIS_ICAL_TOKEN`, `RECODEX_ICAL_TOKEN`, `RECODEX_API_TOKEN`); rotating the token on SIS needs a `.env` edit and restart. *Recommended: yes* (no settings UI for secrets, like the other tokens).
12. **ReCodEx in Phase 18.** Add the ReCodEx iCal feed to Phase 18 (deadlines), and the optional `read-all` API token for points and second deadlines? *Recommended: iCal yes; API token yes but optional* (it must be renewed yearly; without it only first deadlines are known).
13. **Owl through the browser helper (18b)?** *Recommended: yes* (no student API exists). Alternative: copy the `owl_session` cookie into `.env` (rejected by default: it is a login session for the whole account and its lifetime is unverified).
14. **Course websites (18c).** Watch the SIS `Další informace` URL of every course automatically plus manual URLs, daily, and let Claude read the changed text for deadlines (L2)? *Recommended: yes.* Alternative: change push only, no Claude.
15. **Microsoft 365 route (18d).** Try Graph with the owner's own app registration first, fall back to published ICS + share-sheet Shortcut? *Recommended: yes, after the consent check.* Which mail goes to Claude: all school Inbox mail except a blocklist, or only mail matched to a course? *Recommended: only mail matched to a course or from an school-domain sender.*
16. **Teams.** Cover Teams only through its notification mails and calendar meetings (no Teams API)? *Recommended: yes* (channel posts need admin consent).

### 2.21 Schedule settings and Run now (Phase 19)
Accepted 2026-10-07, all eight open questions answered "as recommended" (`DECISIONS.md` #15). Built.

Before this, every schedule value was an env setting or a constant in a service file (`REMINDER_TIME`, `DIGEST_TIME`, `REVIEW_FROM`, …). Changing one needed a `.env` edit and a restart. Now `/settings/schedule` edits them, and **Run now** runs a job at once.

**Layering.** A value is read as: stored override (DB) > env > code default. No stored override means nothing changes for an existing install. Reset removes the override. Secrets (tokens, passwords, IBAN) are never editable here (same rule as §2.20 question 11).

**Storage.** `AppSetting(key pk, value_json, updated_at)`. Every editable value is a `Settings` field (the constants became env-capable fields with the old value as default: `REMINDER_TIME`, `DIGEST_WEEKDAY`, `SCHOOL_EXAM_DAYS`, …, all in `.env.example`). `OverrideStore` lays the stored overrides over `Settings` (`apply`); the API and web dependency `SettingsDep` and the scheduler's `JobContext.settings()` both return that effective copy, so no service changed its way of reading settings. The store re-reads the table every 5 s (and at once after a save), so a second process sees an edit. A stored value that no longer validates is ignored (logged), never fatal. The services that used constants take them as keyword parameters whose defaults are the old constants (`send_reminders(reminder_time=…, close_days_ahead=…, digest_weekday=…, digest_time=…)`, `send_school_pushes(reminder_time=…, exam_days=…, deadline_days=…, change_gap=…, token_warn_days=…)`, `review.due_plan_start(…, ReviewTiming)`, `prune_backups(keep_days=…)`); the delivery policy (retry delay, max attempts, max age) travels on `QuietHours`, which every sender already receives.

**Applying a change.** Saved values apply live: a save moves the affected interval jobs to their new interval at once (`sync_intervals`), and every job tick repeats that check, so an edit saved by another process also takes effect. Wall-clock rules read the new value on their next minute tick. No restart; nothing needs one. `WEEKLY_REVIEW_ENABLED` and `SCHOOL_PUSHES_ENABLED` are checked at run time, so their jobs are always registered (the review job only when Orbit itself can make the review).

**Editable settings** (group, key, default). The weekday and time rules are `DIGEST_WEEKDAY` / `DIGEST_TIME`, `WEEKLY_REVIEW_WEEKDAY` (Friday to Sunday) / `WEEKLY_REVIEW_TIME` / `WEEKLY_REVIEW_CATCH_UP_WEEKDAY` (Monday to Thursday), and so on; the table in `orbit/services/schedule_config.py` (`DEFINITIONS`) is the list the page shows:
- *Calendar*: `CALDAV_POLL_MINUTES` 10; `BRIEF_MINUTES_BEFORE` 30; `CAPTURE_MINUTES_AFTER` 15.
- *Reminders*: `REMINDER_TIME` 09:00 (birthdays, lent items, promises; constant); `CLOSE_DAYS_AHEAD` 5 (constant); `ITEM_REMINDER_DAYS` 21; digest weekday Sunday and `DIGEST_TIME` 18:00 (constants).
- *Weekly review*: `WEEKLY_REVIEW_ENABLED`; `REVIEW_FROM` 19:00 Sunday; `CATCH_UP_WEEKDAY` Wednesday (constants).
- *School*: `SIS_POLL_MINUTES` 60 (min 15); `SCHOOL_EVENING_TIME` 20:00; `SCHOOL_PUSHES_ENABLED`; exam and deadline reminder time 18:00, `EXAM_DAYS` (7, 1), `DEADLINE_DAYS` (3, 1), `CHANGE_GAP` 1 h, `TOKEN_WARN_DAYS` 14 (constants).
- *Notifications*: `QUIET_HOURS` 22:00 to 08:00; retry delay 5 min, max attempts 5, drop after 24 h (constants).
- *Housekeeping*: `CLAUDE_JOB_RETENTION_DAYS` 7; contact backup retention 90 days (constant); prune intervals 24 h and 1 h (constants).
Not editable: the 60 s, 30 s and 300 s poll ticks (they are checkers, not user-facing schedules), Claude job lease and wait times (engineering values, already settings or constants with no user meaning).

**Read-only rows (outside the app process).** Shown with their current value where Orbit can know it, with a "change in `.env` / systemd" hint: `BACKUP_TIME` 03:30 and backup retention (separate `backup` container), `orbit-review.timer` hourly at :05, the Claude worker loop. Orbit cannot change these from the web process.

**Run now** (button per job; each runs the job function once, in the request, and shows the result line):
- `sync_calendar`: pull the calendar now.
- `sis_sync`: exists as "Sync now" (§2.20); this page links to the same function.
- `flush_notifications`: send held pushes now (ignores `not_before` but not quiet hours).
- `reminder_pushes`, `meeting_pushes`, `school_pushes`: run the check now. They only record what is already due; dedupe keys still hold, so nothing is sent twice. They do not force an early digest or reminder.
- `weekly_review`: start this week's review now (same as `?force=1`, §2.18).
- `prune_contact_backups`, `prune_claude_jobs`: run now.
Not given a button: `flush_usage` (statistics), `late_claude_answers`, `claude_worker_alerts` (event reactions).

**Job status.** Each row shows next run, last run time and result (ok, or failed with the error line, and "by hand" for Run now). Kept in `JobRun(job_id pk, last_started_at, last_finished_at, ok, message, manual)`, one row per job, written by `jobs.scheduler.execute` (scheduled ticks and Run now alike). A job never runs twice at once (a per-job lock: a Run now during a tick says "Already running"). A job that raises is logged and recorded as failed; it never stops the scheduler. A failure to record the status never fails the job.

**Run now details.** Every button calls `jobs.scheduler.execute(manual=True)`. `flush_notifications` also takes pushes waiting for a retry (quiet hours still hold, as decided). `weekly_review` makes next week's review whatever the day, like `?force=1`, only when Orbit itself has the Claude API client; with the Claude worker the worker takes the review itself when due, and the page says so (no button). A job whose source is not set up (no calendar, no school source) shows why and has no button.

**Autonomy and proposals.** Level L0: the owner edits their own configuration directly. Claude never changes a setting, so no proposal kind. Changes are logged (key, old, new, time) in `AppSettingLog` and listed on the page; Reset restores the env value. A value typed equal to the env value removes the override.

**Context provider.** None: schedule settings carry no domain facts. The page is operations, like `/claude`.

**Web.** `/settings/schedule` (nav link "Schedule"): the jobs with last and next run and Run now, then one form per group with a time, number, weekday, checkbox or text field per row, an "Edited. From `.env`: X" hint and Reset per row, Save per group, the outside-the-app rows, and the recent changes. Server-rendered, plain forms, works without JS.

**API.** `GET /api/v1/settings/schedule`, `PATCH /api/v1/settings/schedule` (`{"values": {key: value}}`, all or nothing, 422 on a bad value, 404 on an unknown key), `DELETE /api/v1/settings/schedule/{key}` (reset), `GET /api/v1/jobs`, `POST /api/v1/jobs/{job_id}/run` (404 for a job without Run now).

**Tests.** `tests/test_schedule_config.py` (parsing, bounds, layering, all-or-nothing, log, reset, store cache, missing table), `tests/test_schedule_params.py` (each former constant as a parameter), `tests/test_schedule_jobs.py` (registry, `execute`, locks, failures, intervals, Run now of flush and review), `tests/test_schedule_endpoints.py` (API and page).

**Open questions as asked** (the owner accepted every recommendation, 2026-10-07):
1. **Scope.** All editable rows above, or only the ones already env settings (poll minutes, quiet hours, brief and capture offsets, school evening time, retention)? *Recommended: all, so the hardcoded 09:00, 18:00, Sunday and 19:00 become editable too.*
2. **Storage.** DB overrides (live edit, no restart) or write back to `.env`? *Recommended: DB. Writing `.env` from the web app is risky and needs a restart.*
3. **Run now for the check jobs** (`reminder_pushes`, `school_pushes`, `meeting_pushes`): re-check only, or also a "send digest now" / "send tomorrow's lessons now" that sends a push early? *Recommended: re-check only in this phase; a forced early push is a separate button later.*
4. **Run now and quiet hours.** Does Run now bypass quiet hours for a push it creates? *Recommended: no.*
5. **Job status table** (`JobRun`): keep (next/last run and errors visible) or skip (Run now only)? *Recommended: keep.*
6. **Backup and systemd rows.** Read-only info rows, or leave them out? *Recommended: read-only rows.* A real "Run backup now" would need the app to reach the backup container; *recommended: not in this phase.*
7. **Where.** New `/settings/schedule` page linked from the nav, or a section on the existing ops page (`/claude`)? *Recommended: new page.*
8. **Per-weekday digest and review.** Edit the weekday too, or only the time? *Recommended: weekday and time.*

Files: `orbit/domain/app_settings.py`, `orbit/services/schedule_config.py`, `orbit/services/job_runs.py`, `orbit/jobs/scheduler.py` (`JOBS` registry, `execute`, `sync_intervals`), `orbit/api/schedule.py`, `orbit/web/schedule.py` + `templates/ops/schedule.html`, migration `0018`.

### 2.22 Custom review report for a date range (Phase 20)
Accepted 2026-10-07, all nine open questions answered "as recommended" (`DECISIONS.md` #16). Built.

The weekly review (§2.17) is fixed: the week I lived and the week ahead, on a schedule. The owner wants a **report they ask for, over a date range they set**: "how did October go", "this semester so far", "the two weeks before the exam".

**What it is.** An on-demand agent in the sense of `VISION.md §4`, a sibling of the weekly review, not a replacement. Input: a start date, an end date and an optional **focus** (free text, and optionally one goal, person or course). It builds the shared context for that window through `build_context` (§2.15, the same providers and masking as the weekly review), asks Claude through `integrations/claude.py`, validates the structured answer and stores a **read-only report (L1)**. It writes no proposals and touches no domain table. Autonomy L0/L1; raising it needs a `DECISIONS.md` entry.

**Not the same row as `WeeklyReview`.** `WeeklyReview` is unique per planned week and has a reviewed week plus a planned week. A report has one window and no schedule, so it is its own table, `ReviewReport`, and its own output model. The weekly review and its job do not change.

**Request.** `/reports/new`: two date fields, presets (last 7 days, last 30 days, this month so far, last month, this semester so far; "since a date" is just a start date with the end left at today), a focus box with one optional goal, person and course, **Make report**. Rules: end not before start, end at most 30 days after today, at most 92 days long (`context.windows.MAX_WINDOW_DAYS`, not a setting). One report runs at a time; a second request while one is running is refused with a message, so a heavy range cannot be started twice.

**What Claude is given** (masked as in §2.14; sent in full per `DECISIONS.md` #7): one context snapshot for the window (goals first, then people, ledger, promises, meetings, school), at `CONTEXT_BUDGET_CHARS`; the focus text and ids; today's date; the summary of the latest weekly review that overlaps the window, if any. Provider limits that matter for a past window: notes, interactions and meetings are filtered by the window; **debts, promises and goals are current state**, not history, so the report says "open now", not "open then". The page states this under the summary.

**Output** (`ReportOutput`, Pydantic, English, Slovak or Czech content quoted as it is): `summary` (headline of the range, 3 to 5 sentences), `highlights` (what happened, each `{text, goal_ids[], person_ids[]}`), `slips` (what was promised or planned and did not happen), `patterns` (what repeats: people not seen, goals untouched, deadlines bunched), `numbers` (a few facts the providers hold: meetings, notes, promises kept and open, lessons, exams; computed by Orbit, not by Claude, then shown beside its text), `questions` (up to 3 things worth deciding next, no commands). Every id must exist in the snapshot or it is dropped, as in §2.17.

**Page.** `/reports` lists past reports (range, focus, status); `/reports/{id}` shows the sections above, the range and focus, a footer with model, tokens and "What Claude saw" (stored input, like §2.17), and a **Print / save as PDF** view (print stylesheet) and **Copy as Markdown**. A failed report shows the error and **Try again**. A report can be deleted (it holds a copy of personal context).

**Claude path.** Same rule as the weekly review (`CLAUDE.md` rule 5): the API client when `ANTHROPIC_API_KEY` is set, else the Claude worker as a job kind `report` (§2.19), background priority. **No-key, no-worker fallback:** Orbit still makes the `numbers` section and a plain list of the window's meetings, notes, promises and exams (no Claude text), so the page is never empty. It says that Claude was not used.

**Failure and idempotency.** The row is inserted `pending` before Claude is asked. A pending row older than 15 minutes (a stopped server) can be retried. Up to 2 attempts, then `failed` with the error and Try again. The same range and focus asked again makes a new report; there is no cache and no dedupe, because the data changes.

**Schedule and Run now.** None. A report is only made when asked. (If the owner wants a recurring custom range, e.g. "every month-end", that is a later `SPEC` change and a setting on `/settings/schedule`, §2.21.)

**Data model** (planned, §3 on acceptance): `ReviewReport(id, range_start, range_end, focus_text?, focus_goal_id?, focus_person_id?, focus_course_id?, status[pending|ready|failed], output_json?, numbers_json?, input_json?, input_chars?, model?, input_tokens, output_tokens, error?, attempts, claude_used, created_at, updated_at)`. **API** (planned): `POST /api/v1/reports` (`{start, end, focus?}`, 202 with the report id), `GET /api/v1/reports`, `GET /api/v1/reports/{id}`, `GET /api/v1/reports/{id}/input`, `DELETE /api/v1/reports/{id}`.

**Tests (planned).** Range rules; window building for a past range; masking; id validation and drop rules; numbers computed from fixtures; the no-Claude fallback; the worker job kind with a fake worker; one-at-a-time; pending-row retry; delete; web and API.

**Open questions as asked** (the owner accepted every recommendation, 2026-10-07):
1. **Past only, or also the future?** *Recommended: any range, past or future up to 30 days ahead, so "the next two weeks" works; the wording of the report follows (what happened vs what is coming).* Alternative: past only.
2. **Proposals.** Should a report create Inbox proposals like the weekly review? *Recommended: no in this phase (read-only L1); the weekly review stays the one that proposes.*
3. **Focus.** Free text plus one optional goal, person or course, or free text only? *Recommended: free text plus one optional link.*
4. **Longest range.** 92 days (about a semester), or longer with a coarser snapshot? *Recommended: 92 days; longer ranges would hit the context budget and say little.*
5. **Output sections.** The six above (summary, highlights, slips, patterns, numbers, questions), or fewer? *Recommended: as listed.*
6. **Fallback without Claude.** Plain facts and numbers (recommended), or refuse without Claude as the weekly review does?
7. **Export.** Print view and Copy as Markdown only (*recommended*), or also a file download (PDF/Markdown) and a push when ready? *Recommended: a push when ready, since a heavy range can take minutes; no file download.*
8. **Retention.** Keep reports until deleted (*recommended*), or clear them after N days like Claude jobs?
9. **Phase.** After the first two real weeks of Phase 19 and 18c, or now? *Recommended: now is fine; it touches no module data and reuses `build_context`.*

**Built as** `orbit/domain/report.py` (`ReviewReport`), `orbit/services/reports.py` (range rules, `create`, `prepare`, `numbers`, `plain_sections`, `sanitize`, `finish`, `generate`, `reopen`, Markdown, push), `orbit/services/report_run.py` (`start` and `retry` pick the path; `run_api` runs after the request), `integrations/claude.py` (`ReportOutput`, `ReportRequest`, `ReportWriter`, `ClaudeClient.make_report`), `orbit/services/claude_jobs.py` (kind `report`, `enqueue_report`), `orbit/api/reports.py`, `orbit/web/reports.py` + `templates/reports/`, migration `0019`, `scripts/claude_worker.py` (a report gets the long timeout). Details the text above leaves out:
- **Paths.** API client: the row is `pending`, the request returns (202 on the API, a redirect on the page that refreshes every 5 s), `run_api` makes it in a background task and pushes. Worker: the stored input is built at once and a `report` job is queued (`report_id` on `ClaudeJob`, wait 2 h, lease 30 min, 2 tries by the job rules); `submit` finishes the report and pushes, a job that ends failed or expired fails the report, a rate limit keeps it waiting. No Claude, or a worker that is away or logged out: a plain report at once with the reason on the page. A report job is never rerun from the queue (its own page has Try again).
- **Numbers** are counted from the validated, masked snapshot (not by extra queries): meetings, notes written, interactions, people contacted, active goals, open and overdue promises and closed ones (last 30 days), and, when school data exists, lessons with hours, exams and deadlines in the range. A "Cut to fit" row appears when the size budget trimmed the snapshot.
- **Plain report** sections (meetings, notes, exams and deadlines, open promises now, goals now) are rendered from the stored snapshot, so nothing extra is stored.
- A **person** focus narrows the people and meetings provider (`ContextFocus`); a goal or course focus is named in the prompt. The latest ready weekly review that overlaps the range is given to Claude as memory.
- A report is never cached or deduplicated, makes no proposals, and "Delete report" removes it with the context it kept. Reports are kept until deleted.
- `GET /api/v1/reports/{id}/input` returns the stored input. Print uses a print stylesheet; Copy as Markdown is a text box with Copy and Share.

### 2.23 Appointments from capture (Phase 21, built)
**Problem.** "I have a haircut on Friday at 15:00" is a future event, not a fact about the barber. Today capture (§2.5) has no item kind for it, so it
files the sentence as a `note` on the barber's person page. The note is wrong (nothing happened yet), is never seen again at the right time, and puts
nothing in the calendar.

**Proposal.** A fifth capture item kind, `appointment`, next to note, interaction, debt and promise. Same flow: Claude proposes, the Inbox card is edited, confirm writes.
- `AppointmentDraft` (in the `capture` payload): `person_ref?` (the barber, doctor, dentist; optional, existing id or `new:N`), `title`, `start` (local Prague date and time, as
  Claude read it; required), `end?` (default start + 1 h), `location?`, `included`. Claude gets today's date and weekday so "Friday" and "next week" resolve. No date or time found ->
  no appointment draft; the text stays a note draft as today. Past dates are rejected by validation (the card asks for a date).
- Confirm creates a `Meeting` row with `calendar_name = "orbit"` and `calendar_uid = "orbit-appt:<uuid>"`, linked to the person (`MeetingPerson.link_source = manual`).
  Reusing `Meeting` gives the pre-meeting brief push (§2.8), the post-meeting capture prompt (§2.9) and the Meetings page with no new module. The CalDAV sync must not mark
  these rows `cancelled` (it only touches rows whose `calendar_name` it polls).
- ICS feed (§2.2): Orbit appointments appear as timed events (🗓 title), stable UID, so Apple Calendar shows them. Cancelling or editing on the Meetings page updates the feed.
- Rule 1 holds: nothing is created until the owner confirms the card. Undo = cancel the meeting.
- No-key fallback (§2.18): a keyword fallback is not attempted; the text stays a note draft as today. Appointments need Claude, like debts and promises.

**Prompt change (independent of the new kind).** Tell the extractor: a plan, appointment or intention in the future is never a `note` or an `interaction`.
Notes record what is known; interactions record what happened.

**Decided 2026-10-07 (all answers as recommended; `DECISIONS.md` #19).** Questions and answers:
1. **Reuse `Meeting`, or a new `Appointment` model?** *Recommended: reuse `Meeting`* (briefs, capture prompt and the page come free). Cost: one `source` marker (`calendar_name = "orbit"`),
   and a user-made event can be edited or cancelled in Orbit but is not written back to iCloud.
2. **Write to iCloud Calendar too (CalDAV PUT)?** *Recommended: no*; the ICS feed is enough and writing to the calendar is a new outward-facing action (`DECISIONS.md` #1 style).
   Alternative: write after confirm, behind a setting.
3. **Reminder.** Pre-meeting brief only (default 30 min before, §2.8), or also a day-before push for appointments? *Recommended: brief only; add the day-before push if missed.*
4. **Person optional?** *Recommended: yes*; "dentist Monday 10:00" without a contact still makes an appointment (no brief, still in the feed).
5. **Post-meeting capture prompt** after a haircut ("Anything to note?"): keep (free with `Meeting`), or skip for appointments? *Recommended: skip for appointments with no person link; keep otherwise.*
6. **Duplicates.** The same appointment dictated twice: dedupe by person + start (within the capture duplicate window, §2.5)? *Recommended: yes, same as the recent-duplicate check.*

**Built as** `AppointmentDraft` and `CapturePayload.appointments` in `services/capture.py`; `ExtractedAppointment` and the prompt rule in `integrations/claude.py`
(`CaptureExtraction.appointments` defaults to empty, so older answers still validate); `meetings.create_appointment` / `appointment_exists` / `ORBIT_CALENDAR`;
`ics._appointment`; the Inbox card "🗓 Appointments" and "+ Appointment" in `web/inbox.py` and `inbox/detail.html`.
- `start` and `end` are local Prague wall time (no zone) in the payload and become UTC on confirm (`CLAUDE.md` rule 3). Claude answers `YYYY-MM-DDTHH:MM`; a value that does not parse drops the draft, so the text stays a note.
- Confirm refuses a start before today and an end not after the start (422 / flash, nothing written). A dictated appointment on today's date later in the day is fine.
- Duplicate check on confirm: an Orbit meeting, not cancelled, with the same start and the same person (or, with no person, the same title) is not made again.
- The CalDAV sync never cancels `ORBIT_CALENDAR` rows, even if a polled calendar has that name.
- The pre-meeting brief and post-meeting capture prompt need a linked person (§2.8, §2.9), so an appointment with nobody linked gets neither. The ICS UID is `appointment-<meeting id>@orbit`; a cancelled meeting leaves the feed.
- **Cancel:** the meeting page shows "Cancel appointment" for these rows (`POST /meetings/{id}/cancel`, web only); calendar events are cancelled in the calendar.
- No new table and no migration.

### 2.24 Navigation: a "More" menu (built)
**Problem (audit 2026-10-07).** The main navigation is five tabs (Home, Inbox, People, Meetings, Ledger) plus `.topbar-extra`: Import, Goals, School, Review,
Reports, Schedule, Context, Claude (worker backend only) and Log out. The extra links show on every screen width. At 375 px the page is 621 px wide, the links from
Reports onward are off-screen, and in Chromium mobile emulation the fixed bottom tab bar fell below the visible screen. At 1024 px the bar is 1145 px wide and the
Inbox badge wraps onto two lines.

**Proposal.** Keep the five-tab bar. Move every other link into one **More** entry. No new JS library.
- **Phone (under 768 px):** the tab bar gets a 6th tab, "More". It opens `/more`, a plain page listing the links as large rows (Import, Goals, School, Review, Reports,
  Schedule, Context, Claude if the worker backend is on, Log out). It works without JS and keeps the back button. `.topbar-extra` is hidden on phones.
- **Desktop (768 px and up):** the same links in a "More" dropdown (`<details class="more-menu">`, closes on outside click through the small existing `app.js`).
  The top bar then holds the brand, five tabs, More. The Inbox badge stays on one line (`white-space: nowrap`).
- One list of links in one place (a Jinja macro or a `MORE_LINKS` constant) feeds both, so they cannot drift.
- The current page marks "More" as current when it is one of the extra pages.
- Done when: `document.documentElement.scrollWidth <= innerWidth` at 320, 375, 768 and 1024 px; the tab bar is visible on a phone; every link is reachable; a template test
  asserts every extra link appears on `/more` and in the dropdown.

**Decided 2026-10-08: all answers as recommended** (`DECISIONS.md` #20). Questions and answers:
1. **Do School or Goals earn a tab?** School is used daily during the semester. *Recommended: replace nothing; School and Goals are the first two rows of More* (one extra tap).
   Alternative: School replaces Ledger on the tab bar during the semester; Ledger moves to More. Alternative: a 6th and 7th tab, too tight at 375 px.
2. **Order inside More.** *Recommended: School, Goals, Review, Reports, Import, Schedule, Context, Claude, Log out* (used most to least). Say if you want another order.
3. **`/more` page or a bottom sheet on phones?** *Recommended: the `/more` page* (no JS, back button works, easy to test). Alternative: a sheet that slides up.
4. **Log out** inside More, or stay visible in the top bar on desktop? *Recommended: inside More* (it is rarely pressed).
5. **Badges in More.** Should More show a dot when something inside needs you (an overdue school deadline, a failed Claude job)? *Recommended: no for now*; Home already shows attention items.

**Built as** `MORE_LINKS` and `more_links(request)` in `web/templating.py` (one list; Claude only with the worker backend), `GET /more` in `web/pages.py`
with `templates/more.html`, the `<details class="more-menu">` and the 6th `tab-more` in `base.html`, CSS in `app.css`, and the outside-click close in `app.js`.
Checked in the browser at 320, 375, 768 and 1024 px: no horizontal scroll and the tab bar sits at the bottom. `tests/test_web_nav.py`.

### 2.25 Home and Ledger page length (built)
**Problem (audit 2026-10-07, synthetic data: 400 people, 800 debts, 400 promises).** Home "Needs attention" has no cap on promises and lent items (only keep-in-touch
is capped, `HOME_CATCH_UPS = 5`): 188 rows, about 18,600 px, and "Meetings today" and the school strip sit below it. The Ledger renders every open debt and every open
promise: 801 forms, 846 KB, 70,000 px at 1024 px. Real data is smaller today, but both pages grow without a limit.

**Proposal: Home.** New order: Capture, search, **Meetings today**, **School** (next lesson and deadline), Inbox, Weekly review, **Needs attention** capped, Goals,
Money, Birthdays and name days, calendar feed.
- Needs attention shows at most `HOME_ATTENTION = 5` rows, ordered: promises due today or overdue (oldest first), lent items to ask back, then catch-ups. Under the list: "…and N more" linking to
  `/attention`, a new page with the full list in the same three groups (no cap). The catch-up cap of 5 inside Home goes away; the 5 total covers all three kinds.
- Done when: Home is under about 3 screens (about 2,500 px at 375 px) with the audit data.

**Proposal: Ledger.** Cap every list at `LEDGER_PAGE = 20` rows with a "Show all N" link (`/ledger?all=<section>`, one section expanded, plain GET, no JS).
- Sections: Who owes me, Whom I owe, Lent out, Borrowed, Favors owed to me, Favors I owe, Open promises. Each shows its total count in the heading.
- Money sections sort by amount, largest first. Items, favors and promises sort oldest first (the longest overdue is what you chase).
- Totals per currency at the top stay exact (they are computed from all rows, not the visible 20).
- The settle and edit forms render only for visible rows, which is what shrinks the HTML.
- Done when: the Ledger HTML is under 150 KB with the audit data, and every row is reachable through "Show all".

**Decided 2026-10-08: all answers as recommended** (`DECISIONS.md` #21). Questions and answers:
1. **Home cap.** *Recommended: 5 rows in total* across promises, items and catch-ups, most urgent first. Alternative: 5 per kind (up to 15 rows).
2. **Is Meetings today above School?** *Recommended: Meetings, then School.* Alternative: School first during the semester.
3. **Where does the full Needs-attention list live?** *Recommended: a new `/attention` page.* Alternative: expand in place with `/?all=1` (no new page).
4. **Ledger cap.** *Recommended: 20 per section.* Say a different number if 20 feels long on a phone.
5. **Collapse empty or settled sections?** The Ledger shows no settled debts today. *Recommended: nothing extra*; an empty section keeps its one-line "Nothing …" text.
6. **Money sections: per person or per debt?** They list one row per person (summed per currency). *Recommended: unchanged*; the cap counts people.

**Built as** `HOME_ATTENTION`, `_attention`, `_cap` and `GET /attention` in `web/pages.py`; the shared `_attention.html` macro, `attention.html` and the reordered `home.html`;
`LEDGER_PAGE`, `Section`, `_section` and the `all` query parameter in `web/ledger.py`; `ledger.html` shows each section's count and "Show all N". `HOME_CATCH_UPS` is gone.
- The Ledger now asks for every open debt and promise (`ALL_ROWS`); the service default of 500 would have silently hidden rows from "Show all".
- The page cap counts rows, and money sections count people. Anchors (`#lent`, `#owed_to_me`, ...) keep the view at the section after "Show all".
- Tests: `tests/test_web_page_length.py` (cap, "…and N more", `/attention`, per-section "Show all", exact totals, Ledger HTML under 150 KB with 450 rows).

### 2.26 Inbox cards and capture feedback (built)
**Problem (audit 2026-10-07).** (a) A `new_person` card shows only the name and Claude's short context, so the owner cannot tell what was dictated about them. The text lives only in the
sibling `capture` proposal, and that sibling may not exist: a `new_person` proposal is made only for people no capture item refers to, so a dictation that yields nothing but new people
has no `capture` card at all. (b) One dictation that names five unknown people makes five cards; rejecting a wrong batch is five taps. (c) The capture form only dims its button while it
waits, which takes up to 45 s on the worker backend (`claude_wait_interactive_seconds`); it looks frozen.

**Proposal.**
- **(a) Show the dictated text on `new_person` cards.** Add `source_text: str | None` to `NewPersonPayload` (the dictation, as typed, up to 2,000 characters) and show it as the same quote line
  the capture card uses (`“…”`). Rows made before this change have no text and look as they do today. Nothing new is sent to Claude; the text is the user's own, already stored in the
  `capture` payload, now copied onto the card it explains.
- **(b) "Reject all from this capture".** Add `group: str | None` (a random token made once per dictation) to `CapturePayload` and `NewPersonPayload`. A pending `new_person` card whose group has
  two or more pending cards shows a second button, "Reject all N from this capture", that rejects every pending proposal in the group at once. Reject is already reversible (Recently rejected →
  Reopen), so no confirm dialog. It never confirms anything (rule 1).
- **(c) A visible waiting state.** Every capture form (Home, Inbox, meeting page) gets `data-busy="Sending to Claude…"` on its submit button. The existing submit handler in `app.js` swaps the button text to that
  label and `.is-submitting` adds a small spinner (CSS only). No library. If the worker is away the answer is immediate and the label is never seen. C8 (capture answers at once) later removes most of the wait.
- No migration: both payloads are JSON; new fields default to `None`, so older rows still validate.

**Decided 2026-10-08: all answers as recommended** (`DECISIONS.md` #22). Questions and answers:
1. **Reject-all scope.** *Recommended: all pending proposals of the same dictation, `capture` card included.* Alternative: only the `new_person` cards (the `capture` card stays).
2. **When does the button show?** *Recommended: only with two or more pending cards in the group.* Alternative: always.
3. **Text on the card.** *Recommended: the full dictation, shortened to about 200 characters with "…" on the Inbox list, in full on the card page.* Alternative: only the sentence that mentions the person (needs Claude to return it: a prompt change).
4. **Waiting label.** *Recommended: "Sending to Claude…" on the button and a spinner.* Alternative: a full-width banner under the form after 3 s.
5. **Old cards without text.** *Recommended: leave them as they are.* Alternative: look the text up from a nearby `capture` card of the same minute (guessy).

**Built as** `CapturePayload.group`, `NewPersonPayload.source_text` / `group` (`SOURCE_TEXT_MAX = 2000`) and `capture.pending_in_group` in `services/capture.py` (the token is a UUID made once per `capture()` call);
`inbox.reject_group` and `POST /inbox/{id}/reject-group` (web only); `Card.group_pending`, the quote and the button in `inbox/_macros.html` (`full=true` on the card page, 200 characters on the list);
`data-busy` on the three capture forms, the label swap in `app.js` and the spinner in `app.css`.
- The button shows on a `new_person` card when two or more cards of the dictation are pending; it rejects all of them, the `capture` card included. Flash: "Rejected N from this capture."
- Editing a capture card keeps `group` (the edit starts from the stored payload). No migration; the API `/api/v1/capture` makes the same groups.
- Tests: `tests/test_capture.py` (group, old payload validates), `tests/test_web_inbox.py` (quote and truncation, reject all, single card, old card, waiting label, edit keeps group). The label swap was checked in the browser.

### 2.27 Capture can update existing items (built)
**Problem.** Capture (§2.5) only creates: notes, interactions, debts, promises, appointments. "Paťo mi vrátil 12 €", "Terka mi vrátila powerbanku" and "poslal som Terke rozvrh" are updates to
things Orbit already holds, but Claude never sees them, so the text becomes a note draft or, worse, a wrong new debt in the opposite direction. The owner then settles by hand on the Ledger.

**Proposal.** Two new item kinds inside the existing `capture` proposal (same card, same Edit page, same Confirm; L2, `VISION.md §3`). Nothing is applied before Confirm (`CLAUDE.md` rule 1: no auto-settle, ever).
- `SettleDraft` (`settle`): `debt` (a debt ref from the list Claude was shown), `amount?` (money only: the part paid, major units; absent = settle in full), `include`. Covers all three debt types: money "paid",
  item "returned", favor "done". The plan's separate `return_item` is the same operation on an item debt (`ledger.settle` already handles it), so it is one kind.
- `CompletePromiseDraft` (`complete_promise`): `promise` (a promise ref), `status` (`done` or `dropped`), `include`.
- Confirm calls `ledger.settle(debt, amount_minor)` and `promises.set_status(promise, ...)`, the same functions as the Ledger buttons, so partial settles make a remainder debt with a new reference code as today.
- Card lines: "Settle: Paťo owes you 12.00 € (pizza) → paid in full", "Part: 5.00 € of Paťo's 12.00 € (pizza); 7.00 € stays open", "Promise done: you promised Terka: poslať rozvrh".
  The Edit page shows each as a row with a checkbox, the debt or promise picker (only that person's open ones) and, for money, the amount.

**What Claude is sent (extends `build_capture_prompt`; `DECISIONS.md` #7 generous context, rule 5 masking).** For the matched candidate people only (never everyone): their open debts
(`debt:<id>`, direction, type, amount and currency, description, reference code, date created, due) and their open promises (`promise:<id>`, direction, text, due), at most 10 of each per person and 40 of each in all,
oldest first. Descriptions and texts go through `mask_sensitive` like the rest of the prompt. No IBANs or account numbers exist on these rows. Prompt rule: settle only when the text clearly says a thing was paid back,
returned, done or sent; the ref must be one listed; never settle something that was not listed; "paid for me" is a new debt, "paid me back" is a settle; say less than you are sure of, the card is editable.

**Validation (`payload_from_extraction`).** A ref not in the list shown drops the draft. A settle whose debt belongs to a person other than a matched candidate drops it. `amount` larger than the debt, zero or on an item/favor drops the amount (the card then
says "in full?" and the amount field is shown). Two settles of one debt in one dictation keep the first.

**On Confirm.** The debt or promise is loaded again: if it was settled or closed since the card was made (by hand, or by a bank `payment_match`), that item is skipped with a line in the result ("Paťo's 12.00 € was already settled"), and the rest of the
capture still applies. A partial amount over what is still open fails that item the same way.

**No-key fallback (§2.18).** None: with no Claude the text stays a note draft, as for debts today. The Edit page's "+ Settle" and "+ Promise done" buttons let the owner add one by hand.

**Decided 2026-10-08: all answers as recommended** (`DECISIONS.md` #23). Questions and answers:
1. **Partial wording.** "Paťo mi vrátil 5 €" when Paťo owes 12 €: *Recommended: a partial settle of that one debt* (7 € stays as the remainder). Alternative: ask (no draft) when the amount is not the full amount.
2. **Amount matches no debt.** "Paťo mi vrátil 20 €" with only a 12 € debt open: *Recommended: no settle draft; a note draft and a warning line "no open debt of 20 € for Paťo"*. Alternative: settle the 12 € and flag 8 € as extra.
3. **Several debts of one person fit** (two 12 € debts): *Recommended: the oldest one* (what has waited longest), shown on the card so it can be changed in Edit. Alternative: no draft.
4. **Bank `payment_match` proposals (§2.7).** If a pending `payment_match` already covers the same debt: *Recommended: still make the settle draft, and on Confirm of the second one skip the already-settled debt* (the check above), no cross-linking. Alternative: tell Claude about pending matches and let it skip.
5. **Promises: done or dropped?** "Poslal som Terke rozvrh" is `done`; "nepošlem to" is `dropped`. *Recommended: both, `done` by default when unsure.*
6. **Direction safety.** "Paťo mi vrátil 12 €" settles a `they_owe_me` debt; "vrátil som Paťovi 12 €" settles an `i_owe_them` debt. *Recommended: Claude names the direction in the draft and Orbit rejects a ref whose direction disagrees.* Alternative: trust the ref.
7. **Scope of this change.** *Recommended: build C2 (better people recall, §Plan) in the same pass,* since settles only work for people the matcher found; C2 adds a fallback set of recent and close people. Alternative: C1 alone first.

**Built as** (`services/capture.py`) `SettleDraft`, `PromiseUpdateDraft`, `CapturePayload.settles` / `promise_updates` / `warnings`, `OpenItems` and `open_items` (what Claude is shown and may settle), `_settle_drafts`, `_promise_update_drafts`,
`recall_people`, and the new branches in `describe_item` and `apply_capture` (`AppliedCapture.settled`, `closed`, `skipped`); (`integrations/claude.py`) `ExtractedSettle`, `ExtractedPromiseUpdate`, `CandidateDebt`, `CandidatePromise`, the prompt rules and the
"open debt / open promise" lines under each person; (`web/inbox.py`, `inbox/detail.html`, `_macros.html`) the "☑️ Settle what exists" section on the Edit page (include box, part-paid field, done/dropped select) and the warning lines on the card.
- **Differences from the proposal.** The Edit page has no "+ Settle" / "+ Promise done" buttons: picking one debt from a list needs a new picker, and the Ledger's Paid and Done buttons already do it by hand. `amount_minor` is kept for a part only; `currency` is always stored on a money settle.
- **C2 (recall).** Claude is offered the text's fuzzy matches first, then people tagged `close`, talked to in the last 14 days (`RECENT_DAYS`) or in a meeting today, up to `MAX_CANDIDATES` (25). Their open debts and promises are listed too (10 each per person, 40 in all, oldest first).
  The note-draft fallback still trusts only the people the text names, so one recalled person is never assumed to be the owner of a note.
- A settle whose amount is more than the debt makes no draft and adds the warning "No open debt of 20.00 € for Paťo.", and the dictation stays as a note draft.
- Tests: `tests/test_capture_updates.py` (settle, part, no match, wrong direction, oldest twin, items, skip when already settled, promises, prompt, caps, masking, recall) and one web test in `tests/test_web_inbox.py`.

### 2.28 Learn from corrections (built)
**Problem.** The owner fixes the same things on capture cards again and again: "Janko" is really Janko Kováč, a debt came out as "they owe me" but it was "I owe them", a currency is wrong. Orbit throws the lesson away: it keeps only the final payload, and
`match_people` does not know that "Janko" means Janko Kováč, so the next dictation proposes a duplicate new person.

**Decided 2026-10-08 (all answers as recommended; `DECISIONS.md` #24).**
- **Keep the original.** `Proposal.original_json` (nullable) holds the payload as Claude proposed it (`capture` and `new_person` rows, set when the proposal is made and when a late answer replaces a draft). `payload_json` stays the edited version; the difference is the lesson.
  Kept as long as the proposal row is kept; rows from before this change have none and teach nothing. Migration `0021`.
- **(a) "Add alias X to Y?"** When a confirmed capture card linked a new name to an existing person (`new_people[i].action = link`), Orbit proposes `add_alias` (`ProposalKind.ADD_ALIAS`, payload `person_id`, `alias`; one tap in the Inbox; L2). Skipped when the person already answers to that name,
  when the same suggestion is pending or was rejected before. Confirming adds the alias (`people.add_alias`), so `match_people` finds the name next time.
- **(b) Corrections as examples in the capture prompt.** Up to 6 lines from confirmed captures of the last 60 days, newest first: an unknown name that was an existing person, a debt whose direction, kind or currency was changed, a promise whose direction was changed.
  Sent under "Corrections the owner made to your earlier answers (apply the same lessons)". Names of people are the ones Claude already sees; text is masked like the rest of the prompt.
- **(c) Rejected items.** Up to 6 summaries of rejected `capture` and `new_person` cards of the last 60 days under "Items they rejected earlier (do not propose the same kind of thing again)", as the weekly review already does for its proposals.
- **What is kept, how long.** `original_json` with the proposal (no expiry; proposals are kept, `DECISIONS.md` rejected-proposals note); the prompt reads only the last 60 days. Nothing leaves the server except those prompt lines.

**Built as** `Proposal.original_json`, `ProposalKind.ADD_ALIAS`, `AddAliasPayload` and `ADD_ALIAS_HANDLER` (`services/people.py`), `capture.propose_aliases` (called from the capture confirm), `services/corrections.py` (`recent_corrections`, `recent_rejections`),
`CaptureRequest.corrections` / `rejected` and their prompt lines, the card label "🏷 Another name". Tests: `tests/test_corrections.py`.

### 2.29 Intake router and school deadlines from capture (built)
**Problem.** The owner shares different things (dictation, a bank CSV, a course PDF or link) through different Shortcuts, and capture knows nothing about school: "zápočet z NINF030 20. októbra" becomes a note.

**Decided 2026-10-08 (all answers as recommended; `DECISIONS.md` #25).**
- **One endpoint, deterministic routing, no Claude in the router.** `POST /api/v1/intake` (bearer; form fields `text`, `url`, `file`) calls `services/intake.decide`. Order: a file beats a link, a link beats text.
  File: starts `%PDF` → school resource; starts `BEGIN:VCARD` → contacts import; a CSV whose header a bank parser knows (`detect_bank`) → bank import; anything else → 422, nothing saved.
  Link (the `url` field, or text that is only a link) → school resource. Everything else is text → capture. The response is `{route, summary, ids, course}`.
- **Course for a PDF or link:** the one current course whose code, or whose name (6+ characters), appears in the filename and text around it (diacritics-insensitive). None or several → "Unsorted" (the School page already lists those), never a guess.
- **Claude only reads plain text, as before.** The routing cases that would need a model (a file that could be several things) are refused instead; revisit if a real case shows up.
- **School deadlines in capture.** `CaptureExtraction.school_deadlines` (course code, title, kind, due `YYYY-MM-DD` or with `THH:MM`). The prompt lists this semester's courses (`CaptureRequest.courses`, up to 40). Each becomes a separate `school_deadline` proposal
  (the existing kind and card; `source_kind = "dictation"`, the dictation as its quote) via `school_reader.propose_dictated_deadline`. A course code that was not listed gives a deadline with no course; a past date, an unparseable date and a deadline that is already known (same course, similar title, within a day) are left out.
- **Tasks without a person: not built.** A `Promise` needs a person (a non-null foreign key used by the Ledger, reminders, the digest and the review). Making it optional touches all of them and "a promise to myself" is a product decision about how those pages look; it needs its own spec. Until then such a dictation stays a note.
- What is sent to Claude: this adds the list of course codes and names to the capture prompt; nothing else. PDF and link text still go only through the school reader (`SPEC §2.20`), which is unchanged.

**Built as** `services/intake.py` (`decide`, `match_course`, `Route`), `api/intake.py`, `capture.school_deadline_proposals`, `school_reader.propose_dictated_deadline`, `ExtractedSchoolDeadline`, `CandidateCourse`, the prompt rule and course list, and `docs/SHORTCUTS.md` (`Orbit Share`).
A late answer (§2.19) makes the same deadline proposals, and removes the note draft when only people or deadlines came of it. Tests: `tests/test_intake.py`.

### 2.30 Smaller review input with signals (built)
**Problem (audit 2026-10-07).** The weekly review sent two full context snapshots, each cut to the 400,000-character budget (about 100,000 tokens), so about 200,000 tokens per review on audit-size data. Goals, people, the Ledger and promises are "state now" and were
sent twice, and the collisions the review exists to find (a meeting over a lesson, three deadlines in two days) sat somewhere inside the JSON for Claude to spot.

**Decided 2026-10-08 (all answers as recommended; `DECISIONS.md` #26).**
- **State once.** `next_week` (the planned week) is the full snapshot and carries everything true now: goals, school, people, ledger, meetings, promises. `last_week` (the reviewed week) is built with `build_context(only=("people", "meetings", "school"))` and
  `ContextFocus(activity_only=True)`: only who he met and what the calendar and timetable held that week. Left-out modules are empty in the stored snapshot (`ContextSnapshot.omitted`) and absent from `prompt_json()`. Promises done and debts settled last week are in `next_week` (they are listed for 30 and 90 days back).
- **Signals.** `services/context/signals.py` `compute_signals` (a `Signals` model, sent as `signals_json`, stored in `input_json["signals"]`): `clashes` (a meeting overlapping a lesson, or an exam or timed deadline), `overdue` (open promises, debts and goals past their date, with days over), `people_past_cadence`, `net_money` (per currency),
  `bunched_deadlines` (3 or more exams, deadlines and promises due within 48 hours), `goals_with_nothing_linked` (no open or recently kept promise and no person). Each signal is a count and its first 8 lines. All text is masked like the rest.
  The prompt tells Claude these are exact: start from them, check them against the snapshot, explain, do not recount.
- **Reports.** A report already sent one snapshot. It now also gets the signals for its range (overdue relative to the range start) and says so in its system prompt.
- **Not changed:** the 400,000-character budget (`DECISIONS.md` #7), the API and worker paths, the proposals a review may make, and how an answer is checked (`known_ids` reads both snapshots).

**Measured** with `scripts/measure_review_prompt.py` (audit-size data: 400 people, 2,400 notes, 1,500 interactions, 800 debts, 400 promises, 1,200 meetings, SQLite): before 799,268 characters (about 200,000 tokens), after 532,330 (about 133,000), a third less.
`last_week` fell from 399,518 to 130,881 characters; `next_week` is still at the budget (399,836) because the people module alone fills it with this much data; the signals are 1,613 characters. A lower review budget would cut more; that is a product choice (`DECISIONS.md` #7) left to the owner.

**Built as** `ContextFocus.activity_only`, `build_context(only=...)`, `ContextSnapshot.omitted` / `prompt_json`, `Signals` and `compute_signals`, `ReviewRequest.signals_json`, `ReportRequest.signals_json`, `review.LAST_WEEK_MODULES`, and the prompt text. Tests: `tests/test_signals.py`.

### 2.31 A model for each kind of work in the API path (built)
**Problem.** `ClaudeClient` (the pay-per-token API path) used one `ANTHROPIC_MODEL` for everything: a one-line meeting brief and a whole week's review ran on the same model at the same settings. The Claude Code worker already picks a model by priority (`WORKER_CLAUDE_MODEL_BACKGROUND`).

**Decided 2026-10-08 (all answers as recommended; `DECISIONS.md` #27).** Three env settings, no model name in code (`CLAUDE.md` rule 5), all optional:
- `ANTHROPIC_MODEL` (existing): the **default** model: `capture`, `draft`, `school_read`.
- `ANTHROPIC_MODEL_FAST`: `meeting_link`, `classification`, `brief` (short, structured, high volume). Empty: the default model. Fits a small model such as Haiku.
- `ANTHROPIC_MODEL_DEEP`: the weekly review (`weekly_review`) and `report`. Empty: the default model. These calls run with **adaptive thinking** (`thinking: {"type": "adaptive"}`) and `output_config.effort` = `ANTHROPIC_DEEP_EFFORT` (`low`, `medium`, `high`, `xhigh`, `max`; default `high`). `off` sends neither, for a model that has no adaptive thinking (Haiku 4.5, older models).
- Structured output stays `messages.parse(output_format=...)` (no forced `tool_choice`, which the newest models reject), and `output_config` is merged with it by the SDK. `budget_tokens` is not used: the current models reject it.
- Usage records (`/ops/claude`, the usage log) name the model that answered. The worker path is unchanged.
- No new data goes to Claude; only which model reads what changes.

**Built as** `FAST_PURPOSES`, `DEEP_PURPOSES`, `ClaudeClient.model_for`, `_thinking_options` and the three constructor arguments in `integrations/claude.py`; the settings and `.env.example` entries; `create_app` passes them. Tests: `tests/test_claude_models.py`.
Open for later: the review and reports on the worker path could use `WORKER_CLAUDE_MODEL_*` the same way; the owner runs the worker in production, so this setting matters only if they return to the API.

### 2.32 Retry with the validation error, and checked flags instead of self-confidence (built)
**Problem.** (a) When Claude's answer failed validation the API path asked the same question again, so it often failed the same way. (b) The Inbox "Claude wasn't sure" warning came from Claude's own `confidence`, which is poorly calibrated: it warned on correct cards and stayed quiet on wrong ones.

**Decided 2026-10-08 (all answers as recommended; `DECISIONS.md` #28).**
- **Retry with the error.** After an answer that fails Pydantic validation, `ClaudeClient._parse_with_usage` asks again with the same prompt plus "Your previous answer was rejected by validation: <field: message; ...> Answer again, fixing exactly these." (first 6 errors; `rejection_note`). The worker script already did this for the worker's 422 (`JobOutputError`, `scripts/claude_worker.py` `process`), so that path is unchanged.
  The SDK's `messages.parse` does not return the failed text, so the note carries the field errors, not the previous answer.
- **Flags computed by Orbit** (`services/capture_flags.py`), per item of a capture card, worked out when the card is drawn (so fixing the item removes its flag): `X could also be Y` (the dictation's words fit two non-archived people: exact or inflected name or alias tokens);
  `New person: N` (an item points at a new person the card will create); `Amount missing`; `Date read from “zajtra”: check it` (a promise due date, appointment or interaction date together with a relative word in the dictation: today, tomorrow, weekdays, "next", Slovak, Czech and English).
- **Where they show.** Under the matching line on the Inbox card (`⚠ ...`) and in a "Check these" section on the card's Edit page. Claude's `confidence` is still stored; the card shows it only as a quiet line when under 60 % ("Claude's own confidence: 40 %"), and the Edit page shows it in its header as before.
- No new data goes to Claude. No migration.

**Built as** `rejection_note` and the changed retry in `integrations/claude.py`; `services/capture_flags.py` (`flags_for`, `item_flags`, `ambiguous_people`, `relative_word`); `Card.flags` in `web/inbox.py`; the card, Edit page and macro changes in `inbox/_macros.html` and `inbox/detail.html`. Tests: `tests/test_capture_flags.py`.

### 2.33 Capture answers at once (built)
**Problem.** The capture request waited for Claude: with the worker up to 45 s (the request thread polled the database every 0.5 s), with the API up to 30 s × 4 tries, while the Shortcut hung. A late answer already replaced the note draft (§2.19), but only as the exception.

**Decided 2026-10-08 (all answers as recommended; `DECISIONS.md` #29).**
- **Always save first, ask afterwards.** `capture(..., defer=True)` (every web, API and intake capture with a Claude client) makes no Claude call. It saves the text as the usual note draft with `CapturePayload.processing = True` and answers in under a second: "Saved; Claude is reading it and you get a push when it is ready." (`items: 1`). Without a Claude client nothing is pending: the note draft as ever (§2.18).
- **Both backends use the late path.** `capture_defer.dispatch`: with the worker, `JobClaudeClient.defer_capture` queues the job without waiting (`enqueue(wait=False)`), and `apply_late` replaces the draft when the answer arrives (§2.19, unchanged); with the API client, a background task (`answer_in_background`, run after the response) asks, then `upgrade_draft`.
  Either way a push "📥 Capture ready" follows (`late_capture` kind, once per capture); it respects quiet hours.
- **Failure and silence.** If Claude fails, declines, answers nothing usable, the job expires or the queue is full, the draft stays the note it is and stops saying "processing" (`capture.give_up`; `apply_late` now also runs `housekeeping` and gives up failed or expired capture jobs). A draft the owner edited meanwhile is left as they wrote it. The card stops saying "reading" after 10 minutes whatever happened (`PROCESSING_SHOWN`).
- **What they see.** The card shows "⏳ Claude is reading this…" and polls itself every 3 s (`hx-trigger="every 3s"` on `GET /inbox/{id}/card`) until the answer is in; no JS library. The Shortcut shows the immediate summary; the push says when the proposals are ready.
- **What changes in the answer.** `POST /api/v1/capture` returns the draft's id and `items: 1`, and the final proposal ids are listed in the Inbox. Callers that expected the extracted items in the response must read the Inbox.
- The first extraction on a restart: if the server stops while Claude is thinking, the draft stays a note (nothing is lost).

**Built as** `capture.PendingCapture`, `CaptureResult.pending`, `capture(defer=...)`, `give_up`, `_stop_processing`; `services/capture_defer.py` (`dispatch`, `answer_in_background`); `JobClaudeClient.defer_capture`; `claude_jobs._give_up_failed_captures` and the `enqueue(wait=False)` deadline fix; `deps.run_capture` used by `api/inbox.py`, `web/inbox.py` and `api/intake.py`; `Card.processing`, `inbox/_card.html`, the card route. Tests: `tests/test_capture_defer.py`; the worker end-to-end test now drives the deferred flow.
- `upgrade_draft` now also gives the new people their `source_text` and the dictation's `group` (§2.26).

### 2.34 A worker slot reserved for interactive jobs (built)
**Problem.** The worker answered one job at a time. A long `school_read`, `classification` or `report` (minutes) delayed a capture or a message draft that somebody was waiting for, and the weekly review held the queue entirely.

**Decided 2026-10-08 (all answers as recommended; `DECISIONS.md` #30).**
- **Two loops.** `scripts/claude_worker.py` runs its usual loop (any job, interactive first) and a second loop that claims only interactive jobs (`capture`, `draft`; `POST /api/v1/claude-worker/claim?interactive_only=true`). The reserved loop never starts the weekly review. The health check, heartbeat and self-test stay in the first loop; a rate limit pauses both.
  So at most two Claude Code processes run at once: one may be a long job, the other always free for a capture.
- **Setting.** `WORKER_INTERACTIVE_SLOT` in `/etc/orbit-review.env`: on by default; `0`, `false`, `no` or `off` is the old one-job-at-a-time worker.
- **What it costs.** Memory: two Claude Code processes can be alive together on the 1 GB VM (the unit's `MemoryHigh` is 600 MB and `MemoryMax` 900 MB, with the 2 GB swap file, `docs/DEPLOY.md` §10). Subscription: a review no longer pauses captures, so usage in the same 5-hour window is a little denser; the total work is unchanged.
- Server side: `claude_jobs.claim(interactive_only=True)` filters on `priority = INTERACTIVE`; nothing else changes (leases, retries, late answers).

**Built as** `claude_jobs.claim(interactive_only=...)`, the `interactive_only` query parameter of `/claim`, `Worker.loop` / `Worker.run`, `Config.interactive_slot`, the service unit's memory lines and `docs/DEPLOY.md`. Tests: `tests/test_claude_worker_script.py` (the slot, the endpoint, the second loop, the env setting).

### 2.35 Fitness: Liftoff workouts (Phase 33, built 2026-10-09)
**Goal.** The owner's gym workouts from the Liftoff app (getgymbros.com) are in Orbit, so goals, the weekly review and the plan can see training load ("5 gym sessions planned, 3 done"). First proof point of the fitness domain (`VISION.md` §5); Apple Health (sleep, steps) stays a later step.

**Research (2026-10-09).** Liftoff has no public API and no export in the app that I could find. An unofficial MIT-licensed CLI, [quantcli/liftoff-export-cli](https://github.com/quantcli/liftoff-export-cli), exports a user's own data through the app's private API. What it shows:
- tRPC over HTTPS, base `https://v2-16-3.api.getgymbros.com` (the CLI used `v2-13-10`; the app itself used `v2-16-3` on 2026-10-09, seen in a proxy capture that also confirmed the `user.refreshToken` call shape; the version is in the host name; the server answers "server is deprecated" after an app update, so the base must be configurable: `LIFTOFF_API_BASE`).
- Login `user.signIn` (`usernameOrEmail`, `password`, `provider: "gymbros"`) returns `accessToken`, `refreshToken`, `accessTokenExpiresAt`. `user.refreshToken` renews the access token and reuses the refresh token. Accounts without a password can only use a refresh token captured from the app.
- Workouts: `post.getMyPosts` (no input, no pagination, all workouts). Fields: `id`, `startedAt`, `postedAt`, `sessionDuration` (text, "01 hours 06 minutes 01 seconds"), `sessionNotes`, `bodyweight` (string), `caloriesBurned`, `prCount`, `exerciseData[]` (`exerciseName`, `exerciseId`, `exerciseTypes` = `WR` weight/reps, `DD` distance/duration, `ND`, `exerciseNotes`, `setsData[]` with `setType`, `inputOne` = weight in kg or distance, `inputTwo` = reps or seconds).
- **Verified against a real response (2026-10-09, 190 workouts, 1008 exercises, 3094 sets, 2025-03-10 to 2026-10-07; anonymized sample in `tests/fixtures/liftoff/post_getMyPosts.json`):**
  - **Weights and bodyweight are in pounds, not kg** (the CLI comment is wrong): 30 kg arrives as `66.1386786555`, 75 kg as `165.34669663875`. `inputOne` is a float or int, `inputTwo` an int, `bodyweight` a string.
  - Exercise types seen: `WR` weight and reps (942), `BR` bodyweight reps with `inputOne` 0 (61), `AB` weight and reps with bodyweight added or assisted (3), `DD` distance and seconds (2). Set types: `normal` (3089), `drop` (5). `rir`, `superset`, `cableRatio`, `overrideWeightUnit`, `plannedWorkoutId` are always null so far; `exerciseRating` is Liftoff's own score.
  - `sessionDuration` is always "NN hours NN minutes NN seconds" and sometimes absurd (95, 120, 289 hours: sessions left open) or zero. Keep the raw seconds and flag a workout above 6 hours instead of trusting it.
  - `startedAt` and `postedAt` are UTC ISO strings; `postedAt` can be days after `startedAt`. Use `startedAt`. `type` is always `WORKOUT`; `title` null; `caloriesBurned` often 0.
  - Extra fields that carry no training data and are dropped: `image`, `medias`, `PostMedia`, `content`, `experienceGained`, `eggsGained`, `consumablesUsed`, `streaksCount`, `gracePeriod`, `isGhost`, `public`.
- **`DD` distance is km** (the owner, 2026-10-09: a 45 minute treadmill run of 4.1 miles = `6.5983104` km). Both `DD` workouts are treadmill. The second one (7 sets of 120 s at `3.99921984` km each) is physically impossible as 4 km per 2 minutes, so a set faster than 25 km/h is kept but flagged `implausible` and left out of distance totals.
- Not verified: how long the refresh token lives, the real response (field types, units for distance, set types such as warm-up), whether the CLI's description is current. No sample file exists yet (rule 9).

**Proposed design (module contract).**
1. **Ingest.** `orbit/integrations/liftoff.py`: small client (sign-in, refresh, `get_my_posts`), mocked in tests. A job pulls all workouts on a schedule (once a day) and on a "Sync now" button; idempotent by Liftoff post `id`; a changed workout is updated in place (Liftoff is the source, Orbit's copy is read-only).
2. **Models.** `Workout` (liftoff_id, started_at UTC, duration_seconds, bodyweight_minor? no: `bodyweight_grams`, calories, pr_count, notes), `WorkoutExercise` (name, liftoff exercise id, type), `WorkoutSet` (index, type, weight_grams / reps / distance_m / seconds). Integers only. No link to people or goals in v1; goals link through the context provider later.
3. **Context provider** `fitness`: workouts in the window, sessions per week, volume per exercise, PRs, bodyweight trend. Bounded and masked like the others (notes pass through `context.base.text()`).
4. **Proposal kinds:** none in v1 (read-only data; nothing for automation to change).
5. **Web.** `/fitness`: list of workouts, workout detail, sync status. No charts in v1.
6. **Tests.** Parser tests from an anonymized real response in `tests/fixtures/liftoff/`; service tests for upsert, idempotency, duration parsing; client mocked.

**Risks.** Private API: it can change or disable without notice and its terms may forbid third-party access. Storing a Liftoff password (or refresh token) on the server: encrypted in `.env` only, never in the database, never sent to Claude. Sync failure pushes one ntfy message per day.

**Built as** (2026-10-09; route A, `DECISIONS.md` #32; questions C to F taken as recommended, D answered above):
- `integrations/liftoff.py` (client: refresh, `post.getMyPosts`) and `integrations/liftoff_parse.py` (typed parse; pounds to grams, km to metres, duration text to seconds, a set faster than 25 km/h flagged `implausible`, a session over 6 hours `duration_suspect`). Tests: `test_liftoff_client.py`, `test_liftoff_parse.py` (fixture `tests/fixtures/liftoff/post_getMyPosts.json`).
- `domain/fitness.py` (`Workout`, `WorkoutExercise`, `WorkoutSet`; §3), migration 0022; `services/fitness.py` `sync_workouts` (upsert by `liftoff_id`, rebuilt only when `content_hash` changed, vanished workouts kept, an unreadable post skipped and counted). Tests: `test_fitness_sync.py`.
- Job `liftoff_sync` (`JobContext.workouts`, every `LIFTOFF_POLL_HOURS`, default 24, also at start and with Run now; setting on the schedule page under "Fitness"); it is unavailable without `LIFTOFF_REFRESH_TOKEN`. A failed run (rejected token, retired API host) shows on the schedule page and on `/fitness`. No ntfy push on failure yet.
- Context provider `services/context/fitness.py` (`FitnessProvider`, module `fitness` in the snapshot and in the last-week review snapshot): workouts in the window (kg, km, minutes; at most 100), `sessions_in_window`, last workout and days since, latest bodyweight, sessions per week for the last 8 weeks. Empty under a person focus. Tests: `test_context_fitness.py`.
- Web: `/fitness` (list, "Read now"), `/fitness/{id}`; in the More menu. Tests: `test_web_fitness.py`. No API endpoints.
- Not built: failure push, charts, goal links, the "sessions planned" side (needs the planning module), a CLI-push route (`POST /api/v1/fitness/liftoff`).

**Questions asked of the owner (answered as recommended unless noted).**
- A. **Source route.** (1) Orbit calls the private API itself, with a refresh token in `.env` *(recommended: fully automatic)*. (2) You run the CLI on your Mac and a Shortcut or script posts its JSON to `POST /api/v1/fitness/liftoff` *(no credentials on the server, but manual)*. (3) Apple Health only, if Liftoff writes workouts there (no sets or reps).
- B. **Answered 2026-10-09: the owner signs in with Google, so the account has no password.** Password sign-in (`user.signIn`) is not possible. The only way in is a refresh token captured from the app's own traffic (HTTPS proxy such as Proxyman or mitmproxy on the iPhone, look for `user.refreshToken` or the sign-in response); the CLI accepts it as `LIFTOFF_REFRESH_TOKEN`. Open: whether the Google sign-in response gives a refresh token that stays valid long-term, and whether the app pins certificates (then capture fails).
- C. **History.** Import everything Liftoff holds? *Recommended: yes.*
- D. **Units.** The API returns pounds. Store grams (converted, rounded) and show kg? *Recommended: yes.*
- E. **Claude.** Workout data goes to Claude in full through the provider, as for health data (`DECISIONS.md` #7)? *Recommended: yes.*
- F. **Order.** Phase 33 after phase 21 (appointments) is merged? *Recommended: yes.*

### 2.36 Health: sleep, heart rate, steps, bodyweight from the Garmin watch (Phase 34, built; the real Shortcut payload is still to come)
**Goal.** Recovery data next to training load, so a later training agent (§2.37 below) can decide load against deload, and when to train around sleep and the calendar. Second step of the fitness domain after Liftoff (§2.35). The watch is a Garmin, synced to Apple Health (the owner, 2026-10-09).

**Research (2026-10-09, from public guides and Garmin's support list; verify on the phone).**
- Garmin Connect writes to Apple Health, depending on the device and the switches: steps, walking and running distance, heart rate, sleep analysis, active and resting energy, weight, body fat, flights climbed and workouts. It is **one way** (Garmin to Health). It does **not** write HRV, Body Battery, stress, training readiness or training status, which are the best recovery signals.
- Garmin's own Health API is partner-only. The Python library `garminconnect` (unofficial, private endpoints, password and MFA sign-in) exposes sleep stages, HRV, Body Battery, stress, training readiness and status; it can break without notice, like Liftoff's API.
- Apple Health has no server API. Data leaves the phone only through an iOS Shortcut ("Find Health Samples" then POST), which works for daily summaries and not well for dense samples.

**Routes.**
- **H1. Apple Health via Shortcut** (matches `ROADMAP.md` "Later domains"). A daily Shortcut posts yesterday's sleep (start, end, asleep minutes), resting and average heart rate, steps, active energy and the latest weight to `POST /api/v1/health/daily`. No credentials leave the phone, nothing breaks when Garmin changes its app; no HRV, readiness or Body Battery. Runs only when the phone runs the Shortcut automation.
- **H2. Garmin Connect directly** (like Liftoff, DECISIONS #32). Orbit signs in with a token and reads sleep stages, HRV, Body Battery, stress, readiness; fully automatic on the server. Needs the Garmin password or a captured token on the server, MFA handling, and relies on a private API.
- **H3. Both.** H1 first (small, stable), H2 later only if HRV or readiness proves worth the fragility.
*Recommended: H1 now, H2 decided after a few weeks of H1 data.*

**Proposed design (module contract).**
1. **Ingest.** `POST /api/v1/health/daily` (bearer token, like the other Shortcuts; `docs/SHORTCUTS.md` has the recipe). One row per local day, idempotent: a second post for the same day **fills the fields it carries and keeps the others** (changed from "replaces", 2026-10-09: a Shortcut that could not read sleep one morning must not erase an earlier post). null or missing = no data.
2. **Models.** `HealthDay(day unique, sleep_minutes?, sleep_start?, sleep_end?, resting_hr?, avg_hr?, steps?, active_kcal?, bodyweight_grams?, source[apple_health|garmin], received_at)`. Integers only; the sleep night belongs to the day it ends. Later routes may add columns (hrv_ms, body_battery, readiness).
3. **Context provider** `health`: the days in the window, 7 and 28 day averages and the trend (sleep, resting heart rate, steps, bodyweight), nights under a threshold. Bounded and masked.
4. **Proposal kinds:** none (read-only data).
5. **Web.** `/health`: last 30 days as a plain table; no charts in v1. Missing data is shown as missing, never zero.
6. **Tests.** Endpoint and idempotency tests; provider tests; a fixture of a real Shortcut payload once one exists (rule 9: the payload shape is built from a real sample, not guessed).

**Answers (2026-10-09).** A: **H1**. D: **weight from Apple Health**; Liftoff's per-workout bodyweight stays in the `fitness` module and is used only when Health has none. B, C, E, F: recommended defaults taken (E: health data goes to Claude in full; F: Shortcut at 07:00); B and C (what the watch really writes, whether sleep is recorded nightly) are confirmed by the first real payload.

**Built as** (2026-10-09; the payload is Orbit's own contract, which the Shortcut fills, so there is no external format to guess; a real payload is still needed to check the Shortcut):
- `domain/health.py` `HealthDay` (§3), migration 0023, `services/health.py` (`upsert_day`, `list_days`).
- `api/health_days.py`: `POST /api/v1/health/daily` (`day` required; `sleep_minutes` 0 to 1440, `sleep_start`, `sleep_end`, `resting_hr`, `avg_hr`, `min_hr` and `max_hr` 20 to 250, `steps` 0 to 200000, `active_kcal` 0 to 20000, `weight_kg` 20 to 400; numbers as text or decimals are rounded; a time with no offset is Europe/Prague; no day in the future; sleep ending before it starts is refused) and `GET /api/v1/health/days?start&end` (default last 31 days, at most 400).
- Context provider `services/context/health.py` (module `health`, in the snapshot and in the last-week review snapshot): days in the window (at most 120), means over the last 7 and 28 days (sleep, nights under 6 h, resting heart rate, steps, weight, with the count of days that had data), newest day with data, latest weight. Empty under a person focus.
- Web `/health` (More menu): 30 days newest first with 7 and 30 day means; "-" is no data. Tests: `test_health_api.py`, `test_context_health.py`, `test_web_health.py`.
- Shortcut file: built by `docs/shortcuts/build_orbit_health.py` (the signed file is not committed) (details in `docs/SHORTCUTS.md`); posts at 07:00 with `day` = today, the last 24 hours of steps, energy and heart rate, the 7 day mean weight and the last 24 hours of sleep. Unverified on the phone: the Health type names and the sleep sum (Sleep samples may include Awake or In Bed time). The first real payload settles it.
- **Sleep (written against one real phone result, 2026-10-09; the committed fixture is synthetic).** Garmin writes Deep, Core, REM, Awake and one In Bed block per night to Health; Shortcuts cannot sum sleep by stage, so the shortcut posts four raw text columns (`sleep_starts`, `sleep_ends`, `sleep_durations`, `sleep_values`) and `services/sleep_parse.py` computes the night that ended on `day` (samples starting from 18:00 the evening before to 18:00 on `day`): asleep = deep + core + REM, plus each stage, awake, in bed, bedtime and wake time (migration 0025). The four columns must be sent together (else 422 naming the problem); a line that cannot be read is a 422 naming the line. Raw columns win over a plain `sleep_minutes`. Fixture and tests: `tests/fixtures/health/sleep_probe.json`, `tests/test_sleep_parse.py`.
- **Heart rate range:** `min_hr` and `max_hr` (migration 0024) next to the average.
- **Period fix:** the shortcut uses "Start Date is today" (operator 1002) for steps and heart rates and runs in the evening; "in the last 1 day" had covered two calendar days.
- Not built: HRV, Body Battery and readiness (route H2), charts, a push when no post arrives for days.

**Questions asked of the owner.**
- A. **Route** H1, H2 or H3? *Recommended: H1.*
- B. **Which metrics** does Apple Health show from the watch today (sleep, resting heart rate, steps, weight)? Open Health, Browse, and look at which have data from "Garmin Connect". HRV and Body Battery are not expected there.
- C. **Sleep:** does the watch record it every night? *Needed for the sleep trend.*
- D. **Weight:** from the watch or a scale, or typed in? (Liftoff also stores a bodyweight per workout; which one wins when both exist?) *Recommended: Health first, Liftoff as the fallback.*
- E. **Claude sees** health data in full (as `DECISIONS.md` #7 says for health)? *Recommended: yes.*
- F. **Shortcut timing.** Run at 07:00 each day as a personal automation? *Recommended: yes.*

### 2.37 Places, transit times and the school calendar feed (Phase 36, specified 2026-10-09; waiting for the owner's OK)
**Goal.** Lessons, exams and the travel between them are in Apple Calendar through Orbit's own ICS feed, with real door-to-door times
(public transport plus walking) from a transit API. The same travel times feed the training coach (§2.38), so a gym slot costs what it
really costs: from home, from a lesson at Building A, or on the way back.

**Answers so far (the owner, 2026-10-09).** They are **not** subscribed to the SIS calendar, so lessons in Orbit's feed show once: SPEC §2.20
question 3 is reversed (`DECISIONS.md` #33). Travel is **public transport combined with walking**, from a **transit API** (accurate times,
not a typed table). Home and the usual gym are stored as places (configured by the owner).

**Fit with `VISION.md`.** Places link lessons, meetings and workouts through foreign keys (module contract 1). Travel times are ingest from a
real source, cached and idempotent (2). The `travel` part of the calendar provider gives agents minutes and modes, never addresses (3).
Nothing here needs a proposal: lessons come from SIS, travel blocks are computed from them, and neither changes user data (4).

**Research (2026-10-09).**
- Golemio (Prague's data platform) and PID publish GTFS and GTFS-RT feeds, but no journey-planner endpoint. The PID Lítačka planner is built on
  that data and is not a public API.
- **Google Routes API** (`computeRoutes`, `travelMode = TRANSIT`): PID timetables and delays are in Google Maps. A transit route includes its
  walking legs; a plain `WALK` route is asked as well and the shorter one wins. Billed per request with a monthly free cap (a third-party listing
  says 10,000 Essentials requests a month; check the official SKU table when the key is made). Needs a Google Cloud project with billing on and
  an API key. Addresses (home, school buildings, gym) are sent to Google; nothing else is.
- **OpenTripPlanner 2, self-hosted**, on PID GTFS (+ GTFS-RT) and an OpenStreetMap extract of Prague: free, nothing leaves the server, very
  accurate for PID. Needs a Java service of about 2 to 3 GB of memory and a weekly graph rebuild. The worker VM is already tight on memory
  (§2.34), so this likely means a bigger VM.
- IDOS (CHAPS) sells an API; not considered further (paid contract).
*Recommended: Google Routes API now, behind a small `TransitSource` interface, so OTP can replace it later without model changes.*

**Design (module contract).**
1. **Models** (§3, migration 0026).
   - `Place(id, name, kind[home|school|gym|other], address, lat?, lon?, created_at, updated_at)`. Home and gym are entered once on a settings
     page; school places are created by sync from a lesson's `LOCATION` address (the text in parentheses, for example "Vzorová 1,
     10000 Praha 1"), one per distinct address. `Lesson.place_id`, `SchoolEvent.place_id` and `Meeting.place_id` (all nullable) link to it.
   - `TravelLeg(id, from_place_id, to_place_id, depart_bucket UTC (15 minute slot), arrive_by?, minutes, mode[transit|walk], summary
     (for example "walk 4 · tram 1 · walk 3"), lines_json, fetched_at)`; unique on (from, to, bucket, arrive_by flag). A leg asked again within
     its bucket comes from the cache; legs older than 7 days are refreshed when used.
2. **Ingest.** `orbit/integrations/transit.py` (`TransitSource` protocol, `GoogleRoutesSource` with httpx; no new dependency). Requests use
   `arrivalTime` (to be at a lesson on time) or `departureTime` (going home). A job after each SIS sync fills the legs the next 14 days need;
   a monthly request cap (`TRANSIT_MAX_REQUESTS_MONTH`, default 3000) stops calls and shows a warning on the schedule page. Failure: the block
   shows "travel time unknown" and one push per day.
3. **Calendar feed** (`services/ics.py`). New events, all with stable UIDs:
   - 🎓 lessons (code, name, room, teacher), from `Lesson`; a cancelled lesson is removed from the feed (Apple Calendar drops it).
   - 📝 exams and deadlines from `SchoolEvent` (deadlines all-day or at their time).
   - 🚋 travel blocks: home → first place of the day, place → place when it changes, last place → home; ending at the start of the next item
     minus a buffer (`TRAVEL_BUFFER_MIN`, default 5). Title "🚋 24 min to Building A", description with the legs ("leave 8:16, tram 3 from
     the home stop"). No block when the gap between two items at different places is shorter than the travel time: then the later item gets a
     "⚠ tight transfer" note instead.
   - Home in the feed is the word "Home": the address never appears in event text.
4. **Context provider.** The `school` provider gains `place` (name and kind, no address) per lesson; a new `travel` section of the calendar
   provider lists, per day in the window, the travel blocks (minutes, mode summary) and tight transfers. Agents never see addresses or
   coordinates.
5. **Web.** `/places`: home, gym, school places, with "Test route" (shows one live leg). `/school` week view shows travel blocks greyed.
6. **Tests.** Fixture of a real Routes API response (`tests/fixtures/transit/`, built from a real call, rule 9); source mocked in service tests;
   travel-block builder (first and last of day, same place, tight transfer, cancelled lesson, buffer); cache and cap; ICS output and UIDs;
   address never in ICS text or provider output.

**Open questions** (recommendation first):
1. **Transit source:** Google Routes API (needs a Google Cloud project with billing and a key in `.env`; addresses go to Google) or
   self-hosted OpenTripPlanner (free, private, needs a bigger VM)? *Recommended: Google.*
2. **Home in travel:** always start the day from home and end it at home? *Recommended: yes; a day with a meeting elsewhere chains through
   the meeting's place when it has one.*
3. **Buffer and walking:** 5 minutes buffer before a lesson; walking only when it is not slower than transit by more than 5 minutes?
   *Recommended: yes.*
4. **A far-away sports hall:** a sports lesson in a separate place; treat like any other place? *Recommended: yes.*

### 2.38 Training coach: recovery, post-workout analysis and AI-built workouts (Phase 37, specified 2026-10-09; waiting for the owner's OK)
**Goal.** Orbit plans the owner's training into their days. Claude builds each workout (exercises, sets, reps, target weights, rest, estimated
duration), finds the slot that fits lessons, meetings, travel and recovery, and after every logged workout tells them what improved and how well
each muscle had recovered. Recovery estimates start from published research and are then fitted to the owner's own results. This replaces the
earlier "training agent" idea; nutrition and meal timing stay out until Phase 35 has a source.

**Answers so far (the owner, 2026-10-09).** Fully AI-driven: Claude builds a new workout each time and estimates its length; session length today
is about 60 to 70 minutes. Feedback after each workout, based on reps and weights against earlier sessions, sleep and other factors.

**Fit with `VISION.md`.** Reads only through providers (fitness, health, school, calendar, goals). A planned workout is an **L2 proposal**:
nothing reaches the calendar until the owner confirms. Post-workout analysis and the recovery board are **L1** (read-only output). The personal
recovery fit is Orbit's own estimate, kept with its history and shown; it changes no user data (a `DECISIONS.md` entry records this once question 4 is answered).
`VISION.md §7` warns against breadth before the weekly review is proven on real weeks; the owner chose to build this now.

**What the research supports (written into the prompt and shown on the page, so the numbers are not presented as exact).**
- After a session, muscle protein synthesis stays raised for about 24 to 48 hours (MacDougall 1995; Phillips 1997).
- Strength and soreness after hard, high-volume work recover in about 48 to 72 hours, longer after heavy eccentric work (RDL, squats, Nordic
  curls) and in larger muscles; small muscles and low volume recover in about 24 to 48 hours.
- Training a muscle twice a week beats once a week for hypertrophy; with equal weekly volume, frequency matters less (Schoenfeld 2016, 2019).
  About 10 or more hard sets per muscle per week give more growth than fewer (Schoenfeld 2017).
- Short sleep lowers strength and recovery; no study gives an hour count per muscle. So Orbit uses these as **starting values** and fits them to
  the owner's own performance.

**Design (module contract).**
1. **Models** (§3, migration 0027).
   - `Muscle` enum (about 14: chest, front delts, side delts, rear delts, lats, upper back, traps, biceps, triceps, forearms, abs, glutes, quads,
     hamstrings, calves, lower back).
   - `ExerciseMuscle(liftoff_exercise_id, name, muscle, share 0–100)`: which muscles an exercise trains and how much. Claude proposes the map
     for every exercise not yet mapped (one Inbox card per batch, proposal kind `exercise_muscles`); the owner confirms or edits.
   - `MuscleRecovery(muscle, base_hours, fitted_hours?, samples, updated_at)` plus `MuscleRecoveryLog` (each change with the reason): starting
     values from the research above; `fitted_hours` from the post-workout analysis.
   - `WorkoutPlan(id, status[proposed|confirmed|done|skipped|replaced], day, start_at UTC, place_id -> Place, est_minutes, focus (muscles),
     plan_json (exercises with sets, reps, target kg, rest seconds, notes), reasoning, travel_before_min, travel_after_min, meeting_id? (the
     calendar block made on confirm), workout_id? -> Workout (matched after it was logged), created_at)`.
   - `WorkoutAnalysis(id, workout_id unique -> Workout, plan_id?, readiness per muscle json, findings_json, summary, created_at)`.
2. **Ingest.** Liftoff sync runs **every 30 minutes from 06:00 to 23:00** (one call each; was once a day), so analysis follows a workout
   within half an hour. A new workout is matched to the confirmed plan of that day (same day, start within 3 hours).
3. **Deterministic numbers first (no Claude).** For each muscle: hard sets in the last 7 days (sets × share), hours since last trained,
   estimated remaining recovery from `fitted_hours` or `base_hours`, scaled by the volume of that session. For each exercise: estimated
   one-rep max (Epley) per session, best reps at a matched weight, trend over the last 6 sessions. These go to Claude as context and show on
   the page even if Claude is down.
4. **Post-workout analysis (L1, Claude).** After a new workout syncs, Claude gets: the workout, the plan it followed (if any), the same
   exercises' last sessions, the per-muscle numbers, the nights before (sleep, stages, resting heart rate against the 28-day mean), steps of the
   day before, and lessons and travel of the day. It returns (Pydantic-validated): per exercise improved / held / dropped with the reason it
   sees; per muscle trained "recovered / partly / not recovered" with evidence; plan adherence; one short summary. Push (ntfy) with the summary;
   full text on `/fitness/{id}`.
   - **Recovery fit:** when a muscle's lifts improved or held after N hours, that supports recovery ≤ N; a drop after N hours (with normal
     sleep) supports > N. Orbit (not Claude) updates `fitted_hours` from these outcomes, bounded to 24–120 hours and logged.
5. **Workout builder (L2, Claude).** Runs each evening at `TRAINING_PLAN_AT` (default 20:00) for the next day, and on "Plan now" (web,
   Shortcut, push button). Claude gets: recovery board, last 4 weeks of training and analyses, health trends, the day's lessons, exams,
   meetings and free windows with real travel minutes (home → gym, school → gym, gym → next item), gym opening hours, goals and constraints
   from the training profile. It returns one workout or a rest day: focus, exercises with sets, reps, target kg and rest, the slot, and the
   **estimated duration**. Orbit computes its own estimate too (sets × (work + rest) + warm-up, fitted to Liftoff's real session lengths) and
   shows both; a slot is valid only if Orbit's estimate plus travel fits the window. The proposal card shows the slot, travel, exercises and the
   reasoning. **Confirm** creates a `Meeting(calendar_name = "orbit")` "🏋️ Push day · ~65 min" with the exercise list in the description, plus
   travel blocks (§2.37) in the ICS feed. Reject with a reason goes into the correction memory (§2.28). The workout itself is logged in Liftoff
   as today; Orbit does not write to Liftoff.
6. **Training profile** (web settings page, the owner edits): goal (hypertrophy / strength / both), target sessions per week, days or hours that
   are off limits, gym opening hours, equipment that is missing, injuries or exercises to avoid. Sent to Claude in full.
7. **Context provider** `fitness` gains `recovery` (per muscle: status, hours since, hours left, hard sets this week), `plans` (planned vs
   done this week) and the latest analyses. The weekly review sees planned vs done ("5 sessions planned, 3 done").
8. **Web.** `/fitness/today`: recovery board (muscle, status, ready at), today's plan, "Plan now". `/fitness/{id}`: analysis next to the
   workout. `/fitness/profile`. The board states that recovery times are estimates from research, fitted to their data.
9. **Tests.** Muscle numbers and Epley from the Liftoff fixture; recovery fit (improved, dropped, short sleep excluded, bounds); plan slot
   validation against lessons, travel and opening hours; plan ↔ workout matching; proposal apply creates the meeting and travel blocks;
   Claude mocked with fixed outputs; a malformed Claude answer is retried with the error (§2.32) and never applied.

**Open questions** (recommendation first):
1. **Goal and frequency:** hypertrophy, strength or both; how many sessions a week? *Recommended: hypertrophy with strength tracking, 4 a week.*
2. **When plans come:** each evening for the next day (push with Confirm / Another / Rest day buttons), plus "Plan now"? *Recommended: yes.*
3. **Morning sleep:** the health Shortcut posts in the evening, so a plan made at 20:00 knows last night's sleep but a morning re-plan does
   not see the night just ended. Add a 07:00 run of the same Shortcut (sleep fills in, the rest is kept)? *Recommended: yes.*
4. **Recovery fit without asking:** Orbit adjusts each muscle's recovery hours from the analyses by itself (logged, shown, bounded)?
   *Recommended: yes (L1).* Alternative: each change is an Inbox card.
5. **Gym hours and equipment** of the usual gym: type them into the profile, or should Orbit assume a full gym? *Recommended: type them.*
6. **Liftoff polling every 30 minutes** on a private API (17 hours × 2 = 34 calls a day instead of 1)? *Recommended: yes; drop to hourly if it
   draws attention.*
7. **Exercise map:** Claude proposes muscles for all ~60 existing exercises in one card to review? *Recommended: yes.*
8. **Rest days and deloads:** may the builder propose a deload week when lifts drop for two weeks with normal sleep? *Recommended: yes, as a
   proposal.*

### 2.39 Claude Code MCP server: read Orbit's context from a chat (built 2026-10-15)
**Goal.** The owner chats about their data (workouts, sleep, goals, money) in Claude Code on the Mac and asks for ideas. Claude Code reads Orbit through a local MCP server instead of pasted curl output.

**Shape.** `orbit/mcp_server.py`, a stdio MCP server (the `mcp` SDK, `MCPServer`), started by Claude Code from `.mcp.json`: `uv run python -m orbit.mcp_server`. It is a client of Orbit's HTTP API, like the Shortcuts: it opens no database and imports no service. It reads `ORBIT_URL` (default `http://localhost:8000`) and `ORBIT_API_TOKEN` from its own environment; a missing token stops it at start with a clear message. The token is never printed or put in an error.

**Tools (v1, read-only).**
- `get_context(preset?, start?, end?, person_id?)`: `GET /api/v1/context`. Returns the `ContextSnapshot` JSON (people, ledger, meetings, promises, goals, school, fitness, health), already masked and bounded by the server (rule 5, `VISION.md` §4). Window as in §2.16: `preset` is `this_week` or `next_7_days`, or `start` + `end` (ISO dates).

**Rules.** No write tool: advice in chat changes nothing; a plan the owner likes goes in through the web UI or a capture, so it becomes a proposal (rule 1). A non-2xx answer becomes a short tool error with the status and the server's `detail`, never the request headers. The server speaks to the Orbit instance in `ORBIT_URL` only, local or production.

**Open questions.**
1. Raw sets and reps are not in the snapshot (only the `fitness` summary). Progression analysis needs a read-only `GET /api/v1/fitness/workouts` and a second tool; waits for the owner's OK.
2. `GET /api/v1/health/days` as a tool for raw sleep and heart rate rows.
3. Production: `ORBIT_URL` over HTTPS with the bearer token on the Mac only; keep the token out of `.mcp.json` (it is checked in) and set it in the shell.

## 3. Data model (SQLAlchemy)

```
Person(id, display_name, first_name?, last_name?, birthday_month?, birthday_day?, birthday_year?,
       nameday_country? (default SK), nameday_name?, country?, city?, how_met?, met_through_id? -> Person, photo_url?, archived,
       vcard_uid? unique, created_at, updated_at)
Alias(id, person_id, value, value_norm)                             # unique(person_id, value_norm)
Handle(id, person_id, type, value, value_norm, label?)            # unique(type, value_norm)
Tag(id, name, name_norm unique) ; PersonTag(person_id, tag_id)
Note(id, person_id, text, text_norm, meeting_id?, created_at, updated_at)
Interaction(id, occurred_on, kind, summary?, meeting_id?) ; InteractionPerson(interaction_id, person_id)
NotDuplicate(person_a_id, person_b_id)                              # dismissed duplicate pairs, a < b

Debt(id, person_id, direction, type, amount_minor?, currency?, description? (required for item/favor),
     reference_code? unique, due?, reminder_days?, parent_id? -> Debt,
     source_transaction_id?, created_at, updated_at, settled_at?, settled_by_transaction_id?)
# source_transaction_id: the bank row it was split from (Phase 6); settled_by_transaction_id: the incoming
# payment it was settled with (Phase 7, set on confirm, cleared by an undo).

Transaction(id, bank, booking_date, occurred_at?, amount_minor (>0), currency, direction[in|out],
            kind[card|transfer|topup|exchange|atm|fee|other], counterparty?, counterparty_norm?,
            counterparty_iban? (compact, for payer matching), iban_masked?, message?, message_norm?, bank_reference?,
            classification?, hash unique, import_id, raw_json)             # table: bank_transaction
BankImport(id, bank, filename, imported_at, rows_total, rows_new, rows_duplicate, rows_skipped)

Meeting(id, calendar_uid unique, calendar_name, title, location?, start_utc, end_utc,
        cancelled, link_key?, brief_sent_at?, capture_sent_at?, brief_summary?, created_at, updated_at)
# link_key: hash of title + location + attendee emails; automatic links are redone only when it changes.
# brief_summary: Claude's one-liner, written when the brief is sent.
MeetingPerson(meeting_id, person_id, link_source[attendee|title|claude|manual])

ContactLink(person_id unique, carddav_uid unique, href, etag, base_json, last_synced_at)
# base_json: field -> value at the last sync (the "base" of the three-way diff)
SyncRun(id, source[carddav|vcf], started_at, applied_at?, status[review|applied|failed], summary_json)
SyncChange(id, run_id, person_id?, field, direction[pull|push], kind[auto|proposal|conflict|new_contact|
           possible_match|removed_upstream], old_json?, new_json?, state[pending|applied|rejected|undone],
           reject_hash?)                                           # reject_hash: remembered rejections
ContactBackup(id, person_id, run_id?, href, vcard_text, etag_after?, created_at, restored_at?)

Promise(id, person_id, direction, text, due?, status, meeting_id?, goal_id? -> Goal, created_at, updated_at, closed_at?)
# goal_id (Phase 15): the goal this promise serves; cleared when the goal is deleted.

Goal(id, title, title_norm, why?, horizon?[week|month|quarter|year|ongoing], target?, target_date?,
     status[active|paused|done|dropped], rank?, reviewed_at?, created_at, updated_at, closed_at?)
GoalPerson(goal_id, person_id)
WeeklyReview(id, plan_start unique, review_start, review_end, plan_end, status, output_json?, input_json?,
             input_chars?, model?, input_tokens, output_tokens, error?, attempts, pushed_at?, created_at, updated_at)
# (Phase 16, §2.17) status[pending|ready|failed]; one per planned week; input_json holds what Claude was given.
# (Phase 15, §2.16) rank is set exactly when status = active (check constraint), 1..n contiguous;
# title_norm is unique among active goals (service-enforced, like tags).

ReviewReport(id, range_start, range_end, focus_text?, focus_goal_id?, focus_person_id?, focus_course_id?, status[pending|ready|failed],
             output_json?, numbers_json?, input_json?, input_chars?, model?, input_tokens, output_tokens, error?, attempts, claude_used, plain_reason?, created_at, updated_at)
# (Phase 20, §2.22) one row per report; the focus links are set null when their goal, person or course is deleted; ClaudeJob.report_id links a queued report job.

AppSetting(key pk, value_json, updated_at)
AppSettingLog(id, key, old_json?, new_json?, created_at)
JobRun(job_id pk, last_started_at, last_finished_at, ok, message, manual)
# (Phase 19, §2.21) an override beats the env value; no row = the env value applies; the log holds every change.

ClaudeJob(id, kind, status[queued|running|done|failed|expired|cancelled], priority, input_hash, system, prompt, schema_json,
          wait_until, late_json?, review_id? -> WeeklyReview, consumed, attempts, claimed_at?, lease_until?, finished_at?, result_json?, error?, model?,
          input_tokens, output_tokens, duration_ms?, created_at)
# (Phase 17, §2.19) prompt/result cleared after CLAUDE_JOB_RETENTION_DAYS; late_json is set for captures (late upgrade).
ClaudeWorker(id=1, last_seen_at, cli_version?, auth_ok, rate_limited_until?, last_selftest_at?)   # one row

Course(id, code unique, name, name_en?, credits?, hours_lecture?, hours_seminar?, completion?, semester?[winter|summer|both],
       faculty_code?, department?, guarantor?, language?, web_url?, detail_fetched_at?, detail_failed_at?, detail_tries, created_at, updated_at)
Lesson(id, uid unique, ticket_code? index, kind[lecture|seminar|other], course_id? -> Course, title, teachers_text?, room?, location?,
       start_utc index, end_utc, cancelled, first_seen_at, changed_at? index, change_note?, raw_json?, created_at, updated_at)
SchoolEvent(id, source[exam_feed|events_feed|harmonogram|recodex|manual|owl|proposal|outlook], uid? , kind[exam|deadline|event|term|other], title, description?,
            location?, url?, course_id? -> Course, start_utc index, end_utc?, second_deadline_utc?, points?, max_points?,  # points: Numeric(6,2), ReCodEx/Owl
            all_day, cancelled, raw_json?, created_at, updated_at)
            # unique(source, uid) for feed rows; manual rows have no uid. (Phase 18, §2.20)
SchoolResource(id, course_id?, kind[page|pdf|file|github|note|login], origin[sis|owl|ticket|claude|manual], url?, file_sha256?, label, kept, dismissed,
               status[ok|unchanged|changed|no_text|error|broken|login], etag?, last_modified?, content_hash?, text?, prev_text?, last_diff?,
               checked_at?, changed_at?, read_at?, fail_count, error?, suggested_reason?, created_at, updated_at)
CourseFact(id, course_id, topic[grading|attendance|exam|other], text, quote?, resource_id?, created_at)
# (Phase 18c, §2.20) text is capped at 200,000 characters; a suggestion is kept = false; dismissed rows are never suggested again.
SisFetch(id, source[roz_externi|rozvrhng|term_st2|stev|course|harmonogram|recodex|recodex_api|owl], started_at, finished_at?, status[ok|unchanged|empty|denied|error],
         items_seen, items_changed, body_hash?, error?)            # never holds the token; error text is redacted
# (Phase 18, §2.20) Lesson.uid is normalized "<ticket>_<julianDay>" (feed UID without the RNG prefix and @ suffix); a vanished future lesson is
# cancelled, never deleted. Phase 18b adds SchoolAnnouncement(ticket_code, course_id?, sent_at, sender_text, body, imported_at).
CourseScore(id, course_id -> Course, source[recodex|owl], gained, total, pass_threshold?, passes?, fetched_at)   # unique(course_id, source); Numeric(8,2)
OwlCourse(id, path unique "<sem>/<ident>", name, semester_label?, course_id? -> Course, link_manual, description?, fetched_at?, created_at, updated_at)
# Later school phases (§2.20 "Sources beyond SIS"):
# 18c SchoolResource and CourseFact (fields in §2.20 "Phase 18c"), replacing the earlier CourseSite draft: etag?, last_modified?, text_hash?, text?, last_diff?, status[ok|unchanged|error|broken],
#     checked_at?, changed_at?, fail_count) and proposal kind school_deadline; 18d MsAccount(encrypted refresh token, scopes, delta links, status) and
#     SchoolMail(message_id unique, received_at, sender_text, subject, body_text, course_id?, source[graph|shortcut|forward], sent_to_claude_at?).

# Fitness (Phase 33, §2.35). Weights are grams, distances metres, times UTC; Liftoff is the source and the copy is read only.
Workout(id, liftoff_id unique, started_at, posted_at?, duration_seconds, duration_suspect, bodyweight_grams?, calories, pr_count, notes?, content_hash, created_at, updated_at)
WorkoutExercise(id, workout_id -> Workout cascade, position, liftoff_exercise_id, name, kind[WR|BR|AB|DD|other as given], notes?)
WorkoutSet(id, exercise_id -> WorkoutExercise cascade, position, set_type[normal|drop], weight_grams?, reps?, distance_m?, seconds?, implausible)

# Health (Phase 34, §2.36). One row per local day; null is "no data"; times UTC; weight in grams.
HealthDay(id, day unique, sleep_minutes?, sleep_start?, sleep_end?, sleep_deep_minutes?, sleep_core_minutes?, sleep_rem_minutes?, sleep_awake_minutes?, sleep_in_bed_minutes?, resting_hr?, avg_hr?, min_hr?, max_hr?, steps?, active_kcal?, weight_grams?, source, received_at, created_at, updated_at)

# Places and travel (Phase 36, §2.37, proposed). Addresses never leave Orbit except to the transit API; agents get place names and minutes.
Place(id, name, kind[home|school|gym|other], address, lat?, lon?, created_at, updated_at)   # Lesson.place_id?, SchoolEvent.place_id?, Meeting.place_id?
TravelLeg(id, from_place_id, to_place_id, depart_bucket, arrive_by, minutes, mode[transit|walk], summary, lines_json, fetched_at)   # unique (from, to, bucket, arrive_by)

# Training coach (Phase 37, §2.38, proposed).
ExerciseMuscle(id, liftoff_exercise_id, name, muscle, share)   # unique (liftoff_exercise_id, muscle); share 0-100
MuscleRecovery(muscle unique, base_hours, fitted_hours?, samples, updated_at)   MuscleRecoveryLog(id, muscle, old_hours, new_hours, reason, workout_id?, created_at)
WorkoutPlan(id, status[proposed|confirmed|done|skipped|replaced], day, start_at, place_id?, est_minutes, claude_est_minutes, focus_json, plan_json, reasoning, travel_before_min?, travel_after_min?, meeting_id?, workout_id?, proposal_id?, created_at, updated_at)
WorkoutAnalysis(id, workout_id unique, plan_id?, readiness_json, findings_json, summary, created_at)
TrainingProfile(id = 1, goal, sessions_per_week, off_limits_json, gym_hours_json, missing_equipment, avoid, updated_at)

Proposal(id, kind, status[pending|confirmed|rejected], summary, payload_json, confidence?,
         source_type, source_id?, created_at, resolved_at?, reopened_at?)
Notification(id, dedupe_key unique, kind, title, body, payload_json, created_at, not_before, sent_at?,
             delivered bool, attempts, last_error?)                # not_before: end of quiet hours / next retry
```

Normalization helper (`value_norm`): lowercase, strip diacritics (NFKD), collapse whitespace; phone → E.164, using
the person's `country` as the region for numbers without an international prefix. There is no global default region.

---

## 4. API surface (v1, bearer token)
```
GET/POST        /api/v1/people            GET/PATCH/DELETE /api/v1/people/{id}
POST            /api/v1/people/{id}/handles|aliases|notes|tags   DELETE .../{sub_id}   PATCH .../notes/{id}
GET             /api/v1/people/duplicates POST /api/v1/people/duplicates/dismiss
POST            /api/v1/people/merge
GET             /api/v1/tags
GET/POST        /api/v1/interactions      PATCH/DELETE /api/v1/interactions/{id}
POST            /api/v1/imports/vcard     POST /api/v1/imports/icloud-contacts
POST            /api/v1/contacts/sync     GET /api/v1/contacts/sync/{run_id}     POST .../apply (body: checked change ids, conflict picks)
POST            /api/v1/contacts/sync/{run_id}/undo (body: change id)   POST /api/v1/contacts/sync/vcf
POST            /api/v1/contacts/sync/backups/{id}/restore   POST /api/v1/contacts/sync/forget
GET/POST        /api/v1/debts             GET/PATCH/DELETE /api/v1/debts/{id}   (edit/delete: open debts only)
POST            /api/v1/debts/{id}/settle   (body: amount_minor? for partial)   POST /api/v1/debts/{id}/unsettle
GET             /api/v1/balances
POST            /api/v1/capture
POST            /api/v1/imports/bank
GET             /api/v1/inbox             POST /api/v1/inbox/{id}/confirm|reject|reopen   (confirm may carry edited payload)
GET             /api/v1/meetings          GET /api/v1/meetings/{id}      POST /api/v1/meetings/sync
POST            /api/v1/meetings/{id}/people   (body: person_id)      DELETE /api/v1/meetings/{id}/people/{person_id}
GET/POST        /api/v1/promises          GET/PATCH/DELETE /api/v1/promises/{id}
GET/POST        /api/v1/goals             GET/PATCH/DELETE /api/v1/goals/{id}   (Phase 15, §2.16)
POST            /api/v1/goals/rank        (body: ids of the active goals in the new order)   POST /api/v1/goals/{id}/review
POST            /api/v1/goals/{id}/people (body: person_id)   DELETE .../people/{person_id}
POST            /api/v1/goals/{id}/promises (body: promise_id)   DELETE .../promises/{promise_id}
GET             /api/v1/school/lessons    ?start=&end=&course=      GET /api/v1/school/courses   GET /api/v1/school/courses/{code}   (Phase 18, §2.20)
GET/POST        /api/v1/school/events     PATCH/DELETE /api/v1/school/events/{id}   (manual events only; GET lists exams, deadlines and feed events)
                (body: title, kind deadline|exam|other, day YYYY-MM-DD, at HH:MM? (none = all day), course_code?, note?; PATCH: fields left out stay)
POST            /api/v1/school/sync       GET /api/v1/school/status   (last fetch per feed)
GET/POST        /api/v1/school/resources (?course=CODE; JSON {url | note, course_code?, label?})   POST .../resources/file (multipart: file, course_code?)   (18c)
POST            /api/v1/school/resources/{id}/keep|dismiss|check   PATCH .../{id} {course_code|null}   DELETE .../{id}   (18c)
POST            /api/v1/school/sis-import (18b: SIS and Owl pages)   POST /api/v1/school/mail (18d, share-sheet route)
GET             /school/map  /school/map.json  /school/map/panel[/{card}]   (web, 18c)
GET             /api/v1/reviews           GET /api/v1/reviews/{id}   GET /api/v1/reviews/{id}/input   POST /api/v1/reviews/run   (Phase 16, §2.17)
POST            /api/v1/review-agent/prepare   POST .../{id}/submit   POST .../{id}/fail   (Phase 16b, §2.18; review token)
POST            /api/v1/claude-worker/claim   POST .../jobs/{id}/submit|fail|rerun   POST .../heartbeat   GET .../status   (Phase 17, §2.19; worker token)
POST            /api/v1/reports (202; start, end, focus?, goal_id?, person_id?, course_id?)   GET /api/v1/reports   GET/DELETE /api/v1/reports/{id}   GET .../{id}/input   POST .../{id}/retry   (Phase 20, §2.22)
GET/PATCH       /api/v1/settings/schedule   DELETE /api/v1/settings/schedule/{key}   GET /api/v1/jobs   POST /api/v1/jobs/{job_id}/run   (Phase 19, §2.21)
GET             /api/v1/context           ?preset=this_week|next_7_days | start=&end=   &person_id=   (the context snapshot, §2.15)
GET/POST        /api/v1/places   PATCH/DELETE /api/v1/places/{id}   POST /api/v1/places/{id}/test-route   (Phase 36, §2.37)
GET             /api/v1/fitness/today (recovery board + plan)   POST /api/v1/fitness/plan (Plan now, 202)   GET/PUT /api/v1/fitness/profile   (Phase 37, §2.38)
GET             /calendar/{ICS_TOKEN}.ics                     (token in path, no bearer)
POST            /a/{token}                                    (push action buttons; signed token, no bearer)
GET             /healthz
```

---

## 5. Out of scope for v1 (backlog)
- PAY by square (SK) / QR Platba (CZ) payment QR per debt, embedding `reference_code`
- Auto "last contact" from macOS iMessage `chat.db` / call history
- Instagram "Download your information" followers/following import
- MCP server so Claude desktop can query/edit Orbit
- Automatic bank feed via a PSD2 aggregator (replacing CSV upload)
- Gift history & wishlists, social graph ("who can introduce me to…"), city-based "who do I know in Brno"
- Home Screen widget (Scriptable)
