# Deploying Orbit

Production runs as one compose project, [`docker-compose.prod.yml`](../docker-compose.prod.yml):

| Service  | What it does                                                                                   |
|----------|------------------------------------------------------------------------------------------------|
| `db`     | Postgres 16, data in the `pg-data` volume. Not reachable from outside.                         |
| `app`    | Orbit. Migrates the database on start. Only Caddy can reach it.                                |
| `caddy`  | HTTPS on 80/443 with an automatic Let's Encrypt certificate, http→https redirect, HSTS.        |
| `backup` | Nightly `pg_dump` → `age` encryption → local copy in `./backups` + off-site copy via `rclone`. |

Every command below runs on the server, in the repo directory, and uses this alias:

```bash
alias dc='docker compose -f docker-compose.prod.yml'
```

## 1. Server
- A small Linux VPS is enough (1 GB RAM). Install Docker Engine with the compose plugin.
- DNS: an `A` (and `AAAA` if you have IPv6) record for your hostname, e.g. `orbit.example.com`, pointing at the server.
- Firewall: open only 22, 80 and 443 (TCP) and 443/UDP, e.g. `ufw allow 22,80,443/tcp && ufw allow 443/udp && ufw enable`.
- `git clone <repo> /opt/orbit && cd /opt/orbit`

### Google Cloud free tier (e2-micro)
Google's always-free tier includes one `e2-micro` VM (2 shared vCPUs, 1 GB RAM) with a 30 GB standard disk, but only
in `us-west1`, `us-central1` or `us-east1`. Anything outside those limits is billed, so follow these steps exactly and
set a budget alert. Google changes the free tier from time to time; check
[the current terms](https://cloud.google.com/free/docs/free-cloud-features#compute) before you start.

On your laptop, with the [gcloud CLI](https://cloud.google.com/sdk/docs/install) (`brew install --cask gcloud-cli`):
```bash
gcloud init                                      # log in, pick or create a project
gcloud services enable compute.googleapis.com
gcloud config set compute/zone us-central1-a

# Fixed IP, so DNS never has to change
gcloud compute addresses create orbit-ip --region=us-central1
gcloud compute addresses describe orbit-ip --region=us-central1 --format='value(address)'

gcloud compute instances create orbit \
  --machine-type=e2-micro \
  --image-family=debian-12 --image-project=debian-cloud \
  --boot-disk-size=30GB --boot-disk-type=pd-standard \
  --address=orbit-ip --tags=orbit-web

# The VPC firewall replaces ufw: SSH (22) is already open on the default network, this adds the web ports
gcloud compute firewall-rules create orbit-web \
  --allow=tcp:80,tcp:443,udp:443 --target-tags=orbit-web
```
- **Disk:** keep `pd-standard` and at most 30 GB. The console's default disk type (`pd-balanced`) isn't free.
- **Budget alert:** Billing → Budgets & alerts → a budget of €1 with an email alert. The free tier doesn't include
  everything; public IPv4 addresses, for example, can show up on the bill. Check the billing report after the first
  few days: it should say €0.00.
- **Egress:** 1 GB a month of outbound traffic is free. Orbit uses a small fraction of that, including the nightly
  off-site backup. Inbound traffic, like pulling images, is free.
- **DNS:** point the `A` record at the address printed above. Skip `AAAA`, because the VM has no IPv6.

On the VM (`gcloud compute ssh orbit`):
```bash
# 1 GB RAM isn't enough to build the image next to Postgres. Add 2 GB of swap.
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# Docker Engine + compose plugin (Docker's official install script for Debian)
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"                  # log out and back in for this to apply

sudo apt-get install -y git
sudo mkdir -p /opt/orbit && sudo chown "$USER" /opt/orbit
git clone <repo> /opt/orbit && cd /opt/orbit
```
For a private repo, add a read-only [deploy key](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys)
(`ssh-keygen -t ed25519` on the VM, then paste the public key into the repo settings) and clone over SSH.
The first `dc up -d --build` on this VM takes a few minutes. Then carry on with step 2.

## 2. Backup key (on your laptop, not the server)
```bash
age-keygen -o orbit-backup.key      # macOS: brew install age · Windows: winget install FiloSottile.age
```
It prints `Public key: age1…`, which goes into `BACKUP_AGE_RECIPIENT`. The server only ever holds the public key, so
a stolen server or storage bucket can't decrypt the backups. **Keep `orbit-backup.key` in your password manager,
plus an offline copy. Without it, no backup can be restored.**

## 3. Off-site storage
Backups are copied with rclone to `BACKUP_TARGET`, e.g. `offsite:orbit-backups`. The `offsite` remote is configured
with `RCLONE_CONFIG_OFFSITE_*` variables in `.env`, so there's no rclone config file. `.env.example` has examples
for Backblaze B2 and an SFTP storage box. Any [rclone backend](https://rclone.org/overview/) works the same way.
Use a bucket or account that holds only these backups, and give its key only the permissions it needs.

## 4. `.env`
```bash
cp .env.example .env && chmod 600 .env
```
- `ORBIT_DOMAIN=orbit.example.com`, `ORBIT_URL=https://orbit.example.com`
- `ORBIT_API_TOKEN`, `ORBIT_SECRET_KEY`, `ICS_TOKEN`, `POSTGRES_PASSWORD`: generate each with
  `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. These are URL-safe, which matters for
  `POSTGRES_PASSWORD` because it goes into the database URL.
- `ORBIT_WEB_PASSWORD_HASH`: on your laptop, `uv run python -m orbit.cli hash-password`, then paste the printed line
  with its single quotes.
- `NTFY_TOPIC` (+ `NTFY_TOKEN`) and the iCloud settings as needed (see [SHORTCUTS.md](SHORTCUTS.md)). Leave `ANTHROPIC_API_KEY`
  and `ANTHROPIC_MODEL` empty: production uses Claude Code on the server (step 10), not the API.
- `BACKUP_AGE_RECIPIENT`, `BACKUP_TARGET`, `RCLONE_CONFIG_OFFSITE_*` (steps 2–3).
- Leave `DATABASE_URL` as it is: the production compose file sets its own.

Check the off-site remote before the first start:
```bash
dc build backup && dc run --rm --no-deps backup rclone lsd offsite:
```

## 5. Start
```bash
dc up -d --build
dc ps                                            # all four services "(healthy)"
dc exec app python -m orbit.cli smoke-test       # checks HTTPS, redirect, API token, ICS feed
```
On first start, Caddy gets the certificate within a few seconds. If it fails, `dc logs caddy` shows why; usually DNS
isn't pointing at the server yet, or port 80 is blocked. The backup service takes its first backup right away.

## 6. Move your data over (once, before using the new instance)
Your development data lives in `orbit.db` (SQLite). `copy-db` copies all of it into the new, still empty Postgres
database. It refuses if the target already has data.
```bash
# laptop: bring orbit.db to the latest schema, then copy it to the server
uv run alembic upgrade head
scp orbit.db server:/opt/orbit/import.db

# server
dc stop app
dc run --rm -v ./import.db:/import/orbit.db:ro app python -m orbit.cli copy-db sqlite:////import/orbit.db
dc up -d app
shred -u import.db                               # plain-text copy of personal data
```
Queued (unsent) pushes come along and go out from the server. Pushes that were already sent keep their dedupe
keys, so they're never sent again.

## 7. Backups
- **Schedule:** the first backup runs when the service starts, then one runs every day at `BACKUP_TIME` (local time).
  A missed night is caught up on the next start.
- **Where:** `./backups/orbit-<UTC time>.dump.age` on the server (kept `BACKUP_KEEP_LOCAL_DAYS`), and the same
  file in `BACKUP_TARGET` (kept `BACKUP_KEEP_DAYS`). Everything is encrypted, and the unencrypted dump never
  touches the disk.
- **Monitoring:** a failed run sends an ntfy push; during quiet hours ntfy delivers it when they end. `dc ps` shows
  `backup` as `unhealthy` once the last good backup is more than 25 hours old. `dc logs backup` shows each run.
- **Manual backup** (e.g. before an update): `dc exec backup backup.sh`

### Restore
The key never has to be stored on the server. Pipe it in from your laptop:
```bash
# Drill: restore the newest off-site backup into a scratch database, list what's in it, drop it again.
# The live data isn't touched. Do this once after setup, then every few months.
ssh server 'cd /opt/orbit && scripts/restore.sh --check latest /dev/stdin' < orbit-backup.key

# Real restore: stops the app, swaps the backup in, starts the app again (it migrates older backups forward).
ssh server 'cd /opt/orbit && scripts/restore.sh --yes latest /dev/stdin' < orbit-backup.key
```
Instead of `latest`, you can name a file in `BACKUP_TARGET` (`orbit-20260923T013000Z.dump.age`) or a local copy
(`/backups/orbit-….dump.age`). A real restore keeps the previous database as `orbit_before_restore_<time>`.
Once everything looks right, drop it with `dc exec db dropdb -U orbit orbit_before_restore_<time>`.

**New server (disaster recovery):** do steps 1, 4 and 5 with the same `.env` (you can pick a new `POSTGRES_PASSWORD`),
then run the real restore. Caddy gets a new certificate by itself.

`scripts/backup-drill.sh` runs the whole backup and restore cycle in a throwaway compose project. CI runs it on every
push. It's safe to run on the server too, because it doesn't touch the real stack.

## 8. Updating
```bash
git pull
dc exec backup backup.sh                         # optional: a fresh backup first
dc up -d --build                                 # migrations run when the app starts
dc exec app python -m orbit.cli smoke-test
```

## 9. Everyday commands
```bash
dc ps                                            # health of every service
dc logs -f app                                   # app log (Caddy keeps no access log)
dc exec app python -m orbit.cli test-push        # push to the phone
dc exec app python -m orbit.cli test-push --action   # push with a Reject button: tests the phone → server path
dc exec db psql -U orbit orbit                   # SQL shell
```
Times and intervals (reminders, digest, review, polling, quiet hours, retention) are edited in the app at `/settings/schedule`,
with a Run now button per job. What you save there beats the same value in `.env`; **Reset** puts the `.env` value back. The
backup time and the systemd timers are not editable there (they run outside the app): change them in `.env` or the unit file.

## 10. Claude Code worker (your Claude subscription, no API key)
Production has no `ANTHROPIC_API_KEY` (`DECISIONS.md` #10 and #11). Every question Orbit has for Claude (capture, briefs, meeting
linking, message drafts, bank classification, the weekly review) goes into a queue, and one **worker** on the VM answers them
with Claude Code (SPEC §2.18, §2.19). The worker runs `claude -p` with **no tools allowed**, so a hostile note cannot make it
act. Orbit checks every answer like an API answer, and everything Claude suggests still waits in the Inbox for your tap. If the
worker is away, logged out or rate limited, Orbit falls back to plain modes (a note draft, no summary line, rules-only bank
classification) and tells you with a push. The page `/ops/claude` shows the worker, the queue and the last jobs. Run everything
below on the VM.

1. **A worker token.** It can call only the review and worker endpoints.
   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
   Put it in `/opt/orbit/.env` as `ORBIT_REVIEW_TOKEN=…`. Leave `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL` empty (remove them if
   they are set). `CLAUDE_BACKEND=auto` then picks the worker. Then `dc up -d app`.
2. **An unprivileged user for Claude Code.**
   ```bash
   sudo useradd --system --create-home --shell /bin/bash orbit-review
   ```
3. **A swap file** (the VM has 1 GB; Claude Code needs headroom). Skip if `swapon --show` already lists one.
   ```bash
   sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
   ```
   ```bash
   echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
   ```
4. **Install Claude Code as that user** with Anthropic's installer from the Claude Code docs, then check `claude --version`.
   Note the version: Orbit shows it on `/ops/claude`, and it only changes when you update it on purpose.
   ```bash
   sudo -iu orbit-review
   ```
5. **A long-lived login for it** (needs your Claude subscription). As `orbit-review`, run `claude setup-token`, open the URL it
   prints in a browser on your laptop, and copy the token it gives back.
6. **The settings file** `/etc/orbit-review.env` (owned by root, group `orbit-review`, mode 640):
   ```
   ORBIT_URL=https://orbit.example.com
   ORBIT_REVIEW_TOKEN=<the token from step 1>
   CLAUDE_CODE_OAUTH_TOKEN=<the token from step 5>
   CLAUDE_BIN=/home/orbit-review/.local/bin/claude
   # optional: a model for everything, and a cheaper one for briefs, meeting links and bank classification
   WORKER_CLAUDE_MODEL=
   WORKER_CLAUDE_MODEL_BACKGROUND=
   # 1 (default): a second loop only for captures and drafts, so a long school read or report never blocks them.
   # It can run two Claude Code processes at once; set 0 on a VM that runs out of memory.
   WORKER_INTERACTIVE_SLOT=1
   ```
   ```bash
   sudo chown root:orbit-review /etc/orbit-review.env && sudo chmod 640 /etc/orbit-review.env
   ```
7. **Self-test.** It makes the real Claude Code call on a tiny question and prints `ok`.
   ```bash
   sudo -u orbit-review env ORBIT_REVIEW_ENV=/etc/orbit-review.env python3 /opt/orbit/scripts/claude_worker.py --selftest
   ```
8. **Start the worker.** If the older `orbit-review.timer` is installed, turn it off first: the worker makes the weekly review too,
   and only one process should run Claude on the VM.
   ```bash
   sudo systemctl disable --now orbit-review.timer 2>/dev/null; true
   ```
   ```bash
   sudo cp /opt/orbit/deploy/orbit-claude-worker.service /etc/systemd/system/
   ```
   ```bash
   sudo systemctl daemon-reload && sudo systemctl enable --now orbit-claude-worker
   ```
   `journalctl -u orbit-claude-worker -f` shows each job. Open `/ops/claude`: the worker shows as running within a minute.
9. **Try a capture.** Dictate something with the Shortcut. The reply comes at once (the text is saved as a draft); a few seconds
   later the draft in the Inbox is replaced by the extracted items and you get a push "📥 Capture ready".

How it behaves:
- **Waiting.** A message draft waits up to 45 s for the worker (a capture does not wait: it is saved first and answered afterwards), briefs, meeting links and bank classification up to 120 s
  (`CLAUDE_WAIT_*` in `.env`). After that the request falls back; a capture is still upgraded later if its draft is untouched.
- **Weekly review.** When the queue is empty, the worker is handed the review from Sunday 19:00 (until Wednesday night, for a missed
  one). It has a second slot reserved for captures and drafts (`WORKER_INTERACTIVE_SLOT`), so a review or a long school read
  (a few minutes) no longer holds a capture back; both can run at once, which uses more of the VM's memory and of the
  subscription's usage limit. With the slot off, a capture waits for the job before it. A failed review is retried by the schedule (3 attempts). To make this week's now, run
  `/opt/orbit/scripts/weekly-review.sh --force` as `orbit-review` (the earlier script still works for that).
- **Login expired.** One push a day with the fix: run `claude setup-token` as `orbit-review` and update
  `CLAUDE_CODE_OAUTH_TOKEN` in `/etc/orbit-review.env`, then `sudo systemctl restart orbit-claude-worker`.
- **Rate limit.** The worker pauses (about 15 minutes), waiting requests fall back at once, background work waits in the queue.
- **Worker down.** One push a day when jobs wait and the worker has not been seen for 10 minutes.
- **Privacy.** The prompt and answer of each job are kept 7 days (`CLAUDE_JOB_RETENTION_DAYS`) for debugging, then cleared.
- **Back to the API.** Set `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL` (or `CLAUDE_BACKEND=api`) and `dc up -d app`; nothing else changes.

## 11a. Course pages and PDFs (school map)
- **Nothing to configure.** Add a teacher's page, a PDF, a GitHub repository or a note to a course on `/school/map` (or with the
  share-sheet Shortcut in `docs/SHORTCUTS.md`). Orbit fetches each kept page once a day from `SCHOOL_SITES_HOUR` (default 07:00),
  and Claude reads what is new or changed, up to `SCHOOL_READS_PER_DAY` (default 20) a day. Both values can be changed on `/settings/schedule`.
- **Uploaded PDFs** live in the `school-files` volume (`/data/school/files`). They are **not** in the nightly database backup;
  the text Orbit extracted from them is (it is in the database). After a restore, a PDF's original is gone but its text, rules and
  proposals are not. Back the volume up yourself if the originals matter.
- **Safe by design.** Orbit only does GET, one site at a time and 5 s apart, obeys `robots.txt`, never fetches local addresses,
  and never fetches a page behind a login. Claude never fetches anything: it reads text Orbit gives it.
- **Claude worker.** With the worker, reads are jobs of kind `school_read`; restart the worker after updating (`scripts/claude_worker.py` changed).

## 11. School feeds (SIS, ReCodEx, Owl)
The school module (SPEC §2.20) reads your timetable, exams and deadlines from token feeds. Orbit never logs in to SIS, ReCodEx or
Owl and only makes GET requests, one at a time, 2 seconds apart, with the user agent `Orbit/<version> personal-assistant`. Every
setting is optional: without them `/school` says "SIS feed not set up" and deadlines you add by hand still work. Add the lines you
have to `/opt/orbit/.env` (the names are in `.env.example`), then `dc up -d app`. The first sync runs at once, then every
`SIS_POLL_MINUTES` (default 60); `/school/status` shows each source and has **Sync now**.

| Setting | Where to get it | Gives |
|---|---|---|
| `SIS_TIMETABLE_KEY` | SIS → Rozvrh → tab **Export** → enable the iCal export; copy the value of `auth_key=` from the link | lessons |
| `SIS_ICAL_TOKEN` | SIS → Harmonogram → **Export do kalendáře** (`harmonogram?do=exporty`); copy the value of `token=` from any `ical.php` link | registered exam terms, SIS events (and lessons when there is no timetable key) |
| `RECODEX_ICAL_TOKEN` | ReCodEx → your profile (Edit user) → box "Deadlines Export to iCal"; the token at the end of the link | assignment deadlines |
| `RECODEX_API_TOKEN` | ReCodEx → your profile → generate a token with scope **Read-only**, 356 days | points, second deadlines, exact course links |
| `OWL_SESSION_COOKIE` | log in to owl.example.edu in Chrome → DevTools → Application → Cookies → `owl.example.edu` → value of `owl_session` | Owl deadlines, points, course rules |

- **Secrets.** These values are passwords in a URL or a cookie. Keep them only in `.env`. Orbit never logs them, never shows them
  and removes them from every error message it stores.
- **Rotation breaks the feed.** "Vygenerovat nový klíč" on SIS (or a new ReCodEx token) makes the old value stop working. Orbit
  then sends one push a day ("SIS calendar feed stopped working", "ReCodEx stopped answering"); paste the new value into `.env`
  and run `dc up -d app`.
- **ReCodEx token expiry.** The read-only token lasts at most 356 days and cannot be extended. Orbit pushes 14 days before it ends.
- **Owl cookie expiry.** When the cookie dies, Owl sends Orbit to its login page and you get one push a day "Owl login expired".
  Copy a fresh `owl_session` value. Its real lifetime is not known yet: note the date when the first expiry push comes.
- **Owl courses Orbit cannot place** (a teacher's own id like `la1-svobodova` whose name matches no course or several) are listed on
  `/school/status` with a course picker. Your pick is kept on every sync.
- **Apple Calendar.** Orbit's own calendar feed does not contain lessons. To see them in Apple Calendar, subscribe to the SIS feed
  link directly (Settings → Calendar → Accounts → Add Subscribed Calendar).
- **Pushes.** Tomorrow's lessons at `SCHOOL_EVENING_TIME` (20:00), exams 7 days and 1 day before, deadlines 3 days and 1 day
  before (both at 18:00), and a batched "schedule change" push at most once an hour. `SCHOOL_PUSHES_ENABLED=false` turns them off.
