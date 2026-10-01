---
name: consult-before-design
description: Before designing or changing a module, find the documents that govern it and the decisions already made about it.
---

# Consult before design

Before writing a design or changing behavior in a path:

1. `governing_docs(path)`: the documents that govern the path, highest authority first, with retractions, open conflicts and overlap hints.
2. `search_decisions(query)`: decisions about the topic. Superseded decisions are hidden by default; pass `status: "all"` to see history.
3. `search_docs(query)`: anything else relevant. Prefer higher `authority_level` hits, and treat any `retraction_summary` as current truth.

If two governing documents disagree, do not pick one silently: report it (see the propose-and-report skill) and say which one you followed and why.

If `governing_docs` returns nothing for a path you are about to change, the manifest does not place it yet. Say so, and propose a binding when you write the design.
