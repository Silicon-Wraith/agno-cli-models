---
name: record-decision
description: Record the decisions a design or change makes, at approval time, with the retractions they imply.
---

# Record a decision

Record a decision when a design is approved, a trade-off is settled, an approach is rejected with reasons, or work is deferred for a reason.

Use the `store_decision` tool (or `reasonhold decide` from a shell):

- `topic`: kebab-case, specific (`queue-ordering-lifo`), not a sentence.
- `decision`: one sentence stating what now holds.
- `rationale`: the evidence and the trade-off, including numbers when there are any.
- `alternatives_considered`: what was rejected, so it is not proposed again.
- `supersedes`: for each document this decision makes wrong, `{"path": "docs/...#Section", "retraction_summary": "what now holds"}`.
- `supersedes_records`: ids of earlier decisions this one replaces (find them with `search_decisions`).
- `resolves`: ids of open conflicts this decision settles.

Check first with `search_decisions` that the decision is new. If an earlier decision said the opposite, retire it with `supersedes_records` rather than leaving two active answers.

The log is append-only. Never edit `decisions.jsonl` by hand; a correction is a new decision.
