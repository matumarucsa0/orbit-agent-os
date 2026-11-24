---
description: Plan a feature or change before touching code: files, tests, docs, risks, questions for the owner
---

Plan: $ARGUMENTS

1. Read `STATUS.md`, `docs/CODEMAP.md`, the nested `AGENTS.md` of the directories involved, and the matching SPEC/ROADMAP/DECISIONS sections. Use the `explorer` subagent for broad searches.
2. Check the request against `docs/VISION.md`: shared context, module contract, proposals for writes, autonomy ladder. If it needs a new module, data source, or changes what is collected or sent to Claude, the plan is a `docs/SPEC.md` section with open questions: write that and stop for the owner's OK.
3. Output (do not edit code yet): files to change (with the function or class), the migration if any, tests to add (names and fixtures), docs to update (SPEC, ROADMAP, DECISIONS, CHANGELOG, `make codemap`), risks and rules involved (cite AGENTS.md rule numbers), questions for the owner, and the gate commands you will run.
4. Wait for approval, then implement one concern at a time, running `make changed` after each step.
