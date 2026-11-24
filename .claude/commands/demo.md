---
description: Boot the demo (fictional persona, fake Claude, throwaway database), check it, and say what you saw
---

Run `make demo` (it seeds a temporary database, starts the server on port 8765 with `ORBIT_CLAUDE_MODE=fake`, checks `/healthz` and `/api/v1/context`, and stops the server).
Report the lines it printed. If $ARGUMENTS names a page or feature, follow the `seed-demo-and-screenshot` skill to open it and describe what is on screen.
Never use a real database or key.
