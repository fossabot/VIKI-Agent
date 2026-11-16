---
name: capture
description: Zero-friction capture into 00-inbox/. Use when the owner says "note this", "capture", or dumps any thought, link, or task fragment that should not be lost.
---

# /capture <text>

Get it out of the owner's head and into the inbox in one move. Speed beats structure here.

1. Create `00-inbox/YYYY-MM-DD-HHmm-<3-5-word-slug>.md` (24h time; slug from the content, lowercase, hyphenated).
2. File content:
   ```
   ---
   captured: <ISO timestamp>
   ---
   <the owner's text, VERBATIM — never rephrase, never summarize, never "improve">
   ```
3. If the capture contains a URL, append a line `link: <url>` after the verbatim text (the /learn or /inbox flow will fetch it later — do NOT fetch now).
4. Confirm in one short line: what was captured, nothing else.

Hard rules:
- **No filing, no tagging, no questions, no follow-up suggestions.** Clarification happens later in /inbox.
- Never append to an existing inbox file; one capture = one file.
- If the owner captures mid-conversation while you're doing other work, capture it and return to the work.
