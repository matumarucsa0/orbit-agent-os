# Contributing

Orbit is a single-user personal project that is open for reading and forking. Issues and small
pull requests are welcome; larger features should start as an issue, because the design is
tied to `docs/VISION.md`.

## Dev setup

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env        # fill in secrets; see comments in the file
uv run alembic upgrade head
uv run fastapi dev orbit/main.py
```

## Gates (all must pass before a pull request)

```bash
make gates     # everything below plus the docs check, the generated-docs check, the scan and the offline evals
# or one by one:
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
shellcheck -e SC1091 docker/entrypoint.sh docker/backup/*.sh scripts/*.sh .claude/hooks/*.sh
```

Faster while working: `make fast` (no endpoint or slow tests), `make changed` (only tests your change can affect),
`make one T=tests/test_x.py::test_name`. `make help` lists every target.

CI runs the same checks, plus the suite against Postgres and a backup/restore drill.

## Conventions

- Conventional commits (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`).
- Docs change in the same commit as the code: `docs/SPEC.md` for behaviour, `docs/ROADMAP.md` for
  finished items, `docs/DECISIONS.md` for a real choice between options.
- Follow the rules in [AGENTS.md](AGENTS.md); they apply to humans too (proposals not direct
  writes, integer money, UTC times, no real data in fixtures).
- Every service function gets tests; every parser gets fixture-based tests. Fixtures must be
  synthetic.

## Before publishing a fork or export

Orbit handles personal data, so check a tree before it goes public. Look for, and remove or
replace with invented values:

- real names (yours and other people's), home or work addresses, phone numbers, IBANs and
  account numbers, e-mail addresses, employer, school, gym or club names;
- real hostnames, domains and device names, and cloud project or team identifiers;
- tokens, API keys, passwords, private keys;
- files that must never be committed: `.env*` (except `.env.example`), `*.db`, `*.sqlite*`,
  `*.dump`, `*.age`, `backups/`, `.claude/settings.local.json`, signed `*.shortcut` files;
- fixtures that were derived from real exports (bank statements, health or workout data,
  contacts, school pages). Regenerate them from scratch rather than editing them.

Add your own personal strings (names, employer, school, hostnames, address fragments) to
`.pre-publish-patterns` (copy `.pre-publish-patterns.example`; the file is git-ignored, never commit
it), then run the scan. It also applies generic patterns for keys, tokens, e-mail addresses, IBANs and
phone numbers, skips the invented values listed in `.pre-publish-allow`, prints only `file:line [kind]`
and exits 1 on a hit. It runs in pre-commit and in CI (generic layer only).

```bash
scripts/scan-personal.sh                      # make scan
PRE_PUBLISH_PATTERNS=/path/outside/repo scripts/scan-personal.sh
```

Expect some false positives (documentation placeholders, fictional phone numbers and
`example.*` addresses in tests). Review every other hit. Also run `git log --format='%an %ae'`
and consider publishing a fresh history instead of an old one.
