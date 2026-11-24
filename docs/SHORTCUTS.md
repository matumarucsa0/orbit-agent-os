# Apple integration contract

The phone is a thin client: iOS Shortcuts send data in, ntfy pushes come out, Apple Calendar subscribes to the ICS feed.
All logic lives in the Orbit server.

## ntfy (push)
- Install the ntfy iOS app and subscribe to `NTFY_TOPIC` on `NTFY_URL` (self-hosted preferred; otherwise ntfy.sh with a
  long random topic + access token).
- Orbit publishes JSON:
  ```json
  {
    "topic": "<NTFY_TOPIC>",
    "title": "💶 Received 24.00 € from Patrik Veselý",
    "message": "“pizza + kino” · Matches: pizza 12.00 € + kino 12.00 €",
    "tags": ["moneybag"],
    "click": "https://orbit.example.com/inbox/173",
    "actions": [
      {"action": "http", "label": "Settle", "url": "https://orbit.example.com/a/<signed-token>", "method": "POST", "clear": true},
      {"action": "view", "label": "Choose…", "url": "https://orbit.example.com/inbox/173"},
      {"action": "http", "label": "Not a debt", "url": "https://orbit.example.com/a/<signed-token>", "method": "POST", "clear": true}
    ]
  }
  ```
- Check the setup from the server with `uv run python -m orbit.cli test-push`.
- **Action URLs use single-purpose signed tokens** (`/a/{token}`: HMAC-signed `{proposal_id, action, exp}`, 7-day
  expiry, single use). The main API bearer token must never be placed in notification payloads.

## Shortcuts to create on the iPhone

### `Orbit Capture` (Action Button / Siri / from post-meeting push)
1. Receive input: **Text** (optional — the meeting ID when launched from a push)
2. Dictate Text (language: Slovak — or ask each time)
3. Get Contents of URL → `POST {ORBIT_URL}/api/v1/capture`
   Headers: `Authorization: Bearer {ORBIT_API_TOKEN}`, JSON body: `{"text": <Dictated Text>, "meeting_id": <Shortcut Input or empty>}`
4. Show Notification → response `summary` (the response is `{"summary": "...", "proposal_ids": [...], "items": 2}`)

Nothing is applied until it's confirmed in the Inbox. The request answers at once (under a second): the text is saved as
a note draft ("Saved; Claude is reading it and you get a push when it is ready.", `"items": 1`), Claude reads it
afterwards, and a push "📥 Capture ready" says when the draft has been replaced by the extracted items. If Claude isn't
configured or fails, the draft simply stays a note. The Shortcut's own timeout no longer matters. The response's
`proposal_ids` is the draft; read the Inbox for the final proposals (`SPEC §2.33`).

Launched from ntfy via `shortcuts://run-shortcut?name=Orbit%20Capture&input=text&text=<meeting_id>`: the post-meeting
push ("📝 Anything to note about Paťo?", 15 minutes after a meeting linked to someone ends) has a **Dictate** button
that runs the Shortcut with the meeting's id as its input. The proposal then also holds the meeting's `meet`
interaction, and the Claude prompt knows who you just met, so "má novú prácu" is enough. For this the Shortcut must
pass **Shortcut Input** (not the dictated text) as `meeting_id`; launched any other way, the input is empty and the
capture is an ordinary one.

### `Orbit Share` (share sheet: text, links, files; replaces `Orbit Import` and the school share Shortcuts)
1. Receive **Text, URLs and Files** from the Share Sheet
2. Get Contents of URL → `POST {ORBIT_URL}/api/v1/intake`
   Headers: `Authorization: Bearer {ORBIT_API_TOKEN}`, Request Body: **Form**. One field, by what was shared:
   `file` (File: Shortcut Input), or `url` (the link; optional `text` next to it, e.g. "NINF030 rules"), or `text`.
3. Show Notification → response `summary`; the response is `{"route": ..., "summary": "...", "ids": [...], "course": "NINF030" or null}`

Orbit decides where it goes (`SPEC §2.29`), with no choice to make: a bank CSV → bank import; a PDF → school resource;
a link → school resource; a vCard → contacts import; plain text → capture. A PDF or link is filed under a course when its
filename or text names exactly one current course (code or name), else it waits in "Unsorted" on the School page.
A file that is none of these is refused (422) and nothing is saved. The older Shortcuts keep working.

### `Orbit Health` (personal automation, every day 07:00; SPEC §2.36)
**Generated file:** run `python3 docs/shortcuts/build_orbit_health.py` on a Mac; it writes and signs `docs/shortcuts/Orbit Health.shortcut` (not committed; AirDrop or open it on the iPhone, tap Add Shortcut). It asks for your Orbit address (no slash at the end) and API token once, at import; nothing secret is in the file. (`--sleep-test` builds an offline probe that copies the raw sleep samples to the clipboard). The Health filter structure was copied from a real shortcut exported from an iPhone.

**Run it in the evening** (a personal automation at about 22:00). It sends no `day`: Orbit uses today's date (Europe/Prague). It posts:
- `steps`, `resting_hr`, `avg_hr`, `min_hr`, `max_hr`: **today** so far ("Start Date is today").
- `weight_kg`: the mean of the last 7 days.
- `sleep_starts`, `sleep_ends`, `sleep_durations`, `sleep_values`: the Health sleep samples since the start of yesterday, as four text columns with one line per sample, in the form Shortcuts prints them (`8 Oct 2026 at 23:17`, `1:33:00`, `Deep`). Orbit (`services/sleep_parse.py`) picks the night that ended today (samples starting from 18:00 yesterday to 18:00 today) and stores asleep minutes (deep + core + REM), each stage, awake minutes, time in bed, bedtime and wake time. "In Bed" is kept apart and is never added to the sleep time. The parser was written from one real result of the sleep test; `tests/fixtures/health/sleep_probe.json` is a synthetic sample in the same format. It needs the iPhone language to be English (stage names, month names).

**Known limits (found on the phone, 2026-10-09).**
- "Start Date is in the last 1 day" means since the **start of yesterday**, so two calendar days, not 24 hours; that is why the first run showed about double the steps. Use "is today".
- A "Find Health Samples" step with no samples stops the whole shortcut ("No Samples Found"): only metrics the watch really writes to Health may be in it. Active Energy was left out for that reason (no samples).
- Steps from the iPhone and from the watch may both be counted; compare with Health on the first days.
- Health values may trigger iOS's "trying to share health items" prompt; allow it in the Shortcuts settings.
- If the evening run happens before the night's samples exist (a late wake up), the night is simply missing and the next run fills it.

Manual recipe (if you prefer to build it yourself; this one posts yesterday's numbers):
Posts yesterday's numbers from Apple Health (the Garmin watch writes them there) to Orbit. Build it in the Shortcuts app, run it once by hand, then add it as a **Personal Automation** at 07:00 daily (turn off "Ask Before Running"). Allow Shortcuts to read each Health category when it asks.
1. **Date** → *Current Date*, then **Adjust Date** → subtract 1 day → *Yesterday*; **Format Date** (ISO 8601, date only) → `day` (a text like `2026-10-08`); **Adjust Date** → *Yesterday* to start of day = `from`; *Current Date* to start of day = `to`.
2. For each number, **Find Health Samples** with *Start Date is after `from`* and *Start Date is before `to`*, sorted as needed, then **Calculate Statistics**:
   - Steps → Sum → `steps`
   - Active Energy → Sum → `active_kcal`
   - Resting Heart Rate → Average → `resting_hr`
   - Heart Rate → Average → `avg_hr`; Minimum → `min_hr`; Maximum → `max_hr`
   - Body Mass → Latest (in kilograms; set the unit) → `weight_kg`. Skip the step when there is no sample.
   - Sleep Analysis: the night that ends on the morning of `day`; samples whose value is *Asleep* (Core, Deep, REM, or Unspecified) → Sum of durations in minutes → `sleep_minutes`; earliest start → `sleep_start`; latest end → `sleep_end`. Count only Asleep, not "In Bed". If you can't build the sum, leave `sleep_*` out and tell me.
3. **Dictionary** with the keys above (numbers as Number values; leave out any value that has no data) → **Get Contents of URL** → `POST {ORBIT_URL}/api/v1/health/daily`
   Headers: `Authorization: Bearer {ORBIT_API_TOKEN}`, `Content-Type: application/json`, Request Body: **JSON** (the dictionary).
4. Show Notification → "Health sent" (only when you test by hand).

Rules of the endpoint: every key is optional, and `day` (empty or missing) means today in Europe/Prague; numbers may be text or decimals (rounded); a time without an offset is read as Europe/Prague; unknown keys are ignored; the same day again fills the fields it carries and keeps the rest (so a run that misses sleep does not erase an earlier post). `null`/missing means "no data", never zero. Check what arrived with `GET /api/v1/health/days` or the **Health** page. Never send 0 for a value the watch did not record.

### `Orbit Import` (share sheet, accepts files)
1. Receive **Files** from the Share Sheet
2. Get Contents of URL → `POST {ORBIT_URL}/api/v1/imports/bank`
   Headers: `Authorization: Bearer {ORBIT_API_TOKEN}`, Request Body: **Form**, fields `file` (File: Shortcut Input) and
   `bank` = `auto` (or `revolut` / `tatra`)
3. Show Notification → response `summary`, e.g. "31 new, 2 already imported, 3 flagged for splitting, 2 payments received"
   (the response also has `import_id`, `bank`, `new`, `duplicates`, `skipped`, `flagged`, `unclassified`, `proposal_ids`,
   `payments`, `payment_proposal_ids`)

Formats: Revolut "Account statement" CSV (English) and Tatra banka's internet-banking CSV (`download-DD.MM.YYYY.csv`).
Sharing the same or an overlapping file again only adds the new rows. Payments that look shared become
`split_transaction` proposals in the Inbox, with one push per import. Money received from people becomes
`payment_match` proposals: a push per payment from someone Orbit recognizes, with **Settle** (settles the matched
debts in one tap), **Choose…** and **Not a debt**; payments from unknown senders are listed in one push.
Nothing is settled without a tap.

### `Add to Orbit school` (share sheet: a page, a PDF or a note for a course)
Share a web page, a PDF or selected text from Safari, Files or Notes into Orbit; Claude then reads it for deadlines and course rules, and whatever it finds waits in your Inbox.
1. Receive: URLs, PDFs and Text from the Share Sheet.
2. Optional: Ask for Input (Text) "Course code" (e.g. `NMAT101`); skip it to leave the item in "Unsorted" on the map.
3. If the input is a **URL**: Get Contents of URL → `POST {ORBIT_URL}/api/v1/school/resources`, Request Body **JSON**: `url` (Shortcut Input), `course_code` (the answer, optional).
   If it is **text**: the same with `note` instead of `url`.
   If it is a **PDF**: `POST {ORBIT_URL}/api/v1/school/resources/file`, Request Body **Form**: `file` (File: Shortcut Input), `course_code` (optional, a text field).
   Headers: `Authorization: Bearer {ORBIT_API_TOKEN}`.
4. Show Notification → the label of the new resource (`label` in the response; `status` and `kind` too).
A link is fetched with the next daily run, or press **Check now** on the map. Pages behind a login (Moodle, Teams) are listed and never fetched.

### `Orbit Quick Debt` (optional)
Ask for person, amount, what for → `POST /api/v1/capture` with text like "Pato owes me 12 € for pizza" (reuses the capture flow → Inbox).

## Apple Calendar
Settings → Calendar → Accounts → Add Subscribed Calendar → `https://orbit.example.com/calendar/<ICS_TOKEN>.ics`
(refresh: every hour).

## iCloud CalDAV / CardDAV (server reads my calendar and contacts)
appleid.apple.com → Sign-In and Security → App-Specific Passwords → create "Orbit" → `CALDAV_PASSWORD`.
`CALDAV_USERNAME` = the Apple ID email.
`CALDAV_URL=https://caldav.icloud.com`, `CALDAV_CALENDARS` = comma-separated calendar names to watch, as Apple
Calendar shows them (case and diacritics don't matter). Only these are read, every `CALDAV_POLL_MINUTES`; the
Meetings page lists the next 14 days and has **Sync now**. A calendar name that isn't found is reported there.

Meetings are linked to people by attendee email (the person's `email` handle) or by a name or alias in the title or
location ("kafe s Paťom", "obed s Terkou"). Linked meetings get a brief push 30 minutes before (📅, also during quiet
hours) and the post-meeting push above; unlinked ones are ignored. Fix a link on the meeting's page (**Link
someone…** / ×): manual links and unlinks stick until the event's title, location or attendees change.

Contacts use the same username and password with `CARDDAV_URL=https://contacts.icloud.com`.
People → Import contacts → **Sync from iCloud** pulls all contacts, one-way and read-only. It is safe to repeat:
only empty fields are filled, and nothing in iCloud or Orbit is overwritten or deleted.
Without iCloud: Contacts app (Mac) → select all → File → Export → Export vCard…, then upload the `.vcf` on the
same page.

## Go-live checklist (deployed instance)
Work through this once after deploying ([DEPLOY.md](DEPLOY.md)), on the iPhone, with the real `https://` URL.
`dc` is `docker compose -f docker-compose.prod.yml` on the server.

**Server**
- [ ] `dc ps`: `db`, `app`, `caddy`, `backup` are all `(healthy)`
- [ ] `dc exec app python -m orbit.cli smoke-test`: every check `ok` (HTTPS, http→https redirect, HSTS, API token,
      ICS feed, push-action tokens)

**Web UI (Safari)**
- [ ] `https://<domain>` shows a valid certificate (no warning) and the login page; the password works
- [ ] Close Safari and reopen the page: you're still logged in (session cookie)
- [ ] Share → Add to Home Screen, then open it from the Home Screen

**ntfy**
- [ ] The ntfy app is subscribed to `NTFY_TOPIC` on `NTFY_URL` (with the access token if the topic is protected)
- [ ] `dc exec app python -m orbit.cli test-push`: the push arrives; its **Inbox** button opens Orbit
- [ ] `dc exec app python -m orbit.cli test-push --action`: the push arrives with **Reject**. Tap Reject without
      opening the push. The "Push test (reject me)" item then no longer shows in the Inbox (it was there before
      the tap). This proves the phone can reach `/a/{token}` over HTTPS.

**`Orbit Capture` Shortcut**
- [ ] URL `https://<domain>/api/v1/capture`, header `Authorization: Bearer <ORBIT_API_TOKEN>`
- [ ] Dictate in Slovak, e.g. "Paťo mi dlhuje 12 € za pizzu". The notification shows the summary, and the Inbox
      has the proposal with diacritics intact
- [ ] Confirm it in the Inbox: the debt/note shows on the person's page
- [ ] Run it from the Action Button / Siri as well

**`Orbit Quick Debt`** (if you use it)
- [ ] Creates an Inbox proposal the same way

**Apple Calendar**
- [ ] Subscribe to `https://<domain>/calendar/<ICS_TOKEN>.ics`: birthdays, name days and promise due dates show
- [ ] Edit a birthday in Orbit; after the next refresh it moves in Calendar, with no duplicate

**Backups**
- [ ] From the laptop: `ssh server 'cd /opt/orbit && scripts/restore.sh --check latest /dev/stdin' < orbit-backup.key`
      lists your people and debts
- [ ] The off-site bucket holds an `orbit-*.dump.age` file

**`Orbit Import`**
- [ ] Share a Tatra banka CSV (Files app → Share → Orbit Import): the notification shows "N new, … flagged for splitting"
- [ ] Share a Revolut statement CSV the same way
- [ ] Share the same file again: "0 new, N already imported"
- [ ] The "🧾 Split?" push arrives; its button opens the split screen; confirming creates the debts

**Calendar & meetings (Phase 8)**
- [ ] `CALDAV_USERNAME`, `CALDAV_PASSWORD` (app-specific) and `CALDAV_CALENDARS` set; Meetings → **Sync now** says
      "N new … (Personal, …)" with no "Calendars not found"
- [ ] Create "kafe s Paťom" (with an existing alias) in an hour: after the next sync it shows "with Patrik …"
- [ ] 30 minutes before, the 📅 brief push arrives; its **Brief** button opens the meeting page
- [ ] Move the event by an hour in Calendar: after a sync the meeting moves and the brief comes again for the new time
- [ ] 15 minutes after it ends, "📝 Anything to note about …?" arrives; **Dictate** opens `Orbit Capture` with the
      meeting id; dictate something: the Inbox proposal has "Meet with …: kafe s Paťom"; confirming logs the interaction
- [ ] Delete the event in Calendar: after a sync it's gone from the Meetings list (kept as cancelled)
- [ ] A card payment made during a linked meeting: its split screen pre-selects the people from the meeting

**Payment back (Phase 7)**
- [ ] A friend with an open money debt (and their IBAN as a handle, or linked once via "Who sent…?") sends it back;
      import the statement: the "💶 Received … from …" push arrives with **Settle**, **Choose…**, **Not a debt**
- [ ] Tap **Settle** without opening the push: the debt shows as settled "by bank payment" on their page
- [ ] A payment from someone not yet linked: "Who sent …?" → pick them → Next → Settle; the next payment from that
      account is recognized

**Reminders & digest (Phase 9)**
- [ ] Give someone a birthday today (or tomorrow and wait): at 09:00 the "🎂 … birthday today" push arrives, once;
      its **Open** button opens their page
- [ ] Tag someone `close` whose birthday or name day is 5 days away: "… birthday in 5 days" arrives at 09:00
- [ ] Lend an item with reminder days 1: the next day at 09:00 "📦 … still has your …" arrives, once
- [ ] A promise to someone due today: "✅ Due today: …" arrives at 09:00 (or right away when added after 09:00)
- [ ] Sunday 18:00: the "🗓 Your week: …" digest arrives; **Open Orbit** opens Home
- [ ] Nothing arrives during quiet hours; a week of reminders with no duplicates
- [ ] Add to Home Screen shows the Orbit icon; opened from there it runs full screen (no Safari bars)
