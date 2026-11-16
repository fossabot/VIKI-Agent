---
name: research
description: Deep-dive a topic on the web and synthesize it into evergreen knowledge notes with confidence-marked claims. Use when the owner says "research X", "find out about", or asks a question the vault can't answer.
---

# /research <topic>

1. **Vault first:** check what's already known (MOCs, grep). Report it; the owner may already have the answer, or the research should build on it.
2. **Sweep:** delegate to the `researcher` subagent — multiple searches, multiple independent sources, follow the primary sources. Prefer official docs/repos/papers over blog summaries; note publication dates (facts go stale).
3. **Sources layer:** each substantive source becomes a `40-sources/` literature note (per /learn rules — raw copy optional for minor sources, mandatory quotes for load-bearing claims).
4. **Synthesis layer:** write or update `30-knowledge/` notes — claim-shaped titles, every claim either cited to a source note or explicitly marked as the assistant's inference with `confidence:`. Contradictions between sources are recorded as contradictions, not silently resolved.
5. Wire into the relevant MOC; leave unresolved threads as `## Open questions` breadcrumbs.
6. Report back: direct answer first, then what was added to the vault, then what remains uncertain.

Scope control: a /research run is 30–90 minutes of agent work, not a dissertation. If the topic is genuinely large, propose splitting it and let the owner pick the first part.
