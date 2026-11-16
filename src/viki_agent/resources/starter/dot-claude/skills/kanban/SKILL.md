---
name: kanban
description: Work the team's Kanban cards in kanban/ — create a card, pick up one card, ask the checker, hand off, or block with one decision. Use when the owner invokes /kanban, asks to make or pick up a card, or a seat or routine works Kanban cards.
---

# /kanban

The team's seats coordinate through cards in `kanban/`, not by messaging each other:

- **Chief** is the front door: it turns requests into cards and does none of the work.
- **Ops** runs the standup and the weekly sweep and does none of the work.
- **Specialists** each own one domain and work their own cards.
- **The `checker` subagent** checks work in its own context and edits nothing.

A Bot, a routine or a chat takes the seat its instructions name. Card work starts from a card. A direct request from the owner in a chat or thread needs none: handle it there within your seat's role. Any other request without a card goes to Chief first.

A card is one note directly in `kanban/`, made from `_templates/card.md`: `type: card`, `status: todo | working | blocked | review | done`, `up:` the project, and `tags: [owner/<seat>]`. Its sections are `## Done when` (checkable items), `## Handoff` (one line each time a seat stops, newest first), `## Needs decision` (only while it is blocked) and `## Check` (the checker's verdict).

1. **Create a card.** Grep `kanban/` first, and update a matching open card if there is one. Otherwise copy `_templates/card.md` to `kanban/<one-line outcome>.md`: no subfolder, one card per outcome, and a name that neither `kanban/` nor `90-archive/kanban/` already uses. Set `status: todo`, `up:` the project overview as a quoted wikilink, and `tags: [owner/<seat>]`, where the seat is a slug of lowercase letters, digits and hyphens. Keep `tags:` on that one line, and put the outcome only in the `# ` heading, which is the card's title: add no `title:` line. VIKI reads only simple front matter, so a `title:` with a comma, colon or quote, or tags written as a list, leaves the card unread. Write `## Done when` as `- [ ]` items. Ask one question rather than guess an owner or an item. Reply with the card's path and owner.
2. **Pick up one card.** Take the oldest by `created:` with `status: todo` and your tag. If there is none, reply "No card" and stop. Set `status: working` and `modified:` first. Work one card at a time.
3. **Ask the checker.** Before `review`, run the `checker` subagent on the card and paste its whole verdict under `## Check`. On `Verdict: FAIL`, fix and ask again, or block.
4. **Hand off.** Whenever you stop, add `- YYYY-MM-DD · <seat> · <status> · <notes touched> · <next step> · <blockers or none>` at the top of `## Handoff`: the next seat reads the card, not your chat. Set `review` only after a PASS. `review` waits for the owner, who sets `done` or returns the card to `todo` with a note. /weekly moves done cards whole into `90-archive/kanban/`.
5. **Block with one decision.** Finish every step that can be undone, and stop at the first that cannot, such as sending, deleting, pushing, paying, or anything outside this vault. Set `status: blocked`, put the one decision and its options under `## Needs decision`, and add a handoff line. The owner writes the decision and sets `todo`.

Rules for every seat:

- A card's status coordinates seats and grants no permission.
- Fetch any fact that could change an action; do not trust memory for it.
- Write general rules, not stories: propose lessons for /retro, and do not edit `system/memory/` yourself.
- When a session changes the vault, append one line to `system/log.md`: the date, your seat, the action and the files.
