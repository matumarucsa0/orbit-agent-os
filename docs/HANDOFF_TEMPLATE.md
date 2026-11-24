# Handoff template

Use this when a session ends with work unfinished, or when you stop and another agent (or you, later) must continue. Put the short
form in `STATUS.md` (update its sections); keep a longer handoff in the pull request description or the final message. Facts only:
every "done" names the command that proved it, every "not done" says why.

```markdown
## Handoff: <short title> (<YYYY-MM-DD>)

**Goal:** one sentence, in the owner's terms.
**State:** done / in progress / blocked. Branch or working tree: clean | uncommitted changes in <paths>.

**Done (with proof)**
- <what> : `<command>` -> <result and counts>

**Not done / open**
- <what> : <why, what is missing, who must decide>

**Decisions made** (and where recorded: DECISIONS #N, SPEC section, code comment)
- <decision> : <why>

**Decisions needed from the owner**
- <question> : <options and your recommendation>

**Files touched:** <paths, grouped by concern>
**Docs updated:** SPEC / ROADMAP / DECISIONS / CHANGELOG / `make codemap`: yes / no (why)

**How to resume**
1. `git status` and read `STATUS.md`.
2. <next command to run>
3. <next file to open and what to do>

**Traps:** <anything that cost you time: a flaky test, an env var, a rule that bit you>
**Not verified:** <what you did not run or could not check: real server, phone, live Claude, Postgres>
```

Rules: no secret values, no real names or numbers, no pasted logs longer than ten lines (name the command instead).
