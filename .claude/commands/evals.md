---
description: Run the offline evals (recorded cassettes, optionally the fake mode) and compare with the committed baseline; never live
---

Follow the `run-evals` skill. Run `make evals`; if $ARGUMENTS contains "fake" also run `make evals-fake`. Report pass rates per suite and whether the output equals
`evals/results/baseline-recorded.json`. Do not run live mode: it costs money and needs the owner's explicit request.
