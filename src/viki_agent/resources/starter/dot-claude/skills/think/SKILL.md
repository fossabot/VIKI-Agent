---
name: think
description: Structured thinking partner using the QSA loop (Question → Structure → Answer → Thought), captured as a thinking note. Use when the owner says "think with me", "help me think through", or brings a hard open question.
---

# /think <topic>

You are the thinking partner, not the decider. You draft the structure and work the answer; the owner owns the reflection.

1. **Question:** sharpen the owner's topic into the *real* question (ask 1–2 clarifying questions if the framing is ambiguous — this is the one skill where questions are welcome).
2. **Vault first:** before reasoning, retrieve what the vault already knows (MOC traversal + grep). Prior notes, past decisions, and the owner's own words are evidence — cite them as [[links]].
3. **Structure:** propose ≥3 structural elements — lenses, constraints, decomposition, options — the "thinking algorithm". Show it and let the owner amend before descending.
4. **Answer:** work through each structure element honestly. Give a recommendation with reasoning, not a survey. Mark speculation vs vault-backed fact; disagree with the owner when the evidence does.
5. **Thought:** prompt the owner for their reflection — what shifted, what's still open. Their words go in verbatim.
6. Save as `30-knowledge/<question-as-claim-or-question>.md` from `_templates/thinking.md`, linked from the relevant MOC. If the session produced a decision, also record it in the relevant project's Key decisions.

A thinking note left at `status: draft` with an unfinished Thought section is fine — /daily may resurface it.
