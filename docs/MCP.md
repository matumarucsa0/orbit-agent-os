# The Orbit MCP server

`orbit/mcp_server.py` lets Claude Code on your own machine read Orbit's context snapshot (people,
ledger, meetings, promises, goals, school, fitness, health). It is a local stdio process, started by
Claude Code from `.mcp.json`. It does not open Orbit's database: it calls `GET /api/v1/context` over
HTTP(S) and shows what the server's masked context provider returns.

## Scope

- **One tool**, `get_context(preset, start, end, person_id)`. It is read-only. There are no write
  tools, no resources and no prompts, and the tool list is fixed in code.
- **A token that opens one route.** `ORBIT_MCP_TOKEN` is accepted only on `GET /api/v1/context`
  (checked against the matched endpoint, not the URL). Every other route answers 401 to it:
  `tests/test_mcp_hardening.py` lists all routes from the app's OpenAPI schema and calls each one
  with the MCP token. The API token (the one Shortcuts use, which can write) is not needed on the
  Mac. Compared in constant time.
- **Fallback.** If only `ORBIT_API_TOKEN` is set the server still starts, uses it, and prints and
  logs a deprecation warning. Create a dedicated token instead:
  `python -c "import secrets; print(secrets.token_urlsafe(48))"`, put it in the server's `.env` as
  `ORBIT_MCP_TOKEN` and in your shell as `ORBIT_MCP_TOKEN` (not in `.mcp.json`, which only
  interpolates it).

## Input checks (in the MCP layer, before any request)

| Argument | Rule |
|---|---|
| `preset` | one of `this_week`, `next_7_days` (an allow-list) |
| `start`, `end` | both or neither; `YYYY-MM-DD`, at most 32 characters, a real date between 2000 and 2100; `end` not before `start`; at most 92 days (the API's own limit) |
| `person_id` | an integer from 1 to 2147483647 (booleans and strings are refused) |
| response | more than 2 MB is refused with a hint to narrow the window |

A refused call never reaches Orbit and its input is not written to the log.

## Audit log

Each call, accepted or refused, appends one JSON line to `ORBIT_MCP_AUDIT_LOG` (default
`~/.local/state/orbit/mcp-audit.jsonl`): `ts`, `tool`, `args_sha256`, `args` (the validated values
only), `status` (`ok`, `rejected`, `error`), `http_status`, `bytes`, `duration_ms`, and a short
`reason` for refusals. It never holds the token, response text or rejected input. The file is
opened append-only with mode 0600 and rotated by size (`ORBIT_MCP_AUDIT_MAX_BYTES`, default 1 MB,
three backups `.1` to `.3`). A failed write is reported on stderr and does not stop the call: this
is a record, not a gate. Append-only is a property of how the server writes, not a guarantee against
someone with write access to the file; ship it elsewhere if that matters.

## Tool poisoning and rug pulls

The tool name, description and schema are static strings in the code you installed. There are no
dynamic tools, no tool descriptions fetched from Orbit or any remote host, and nothing in the
server changes them at runtime. The transport is stdio to a process Claude Code starts itself, so
no other process can register tools into the session through this server. A change to the tool
surface is a code change that shows up in review. The server cannot defend against a tampered
checkout on your own machine.

## The lethal trifecta on the desktop

The snapshot is private data. It also carries text from outside (calendar titles, bank row
descriptions, contact and interaction notes), which an attacker can influence, for example with a
calendar invite. If the Claude Code session that reads it also has tools that can send data out
(web fetch, a browser, a shell with network access, other MCP servers), a poisoned entry could try
to make the model leak what it read. On the server side Orbit breaks this triad: the worker has no
tools and the API path returns JSON only. On the desktop it is up to the session.

- Use `get_context` in a session without network tools or other MCP servers, or with their
  permissions set to ask.
- Do not paste the output into a tool-rich session.
- Treat what you see in the log as the record of what was read: look for unexpected `person_id`
  or wide windows.
- Over HTTP the token travels as a bearer header: use HTTPS for `ORBIT_URL` unless it is localhost.

See also `docs/threat-model.md` for the server side.
