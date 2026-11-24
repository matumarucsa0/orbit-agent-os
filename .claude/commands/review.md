---
description: Review the current diff with the code-reviewer subagent, and the security-reviewer when prompts, auth, tokens or untrusted input are touched
---

Run `git status --short` and `git diff HEAD --stat`. Launch the `code-reviewer` subagent on the diff. If the diff touches `orbit/integrations/claude.py`, prompts, `orbit/api/`, `orbit/web/auth.py`,
tokens, uploads, the MCP server or the worker, also launch `security-reviewer`. Merge both reports into one list ordered by severity, drop duplicates, and say which findings you will fix.
$ARGUMENTS may narrow the scope (a path or a topic).
