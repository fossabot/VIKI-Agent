/** `decision_page.*`: the Decision page's words. */

import { defineCatalog } from "./defineCatalog";

export const catalog = defineCatalog({
  en: {
    "decision_page.intro":
      "Set up proactive VIKI: what it notices, how it tells you, and what work it may start for you.",
    "decision_page.read_only": "Settings aren't ready to change right now.",
    "decision_page.group.notices": "What VIKI notices",
    "decision_page.group.decides": "How VIKI decides",
    "decision_page.group.tells": "How VIKI tells you",
    "decision_page.group.history": "History and learning",
    "decision_page.proactive.off":
      "Off. VIKI checks nothing and starts nothing until you turn it on. You can still chat with VIKI in the launcher.",
    "decision_page.proactive.hint":
      "Pause: VIKI stops checking and starts nothing. Focus: VIKI keeps watching but holds nudges and alerts.",
    "decision_page.how.title": "How VIKI works",
    "decision_page.how.notices.title": "Notices",
    "decision_page.how.notices.text":
      "VIKI keeps an eye on your vault and the app: tasks due, inbox, approvals, chats and runs, Kanban cards.",
    "decision_page.how.notices.jump": "Choose what it watches",
    "decision_page.how.decides.title": "Decides",
    "decision_page.how.decides.text":
      "For each thing it notices, VIKI decides whether to tell you now, list it quietly or wait.",
    "decision_page.how.decides.model":
      "Badges, nudges and alerts, plans and work chats for due tasks, Note links and Superseded notes proposals, and Sources to learn need the Decision model. Everything else also works with simple rules.",
    "decision_page.how.decides.jump": "Set up the Decision model",
    "decision_page.how.tells.title": "Tells you",
    "decision_page.how.tells.text":
      "Items appear in the launcher, the floating circle, each with why it matters now. Quiet hours and your nudge limit hold back nudges and alerts.",
    "decision_page.how.tells.jump": "Choose how it reaches you",
    "decision_page.how.acts.title": "Starts work, if you allow",
    "decision_page.how.acts.text":
      "For each kind of work, choose Off, Ask first or Allowed. Plans wait for your approval.",
    "decision_page.how.acts.jump": "Choose what it may do",
    "decision_page.how.learns":
      "Learns: your answers (Useful, Not useful, Stop these and the weekly check) tune when and how loudly VIKI speaks up. Your limits stay as you set them.",
    "decision_page.how.learns.jump": "See how VIKI is doing",
    "decision_page.how.data":
      "Your data: decision history stays on this computer. With a key, titles and excerpts at your Send level go to the Decision model. Chats VIKI starts go to your model provider, as your own chats do.",
    "decision_page.how.data.jump": "See what leaves this computer",
    "decision_page.watch.description":
      "Choose where VIKI looks for things that may need you, and how often.",
    "decision_page.watch.cadence.change": "About every minute",
    "decision_page.watch.cadence_hint":
      "VIKI also checks when you switch chats or notes, open the launcher or come back to the window.",
    "decision_page.watch.rules_note":
      "These switches also work with simple rules. Where something needs the Decision model, the text beside its switch says so.",
    "decision_page.watch.group.vault": "In your vault",
    "decision_page.watch.group.app": "In the app",
    "decision_page.watch.group.improve": "Improvements",
    "decision_page.watch.improve":
      "Skill suggestions, Sources to learn, feedback reviews and vault upkeep proposals. Off stops them all. Sources to learn, Note links and Superseded notes need the Decision model.",
    "decision_page.watch.dep.tasks":
      "Off also stops plans and work chats for due tasks, plans for overdue tasks and proposals to archive ticked tasks. Plans and work chats for due tasks need the Decision model.",
    "decision_page.watch.dep.inbox": "Off also stops inbox suggestions.",
    "decision_page.watch.dep.vault_changes":
      "Off also stops Note links and Superseded notes proposals, which need the Decision model.",
    "decision_page.reach.description": "How loud VIKI may be, and when it must stay quiet.",
    "decision_page.reach.nudges.none": "No nudges",
    "decision_page.reach.nudges.one": "1 nudge an hour",
    "decision_page.reach.nudges.many": "{n} nudges an hour",
    "decision_page.reach.legend.list":
      "Added to the launcher's list; the launcher brightens briefly.",
    "decision_page.reach.legend.badge": "A number on the launcher.",
    "decision_page.reach.legend.nudge": "The launcher's ring pulses once.",
    "decision_page.reach.legend.alert": "The ring stays lit until you open the panel.",
    "decision_page.reach.rules_only":
      "VIKI is using simple rules, so it only lists items. Nudges and alerts need the Decision model.",
    "decision_page.reach.nudges_hint": "The most nudges and alerts in any hour. 0 means none.",
    "decision_page.reach.alerts_hint":
      "For a task linked to an active project that is due within 2 hours or overdue.",
    "decision_page.reach.in_context_hint":
      "VIKI may hold an item until you open the chat or note it is about.",
    "decision_page.reach.quiet_hint":
      "No nudges or alerts. VIKI holds its own looks, second looks, project plans, saved checks and inbox runs, and Bots start only routines that stop at a draft.",
    "decision_page.reach.stopped": "Topics you stopped",
    "decision_page.reach.stopped_none":
      "None. Choose Stop these on an item in the launcher to stop a topic.",
    "decision_page.model.description":
      "Optional and paid. A TypeSafe model helps VIKI judge what matters and when to nudge you. Without a key, VIKI uses simple rules: it lists what it notices, with no badges, nudges or alerts. This key is separate from your chat model.",
    "decision_page.model.state.ready": "Ready",
    "decision_page.model.state.rules": "Simple rules",
    "decision_page.model.state.no_key": "Simple rules · no key",
    "decision_page.model.state.key_rejected": "Key rejected · using rules",
    "decision_page.model.state.capped": "Daily cap reached · using rules",
    "decision_page.model.state.unreachable": "Can't reach TypeSafe · using rules",
    "decision_page.model.spending": "Spending",
    "decision_page.model.cap_hint":
      "Once today's spend reaches this, VIKI uses simple rules until tomorrow.",
    "decision_page.model.rate": "VIKI sends at most 10 requests a minute.",
    "decision_page.model.today":
      "Today: {decisions} decisions · {requests} model requests · {cost}",
    "decision_page.model.today_compare":
      "Today: {decisions} decisions · {requests} model requests ({comparison} for comparison) · {cost}",
    "decision_page.model.capped": "Daily cap reached: simple rules until tomorrow.",
    "decision_page.model.advanced": "Advanced: version and comparison",
    "decision_page.model.version.pinned": "Stays on this version.",
    "decision_page.model.version.latest": "Follows TypeSafe's latest stable version.",
    "decision_page.model.version.preview": "Follows TypeSafe's preview version.",
    "decision_page.privacy.intro":
      "Titles, note paths, facts in words, and note excerpts cut to your Send level go to the Decision model, so a short note can go whole. Chats go only by their titles; their messages are not sent to that model. You can exclude folders or tags from these requests.",
    "decision_page.privacy.never_hint":
      'Add a folder ending in "/" (for example Private/) or a tag starting with "#" (for example #personal).',
    "decision_page.privacy.remove": "Remove {entry}",
    "decision_page.do.description": "For each kind of work, choose whether VIKI may start it.",
    "decision_page.do.legend.off": "VIKI doesn't start it.",
    "decision_page.do.legend.ask": "VIKI offers it as an action on its item in the launcher.",
    "decision_page.do.legend.allowed": "VIKI starts it at its next check, within the daily limit.",
    "decision_page.do.legend.drafts":
      "Message drafts wait for you to select them, under Ask first and Allowed alike.",
    "decision_page.do.review": "Prepared for your review",
    "decision_page.do.review_hint": "Nothing changes until you approve.",
    "decision_page.do.changes": "Work that can make changes",
    "decision_page.do.changes_hint":
      "Chats use your usual Claude Code permissions, Bot runs use their routine's settings, and saved checks are your own commands.",
    "decision_page.do.safety": "Safety",
    "decision_page.do.more": "More about this",
    "decision_page.do.needs_model_tasks":
      "Plans and work chats for due tasks need the Decision model.",
    "decision_page.do.needs_model_links":
      "Note links and Superseded notes proposals need the Decision model.",
    "decision_page.do.dep.tasks":
      "Tasks due is off in What VIKI watches, so there are no plans or work chats for due tasks.",
    "decision_page.do.dep.overdue":
      "Tasks due is off in What VIKI watches, so VIKI plans nothing for overdue tasks.",
    "decision_page.do.dep.inbox":
      "Inbox is off in What VIKI watches, so VIKI won't suggest destinations.",
    "decision_page.do.dep.improve":
      "Self-improvement is off in What VIKI watches, so VIKI proposes no upkeep.",
    "decision_page.do.dep.upkeep":
      "Vault changes and Tasks due are off in What VIKI watches, so VIKI proposes no upkeep.",
    "decision_page.do.dep.upkeep_links":
      "Vault changes is off in What VIKI watches, so VIKI proposes no Note links or Superseded notes.",
    "decision_page.do.dep.upkeep_tasks":
      "Tasks due is off in What VIKI watches, so VIKI proposes no archiving of ticked tasks.",
    "decision_page.hint.prepare":
      "Plans for due tasks that wait for your approval, and message drafts for the chat you have open.",
    "decision_page.hint.run": "A chat that works on a due task.",
    "decision_page.hint.team":
      "Starts a Bot's tested routine when one of your Kanban cards waits for that Bot.",
    "decision_page.hint.look":
      "Reviews code changes left uncommitted for a day, in a plan-mode chat.",
    "decision_page.hint.second_look":
      "After a long or corrected reply that edited notes, looks for one flaw, better approach or missing piece.",
    "decision_page.hint.project_plan":
      "For a project idle two weeks or tasks three days overdue, proposes next steps.",
    "decision_page.hint.inbox":
      "A read-only run suggests where each capture could go. It sends your captures' opening text to Anthropic.",
    "decision_page.hint.check":
      "On a new commit in a trusted code folder, runs the checks you have run there before. They are your own commands and can change files.",
    "decision_page.handoffs.hint":
      "On: a card one Bot writes for another counts like your own card. Off: that card gets Start instead.",
    "decision_page.upkeep.label": "Vault upkeep proposals",
    "decision_page.upkeep.hint":
      "Link notes, mark replaced notes and archive ticked tasks, in an ordinary vault. Each waits in Approvals. 0 turns them off.",
    "decision_page.brake.hint":
      "When your Claude plan usage reaches this share, or your plan limit is reached, VIKI holds the Claude Code work it would start by itself. You can still start it yourself.",
    "decision_page.data.description":
      "What VIKI's decisions keep on this computer, in numbers and charts, and what you can delete.",
    "decision_page.data.chip": "{n} in decision history",
    "decision_page.data.loading": "Loading your data…",
    "decision_page.data.unavailable": "Data is unavailable right now.",
    "decision_page.data.busy": "Decision is busy. Try again in a moment.",
    "decision_page.data.as_of": "As of {time}",
    "decision_page.data.kinds.title": "What VIKI keeps",
    "decision_page.data.kinds.hint":
      "Stored in VIKI Agent's database on this computer, for this workspace. The key is shared by every workspace.",
    "decision_page.data.dates": "Oldest {oldest} · newest {newest}",
    "decision_page.data.dates.none": "Nothing stored",
    "decision_page.data.meter": "{value} of {max}",
    "decision_page.data.no_limit": "No limit",
    "decision_page.data.explore": "Explore",
    "decision_page.data.kind.decisions.name": "Decision history",
    "decision_page.data.kind.decisions.meaning":
      "Each time VIKI decided whether and how to tell you about something, and each missed item you reported.",
    "decision_page.data.kind.decisions.count": "{decisions} decisions · {reports} reports",
    "decision_page.data.kind.decisions.limit": "Newest {max}, up to {days} days",
    "decision_page.data.kind.suggestions.name": "Suggestions",
    "decision_page.data.kind.suggestions.meaning":
      "What VIKI found to bring up, open or closed, with its title and why.",
    "decision_page.data.kind.suggestions.count": "{open} open · {closed} closed",
    "decision_page.data.kind.suggestions.limit": "Up to {max} open; closed ones kept {days} days",
    "decision_page.data.kind.calls.name": "Model calls",
    "decision_page.data.kind.calls.meaning":
      "What VIKI sent the Decision model and what it answered.",
    "decision_page.data.kind.calls.count": "{n} calls",
    "decision_page.data.kind.calls.limit": "Newest {max}",
    "decision_page.data.kind.chats.name": "Chats VIKI started",
    "decision_page.data.kind.chats.meaning":
      "The chats VIKI started for you, and how each one ended.",
    "decision_page.data.kind.chats.count": "{n} chats · {running} running",
    "decision_page.data.kind.chats.kept":
      "Kept so VIKI doesn't start the same work twice, so they can't be deleted here. Their transcripts stay in Claude Code.",
    "decision_page.data.kind.proposals.name": "Proposals",
    "decision_page.data.kind.proposals.meaning":
      "Question wordings and vault changes VIKI proposed for your approval.",
    "decision_page.data.kind.proposals.count": "{wordings} wordings · {changes} vault changes",
    "decision_page.data.kind.proposals.limit":
      "Up to {wordings} wordings and {changes} vault changes",
    "decision_page.data.kind.proposals.kept":
      "You act on them in Approvals. They can't be deleted here.",
    "decision_page.data.kind.preferences.name": "Learned preferences",
    "decision_page.data.kind.preferences.meaning":
      "What VIKI learned from your answers, and the topics you stopped.",
    "decision_page.data.kind.preferences.count": "{muted} topics stopped",
    "decision_page.data.kind.preferences.learned": "Learned values saved",
    "decision_page.data.kind.preferences.not_learned": "Nothing learned yet",
    "decision_page.data.kind.preferences.limit": "One set, reset all at once",
    "decision_page.data.kind.preferences.kept":
      "To start over, use Reset what VIKI learned in Learning; it resets them all at once. To unmute a topic, use Topics you stopped in How VIKI may reach you.",
    "decision_page.data.open_learning": "Open Learning",
    "decision_page.data.open_reach": "Open How VIKI may reach you",
    "decision_page.data.kind.key.name": "Decision model key",
    "decision_page.data.kind.key.meaning":
      "Your key for the Decision model, sealed on this computer and shared by every workspace.",
    "decision_page.data.kind.key.saved": "Saved",
    "decision_page.data.kind.key.missing": "Not saved",
    "decision_page.data.changed": "Changed {date}",
    "decision_page.data.kind.key.limit": "One key",
    "decision_page.data.kind.key.kept": "Change or remove it in Decision model.",
    "decision_page.data.open_model": "Open Decision model",
    "decision_page.data.activity.title": "Activity",
    "decision_page.data.range": "Range",
    "decision_page.data.range.30": "Last 30 days",
    "decision_page.data.range.90": "Last 90 days",
    "decision_page.data.range.365": "Last 365 days",
    "decision_page.data.series.held": "Held back",
    "decision_page.data.series.listed": "Listed",
    "decision_page.data.series.attention": "Got your attention",
    "decision_page.data.series.reports": "Reports",
    "decision_page.data.activity.summary":
      "{range}: {decisions} decisions ({held} held back, {listed} listed, {attention} got your attention) and {reports} reports.",
    "decision_page.data.activity.days": "Each column is one day, newest on the right.",
    "decision_page.data.activity.weeks": "Each column is one week, newest on the right.",
    "decision_page.data.table.show": "Show as table",
    "decision_page.data.table.hide": "Hide table",
    "decision_page.data.table.day": "Day",
    "decision_page.data.table.week": "Week ending",
    "decision_page.data.empty_range": "No decisions in this range.",
    "decision_page.data.answers.title": "How you answered",
    "decision_page.data.answers.hint": "Over the same days as Activity.",
    "decision_page.data.answers.shown": "Shown to you: {n}",
    "decision_page.data.answers.held": "Held back: {n}",
    "decision_page.data.answer.useful": "Useful",
    "decision_page.data.answer.wrong": "Not useful",
    "decision_page.data.answer.later": "Not now",
    "decision_page.data.answer.mute": "Stopped",
    "decision_page.data.answer.right_to_hold": "Right to hold",
    "decision_page.data.answer.missed": "Should have told me",
    "decision_page.data.answer.none": "No answer",
    "decision_page.data.answers.share": "{label}: {n} ({percent}%)",
    "decision_page.data.answers.reports": "{n} reports of something VIKI missed",
    "decision_page.data.topics.title": "Topics",
    "decision_page.data.topics.hint":
      "Over all stored decisions, busiest first. Select a topic to see its decisions.",
    "decision_page.data.topics.topic": "Topic",
    "decision_page.data.topics.shown": "Shown",
    "decision_page.data.topics.show_all": "Show all {n} topics",
    "decision_page.data.topic.unwatched": "Something VIKI missed",
    "decision_page.data.topic.any": "Any topic",
    "decision_page.data.browse.title": "Browse and delete",
    "decision_page.data.browse.kind": "Show",
    "decision_page.data.browse.decisions": "Decisions",
    "decision_page.data.filter.any": "Any",
    "decision_page.data.filter.shown": "Shown as",
    "decision_page.data.level.badge": "Badge",
    "decision_page.data.level.nudge": "Nudge",
    "decision_page.data.level.alert": "Alert",
    "decision_page.data.level.report": "Report",
    "decision_page.data.filter.answer": "Your answer",
    "decision_page.data.filter.state": "State",
    "decision_page.data.state.open": "Open",
    "decision_page.data.state.acted": "Acted on",
    "decision_page.data.state.dismissed": "Dismissed",
    "decision_page.data.state.stale": "Outdated",
    "decision_page.data.state.done": "Done",
    "decision_page.data.filter.outcome": "Outcome",
    "decision_page.data.outcome.answered": "Answered",
    "decision_page.data.outcome.failed": "Failed",
    "decision_page.data.filter.period": "When",
    "decision_page.data.period.any": "Any time",
    "decision_page.data.period.today": "Today",
    "decision_page.data.period.7": "Last 7 days",
    "decision_page.data.results.decisions": "{total} decisions match; {deletable} can be deleted.",
    "decision_page.data.results.suggestions":
      "{total} suggestions match; {deletable} can be deleted.",
    "decision_page.data.results.calls": "{total} model calls match.",
    "decision_page.data.results.none": "Nothing matches.",
    "decision_page.data.more": "Show 50 more",
    "decision_page.data.select": "Select {name}",
    "decision_page.data.select.limit": "You can select up to 50 at a time.",
    "decision_page.data.details": "Details",
    "decision_page.data.details.hide": "Hide details",
    "decision_page.data.open_context": "Open in Context",
    "decision_page.data.calls.context":
      "The Decision tab in Context & loadout shows each of these calls in full: what VIKI sent, and the answers or why it failed.",
    "decision_page.data.tokens": "{n} tokens",
    "decision_page.data.kept.open_suggestion": "Kept: its suggestion is still open.",
    "decision_page.data.kept.open": "Kept: it is still open.",
    "decision_page.data.kept.chat": "Kept: a chat VIKI started holds it.",
    "decision_page.data.unreadable": "This title can't be read.",
    "decision_page.data.delete.selected": "Delete selected ({n})",
    "decision_page.data.delete.matching": "Delete all {n} matching",
    "decision_page.data.delete.need_filter":
      "To delete by filter, first choose a topic, how it was shown, an answer, a state or a time. To delete everything, use Delete all decision history.",
    "decision_page.data.removing": "Removing… VIKI stops its current check first.",
    "decision_page.data.deleted": "Deleted {n}.",
    "decision_page.data.deleted.kept": "Kept {n} still in use.",
    "decision_page.data.deleted.gone": "{n} were already gone.",
    "decision_page.data.delete.failed": "Could not delete them. Try again.",
    "decision_page.data.load.failed": "Could not load these records.",
    "decision_page.data.detail.at": "Decided",
    "decision_page.data.detail.policy": "Decided by",
    "decision_page.data.policy.rules": "Rules",
    "decision_page.data.policy.model": "Decision model",
    "decision_page.data.detail.why": "Why",
    "decision_page.data.detail.what": "What",
    "decision_page.data.detail.evidence": "Evidence",
    "decision_page.data.detail.shown": "Shown {n} times",
    "decision_page.data.detail.opened": "Opened",
    "decision_page.data.detail.cost": "Model request: {tokens} tokens, {cost}",
    "decision_page.data.detail.version": "Model version",
    "decision_page.data.detail.created": "Created",
    "decision_page.data.detail.updated": "Last changed",
    "decision_page.data.confirm.decisions": "Delete {n} decisions?",
    "decision_page.data.confirm.decisions_matching": "Delete all {n} matching decisions?",
    "decision_page.data.confirm.suggestions": "Delete {n} suggestions?",
    "decision_page.data.confirm.suggestions_matching": "Delete all {n} matching suggestions?",
    "decision_page.data.confirm.calls": "Delete {n} model calls?",
    "decision_page.data.confirm.body.decisions": "Decisions whose suggestion is still open stay.",
    "decision_page.data.confirm.body.suggestions":
      "Open suggestions, and ones a chat VIKI started holds, stay.",
    "decision_page.data.confirm.body.calls": "These calls are removed from VIKI Agent's database.",
    "decision_page.data.confirm.changes": "This also changes:",
    "decision_page.data.confirm.stays": "These stay:",
    "decision_page.data.confirm.delete": "Delete",
    "decision_page.data.effect.learned":
      "What VIKI learned resets. With Learning on, it learns again from the decisions that remain.",
    "decision_page.data.effect.answers":
      "The Decision model stops hearing that they were shown and how you answered them.",
    "decision_page.data.effect.showings":
      "VIKI forgets how often it showed these items in the last day, so it may show them again sooner.",
    "decision_page.data.effect.comparisons":
      "Comparisons with another model version or a proposed question wording lose them.",
    "decision_page.data.effect.vault_change":
      "Approving or rejecting a vault change whose decision is gone teaches VIKI nothing.",
    "decision_page.data.effect.pauses":
      "Pauses on message drafts, saved checks and inbox suggestions are counted again without these answers.",
    "decision_page.data.effect.improve":
      "Self-improvement suggestions drawn from your answers, such as running a retro, are worked out again without them.",
    "decision_page.data.effect.history":
      "They leave How VIKI is doing, its weekly check and the charts here.",
    "decision_page.data.effect.comes_back":
      "If VIKI finds the same thing again, it comes back as a new suggestion, even one you marked Not useful, and with Allowed VIKI may start its work again.",
    "decision_page.data.effect.titles": "The Decision model stops hearing their titles.",
    "decision_page.data.effect.item_pauses":
      "Pauses on message drafts, saved checks and inbox suggestions are counted again without them.",
    "decision_page.data.effect.today":
      "Today's counts of message drafts, saved checks and inbox suggestions drop, so VIKI may raise more of them today.",
    "decision_page.data.effect.looks":
      "Chats VIKI started today for looks, second looks and project plans count toward the daily limit on drafts until tomorrow.",
    "decision_page.data.effect.see_why": "See why on their suggestions can't show these calls.",
    "decision_page.data.effect.last_call":
      "What leaves this computer shows the newest call that remains.",
    "decision_page.data.stay.chats": "Chats VIKI started, and their transcripts in Claude Code.",
    "decision_page.data.stay.settings": "Your settings, grants and the topics you stopped.",
    "decision_page.data.stay.today":
      "Today's model calls and spending, as the daily cap counts them.",
    "decision_page.data.stay.decisions": "Their decisions, in decision history, without their why.",
    "decision_page.data.stay.proposals": "Proposals, and the question wording in use.",
    "decision_page.data.all.button": "Delete all decision history",
    "decision_page.data.all.hint":
      "Removes this workspace's decisions, suggestions and model calls at once.",
    "decision_page.data.all.confirm": "Delete all decision history?",
    "decision_page.data.all.body":
      "This workspace's decision history is removed from VIKI Agent's database.",
    "decision_page.data.all.removes": "This removes:",
    "decision_page.data.all.remove.decisions": "All {n} decisions and reports.",
    "decision_page.data.all.remove.suggestions": "Every suggestion, open ones included.",
    "decision_page.data.all.remove.calls": "Every model call.",
    "decision_page.data.all.remove.learned":
      "What VIKI learned. With Learning on, it learns again from new decisions.",
    "decision_page.data.all.done":
      "Deleted {n} decisions and reports, with every suggestion and model call.",
    "decision_page.data.all.failed": "Could not delete the decision history. Try again.",
    "decision_page.data.see_all": "See every decision in Data",
    "decision_page.data.see_answers": "See how you answered, day by day",
  },
});
