---
name: session-bootstrap
description: Start every session from what this repository currently believes, and keep retractions straight while you work.
---

# Session bootstrap

1. Read the ReasonHold preamble at the top of the session (the SessionStart hook prints it; otherwise run `reasonhold preamble`).
2. If the preamble says the index is missing or stale, say so before relying on search results, and suggest `reasonhold index`.
3. Treat every "Superseded content" row as current truth. Where the retracted document disagrees with the summary, the document is stale.
4. Reading a file directly bypasses the retraction overlay. Before trusting a design document you opened yourself, call `retractions_for` on it.
5. Open conflicts and open candidates in the preamble are unresolved. Do not resolve them yourself; mention them when they touch your task.

## Retraction discipline

- Retract only documents that assert the thing that changed (architecture, specs, plans, guidance). Never retract `src/` or `tests/`: code is changed, not retracted.
- A retraction is not a delete. The document stays; the decision log says which part no longer holds.
- Retract the narrowest scope that is wrong: `docs/x.md#Section` rather than the whole file when only a section is stale.
