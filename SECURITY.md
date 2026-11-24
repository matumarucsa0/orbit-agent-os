# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub security advisories
("Security" tab of the repository, then "Report a vulnerability"). Do not open a public issue
for a security problem. This is a solo-maintained project: expect an acknowledgement within a
week, and a fix or a clear answer as soon as practical. There is no bug bounty.

## Supported versions

Only the `main` branch and the latest pre-release are supported. Orbit is pre-1.0 software,
built for a single user who runs it on their own server.

## Scope

In scope:
- Authentication and session handling (API bearer token, web password, signed cookies, ICS feed
  token, signed notification action tokens).
- Anything that lets one request read or change data without the intended auth.
- Prompt-injection paths that make Orbit write something without a human confirming it
  (the "Claude proposes, the user confirms" rule is the main safety property).
- Secrets or personal identifiers leaking into logs, prompts, notifications or backups.
- The Docker, compose and backup setup shipped in this repository.

Out of scope: a compromised server or host, a leaked `.env` file, denial of service against a
single-user instance, third-party services (Anthropic, ntfy, iCloud, banks), and findings that
need the owner to ignore the confirmation step.

## What data goes to Claude

Orbit sends Claude the context it needs to decide well: people and notes, money and debts,
calendar events, goals, school and fitness data, bank transaction text. Identifiers that carry
no signal (IBANs, account numbers, full phone numbers, e-mail addresses) are masked before
they are put into a prompt. Claude runs either through the Anthropic API (`ANTHROPIC_API_KEY`)
or as Claude Code on the server; without either, every feature has a no-Claude fallback.
Everything else stays on your server. If that trade-off is not acceptable to you, do not enable
the Claude features. See `docs/DECISIONS.md` (entry 7) for the reasoning.

## Threat summary

- Untrusted text (bank descriptions, calendar titles, contact notes, scraped course pages)
  reaches prompts. A poisoned string could make Claude produce a misleading proposal.
- Mitigations: structured output validated by Pydantic, every write is a proposal that a human
  confirms, the server-side worker has no tools and no network exfiltration path, secrets only
  come from the environment, argon2 password hashing, single-use signed action tokens.
- Residual risk: a misleading proposal that the owner then confirms, and, on a desktop, an MCP
  client session that has other tools next to Orbit's private context. Use such a session
  without web or shell tools.
- Every untrusted field is wrapped in `<untrusted_data>` tags and every system prompt tells the
  model to treat that text as data. `tests/test_prompt_injection.py` covers this with a rule-based
  stand-in model and a mock client; it does not measure how a real model behaves.
- The MCP server uses a read-only token (`ORBIT_MCP_TOKEN`) and writes an audit log of calls
  (see `docs/MCP.md`).
- Known gaps (honest list): see "Residual risk" in the threat model, mainly a misleading proposal
  the owner confirms and an MCP session that has network tools next to Orbit's private data.

The full model is in [docs/threat-model.md](docs/threat-model.md).
