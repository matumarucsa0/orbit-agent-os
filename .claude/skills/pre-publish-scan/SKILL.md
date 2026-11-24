---
name: pre-publish-scan
description: Scan the Orbit tree for personal data and secrets before publishing, exporting or committing a fixture (keys, tokens, e-mail, IBAN, phone, private strings, forbidden files). Use before any push to a public place and after adding fixtures or docs.
---

# Pre-publish scan

1. `make scan` (= `scripts/scan-personal.sh`). Output is `file:line [kind]`; it never prints the matched text. Exit 1 on any hit.
2. The owner's private strings live in `.pre-publish-patterns` (git-ignored, one regex per line; template `.pre-publish-patterns.example`) or in
   a file outside the repo: `PRE_PUBLISH_PATTERNS=/path/file scripts/scan-personal.sh`. You cannot read that file (denied); do not ask for its contents.
3. A hit is real until proven otherwise: open the line, replace the value with an invented one (`example.com`, documentation IBAN, `+44 7700 900xxx`),
   rescan. Only a value that is already synthetic goes into `.pre-publish-allow` (regex on the matched text, one line, with a reason comment).
   `scan:ok` on a line is for the owner's credit line only.
4. Also check: `git ls-files | grep -E '(^|/)(\.env|.*\.(db|dump|age|shortcut))$'` finds nothing but `.env.example`; `git log --format='%an %ae'` shows the
   no-reply identity; screenshots in `docs/images/` and fixtures in `tests/fixtures/` contain no real names or numbers.
5. Never paste a real export "just to test", not even edited. Regenerate fixtures from scratch.
CI runs the generic layer (`agent-tooling` job); the private layer only runs where the patterns file exists.
