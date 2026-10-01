---
name: propose-and-report
description: Propose which documents govern new code, and report contradictions between documents, without deciding either yourself.
---

# Propose and report

## Propose a binding

When you write a design for a module, or find a document that clearly governs code but is not placed in the manifest, call `propose_binding`:

- `target`: an existing check or area name, or a new check name.
- `reads`: the governing documents.
- `validates_against`: the code paths they govern (`src/module/`).
- `reason`: one sentence.

A human promotes or rejects it with `reasonhold candidates promote <id>` or `reasonhold candidates reject <id>`. Agents cannot promote; do not ask for a tool that would.

## Report a conflict

When two documents contradict each other about the same code, call `report_conflict` with both documents (and sections), the governed paths, the disputed claim in one sentence, and a short quote from each side as evidence.

Do not resolve it yourself. A decision resolves it later, recorded with `store_decision(..., resolves=[conflict id])`.
