---
name: connect
description: Find non-obvious connections across recent notes, add the links, surface surprising pairings. Use when the owner says "connect my notes", "what links", "find patterns", or after a burst of new notes.
---

# /connect

The highest-leverage maintenance pass: a second brain is worth exactly as much as its connections.

1. **Scope:** default = notes created/modified in the last 2 weeks (when the vault is a Git repository, `git log --name-only --since="2 weeks ago"` on `30-knowledge/`, `40-sources/`, `10-projects/`; otherwise, file modification times) against the whole vault. The owner may name a different scope.
2. **Find candidate links** (delegate bulk work to the `connector` subagent):
   - Shared concepts under different vocabulary (check `aliases:`, cross-language pairs)
   - A knowledge note that answers an `## Open questions` breadcrumb elsewhere
   - Project decisions that instantiate (or contradict!) an evergreen note
   - Unresolved `[[wikilinks]]` that an existing note could satisfy under a slightly different title
3. **Apply:** add `[[wikilinks]]` in both directions where a genuine conceptual link exists (in prose, with a phrase explaining the relation — not bare link lists). Update `## Related` sections and MOCs. Never force a link; a wrong edge is worse than a missing one.
4. **Surface:** report the 3–5 most surprising pairings to the owner with one line each on why they're interesting — these are thinking prompts, and any that spark are /think candidates.
5. Append to `system/log.md`.
