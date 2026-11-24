# Documentation index

One line per file. Agents: start with [CODEMAP.md](CODEMAP.md); everything else is looked up when needed (the big files are
searched with grep, not read whole). Rules for contributors are in [../AGENTS.md](../AGENTS.md).

## For working on the code
| File | What it is |
|---|---|
| [CODEMAP.md](CODEMAP.md) | Module map, data flows (capture, bank import, Claude call, ICS, MCP), jobs, test areas, extension recipes. Tables are generated. |
| [ROUTES.md](ROUTES.md) | Generated: every HTTP route, its handler and what guards it. |
| [CONFIG.md](CONFIG.md) | Generated: every setting (environment variable, default, meaning) and every CLI command. |
| [TESTMAP.md](TESTMAP.md) | Generated: which `orbit` modules each test file imports. |
| [AGENT_SETUP.md](AGENT_SETUP.md) | How the agent harness is wired: instructions, permissions, hooks, skills, subagents, fake Claude, evals; use with Claude Code, Codex, Cursor. |
| [HANDOFF_TEMPLATE.md](HANDOFF_TEMPLATE.md) | Form for ending a session with unfinished work; the short form lives in `../STATUS.md`. |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Short architecture: one request end to end, layers, Claude backends. |

## Product and decisions
| File | What it is |
|---|---|
| [VISION.md](VISION.md) | North star: shared context, module contract, autonomy ladder. Every design must fit it. |
| [SPEC.md](SPEC.md) | What is built and specified: features (section 2), data model (3), API (4). About 1800 lines: read the section you need. |
| [ROADMAP.md](ROADMAP.md) | Phased plan with acceptance criteria and the open verification boxes. |
| [DECISIONS.md](DECISIONS.md) | Numbered decision records (why things are the way they are). Do not reverse one without asking. |

## Claude, quality and safety
| File | What it is |
|---|---|
| [PROMPTS.md](PROMPTS.md) | Prompt versions, how to change a prompt, the prompt changelog. |
| [EVALS.md](EVALS.md) | The eval suites, graders, modes (recorded, fake, live), honest limits. |
| [OBSERVABILITY.md](OBSERVABILITY.md) | OpenTelemetry spans, cost in euros, monthly budget. |
| [threat-model.md](threat-model.md) | Assets, trust boundaries, prompt-injection paths, mitigations, residual risks, what is tested. |
| [MCP.md](MCP.md) | The read-only MCP server for Claude Code: scope, token, input checks, audit log. |

## Running it
| File | What it is |
|---|---|
| [DEPLOY.md](DEPLOY.md) | Deploying with Docker, Caddy, backups, the Claude worker. |
| [SHORTCUTS.md](SHORTCUTS.md) | iOS Shortcuts and ntfy contract, and the go-live checklist. |
| `shortcuts/` | Script that builds the Health Shortcut. `images/` holds the README screenshots and the architecture diagram. |
