---
name: daily
description: Open or create today's daily note and deliver the morning brief — open tasks, loose ends, one resurfaced note. Use when the owner says "daily", "brief me", "start my day".
---

# /daily

1. Open or create `60-calendar/YYYY/YYYY-MM-DD.md` from `_templates/daily.md`.
2. Build the **Brief** section:
   - **Tasks:** scan `10-projects/*/tasks.md` for unchecked items; list due/overdue (📅 ≤ today) first, then up to 5 next actions across active projects.
   - **Loose ends:** skim yesterday's daily note Log for unfinished threads.
   - **Inbox pressure:** count of items in `00-inbox/` and age of the oldest (nudge toward /inbox if >3 items or >3 days).
   - **One resurfaced note:** pick one older note from `30-knowledge/` that connects to something currently active (use the MOCs; explain the connection in one line). Rotate — grep `system/log.md` to avoid repeating recent picks.
3. Deliver the brief conversationally, then leave the note ready for the day's Log entries.
4. Through the day, "log this" or timestamps go into the **Log** section, append-only.

Keep the brief under ~20 lines. It's a morning glance, not a report.
