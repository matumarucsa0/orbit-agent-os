---
name: write-adr
description: Record a real design choice in docs/DECISIONS.md in Orbit's format (numbered entry with context, options, choice, consequences) and link it from SPEC/ROADMAP. Use when you chose between options that affect what is collected, sent to Claude, how autonomous something is, or the architecture.
---

# Write a decision record

`docs/DECISIONS.md` is numbered and append-only in spirit: do not reverse an entry without asking the owner; supersede it with a new one.

1. Read the last two entries (`tail -30 docs/DECISIONS.md`) and copy their shape; the next number is the last plus one.
   The shape is a heading `## N. Title (YYYY-MM-DD)` followed by lines `Status: accepted` (or `proposed`), `Context:`, `Decision:`,
   `Alternatives:` and `Consequences:`.
2. Write each line in plain words: what forced a choice, what was chosen and why, at least two alternatives with the real cost of each, and what becomes
   harder or must be revisited. No marketing, no personal data.
3. Cross-reference: the SPEC section that implements it (`docs/SPEC.md §2.x`) and the ROADMAP phase; reference the number from code comments only if the code
   would otherwise look wrong (`# DECISIONS #35`).
4. Anything that raises autonomy above "proposes, the owner confirms" (`docs/VISION.md` section 3), changes what is sent to Claude, or reverses an entry needs the
   owner's OK before you implement it: write the entry as "proposed", stop, and ask.
5. `make docs` (links and paths), then mention the entry in the CHANGELOG line and `STATUS.md` (decisions pending / made).
