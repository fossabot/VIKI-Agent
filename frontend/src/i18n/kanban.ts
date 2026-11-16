/** `kanban.*`: the Kanban page's strings (KanbanView and App's Kanban fallback); its missing-skill line is Bots' `READINESS_TEXT.missing`. */

import { defineCatalog } from "./defineCatalog";

export const catalog = defineCatalog({
  en: {
    "kanban.empty":
      "No cards yet. A card is a note directly in this vault's kanban/ folder with type: card, status: todo, working, blocked, review or done, and an owner/<seat> tag.",
    "kanban.title": "Kanban",
    "kanban.intro":
      "Cards from this vault's kanban/ folder, by status. Kanban only reads them: Bots move cards with /kanban. Select a card to open its note.",
    "kanban.refresh": "Refresh Kanban",
    "kanban.loading": "Loading Kanban…",
    "kanban.error": "Kanban could not be read. Refresh Kanban to try again.",
    "kanban.coverage": "The vault listing may be incomplete, so some cards may be missing.",
    "kanban.coverage.detail":
      "The vault listing may be incomplete ({detail}), so some cards may be missing.",
    "kanban.coverage.omitted": "at least {count} omitted",
    "kanban.empty.ask": "Ask the Chief to make the first card.",
    "kanban.open_chief": "Open Chief",
    "kanban.attention": "Waiting for you: {review} ready for review, {blocked} blocked",
    "kanban.lane.todo": "To do",
    "kanban.lane.working": "Working",
    "kanban.lane.blocked": "Blocked",
    "kanban.lane.review": "Ready for review",
    "kanban.lane.done": "Done",
    "kanban.lane.other": "Other",
    "kanban.lane.unread": "Couldn't read",
    "kanban.lane.heading": "{lane} ({count})",
    "kanban.lane.empty": "No cards.",
    "kanban.card.owner": "Owner:",
    "kanban.card.owners": "Owners:",
    "kanban.card.no_owner": "No owner",
    "kanban.card.status": "Status: {status}",
    "kanban.card.updated_today": "Updated today",
    "kanban.card.updated_yesterday": "Updated yesterday",
    "kanban.card.updated_days": "Updated {days} days ago",
    "kanban.card.stale": "Stopped moving",
    "kanban.unread.unsupported": "The front matter uses a form Kanban cannot read.",
    "kanban.unread.truncated": "The front matter is longer than Kanban reads.",
    "kanban.unread.changed": "The note changed while it was read.",
    "kanban.unread.unreadable": "The note could not be read ({reason}).",
    "kanban.unread.stopped": "The read stopped early ({reason}).",
    "kanban.unread.retry": "Not read. Refresh Kanban to try again.",
    "kanban.unread.name": "Not read: Kanban cannot read this name.",
    "kanban.unread.cap": "Not read: Kanban reads at most {cap} cards.",
    "kanban.no_vault": "Open a vault to view Kanban.",
  },
});
