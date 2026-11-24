# Claude Code notes for Orbit

All project instructions are in [AGENTS.md](AGENTS.md); read it first. Nested `AGENTS.md` files hold the local
rules of a directory. Map of the code: [docs/CODEMAP.md](docs/CODEMAP.md). Session state: [STATUS.md](STATUS.md).
Setup of this harness (permissions, hooks, skills, subagents): [docs/AGENT_SETUP.md](docs/AGENT_SETUP.md).

Claude-specific:
- Use plan mode for anything that touches more than two files, a model, a migration or a prompt; show the plan
  (files, tests, docs) before editing. Ask when a choice changes what the owner sees, what is collected, or what is sent to Claude.
- Use the `explorer` subagent for broad searches ("where is X handled"), `code-reviewer` before you report done, and
  `security-reviewer` for anything touching prompts, auth, tokens or untrusted input. Keep the main context for edits.
- Use the skills in `.claude/skills/` (add-migration, add-prompt-with-evals, add-api-route, add-integration, run-gates,
  run-evals, seed-demo-and-screenshot, pre-publish-scan, debug-failing-test, write-adr) and the commands
  `/gates`, `/evals`, `/demo`, `/review`, `/plan-feature`.
- Model hints: a cheaper, faster model is fine for exploration, formatting and test-writing; use the strongest one
  for design, migrations, security review and prompt changes.
- `.claude/settings.json` already forces fake Claude, no scheduler and a scratch database. Never put a real key or
  `.env` value into a command, a file or a message. Run `uv run` for every Python command.
- Finish by running `make gates` (or `make changed` for a small change), then update `STATUS.md`.
