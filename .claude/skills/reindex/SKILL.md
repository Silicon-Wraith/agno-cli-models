---
name: reindex
description: When to run reasonhold index, what a full re-index means, and what to do when answers are stale.
---

# Re-index

- `reasonhold index` updates this branch's index incrementally. It switches to a full re-index by itself after a merge, a rebase or reset, a new retraction or retired decision, or a change to the manifest's authority ladder.
- `reasonhold index --full` drops and rebuilds this branch's collection. Use it after changing the embedding model (`ModelMismatch` says so) or when an index looks wrong.
- Queries never index. A stale answer carries a warning naming the cause; an exact "nothing found" from a stale index is refused (`IndexStale`). Run `reasonhold index`, then ask again.
- Each branch has its own collection. A new branch starts with a full index (about 90 seconds for a repository of Ariadne's size).
- `reasonhold gc` lists collections for branches that no longer exist and drops them after confirmation.
- `reasonhold index` also applies mechanical manifest curation (renames and deletions) first; review those edits with `git diff` before committing.
