/**
 * `decisions.*`: copy for the Approvals section (the proposal list) and the vault-proposal
 * inspector it opens. `{item}`, `{count}`, `{ref}`, `{base}` and `{proposed}` are filled by `tf`.
 */

import { defineCatalog } from "./defineCatalog";

export const catalog = defineCatalog({
  en: {
    "decisions.section": "Proposals",
    "decisions.open": "Open",
    "decisions.select": "Select {item}",
    "decisions.load_failed": "Could not load the list.",
    "decisions.batch.approve": "Approve {count} selected…",
    "decisions.batch.handoff": "Create drafts for {count} selected…",

    "decisions.group.attention": "Needs attention",
    "decisions.group.handed": "In progress",
    "decisions.group.snoozed": "Later",
    "decisions.group.recorded": "Recorded",

    "decisions.chip.handed": "Draft created",
    "decisions.chip.rejected": "Rejected",
    "decisions.chip.stale": "Changed since",
    "decisions.chip.source_missing": "Source not found",
    "decisions.chip.open": "Proposed",
    "decisions.chip.applied": "Note matches the proposal",
    "decisions.chip.unknown": "State unknown",
    "decisions.chip_note": "The app has not checked the change itself",

    "decisions.retro.load_failed": "Could not load the item.",
    "decisions.retro.close": "Close",
    "decisions.retro.handoff": "Create a draft",

    "decisions.proposal.copy_label": "Copy the proposal",
    "decisions.proposal.withdraw": "Withdraw the proposal",
    "decisions.proposal.withdraw_ask":
      "Withdraw this proposal? It disappears from the app's proposal list. The note does not change.",
    "decisions.proposal.withdraw_yes": "Withdraw",
    "decisions.proposal.withdraw_no": "Cancel",
    "decisions.proposal.open_editor": "Open in the editor",
    "decisions.proposal.too_large": "The diff is too large to show.",
    "decisions.proposal.no_source": "The source note was not found, so the diff cannot be shown.",
    "decisions.proposal.other_vault":
      "This is not a proposal for the vault that is open, so this action is unavailable.",
    "decisions.proposal.action_failed": "Could not do that.",
    "decisions.proposal.origin_note_edit": "Note edit",
    "decisions.proposal.origin_memory_promotion": "Memory proposal",

    "decisions.handoff.proposal_apply":
      "Apply the following change to the vault note {ref}. Its current sha256 starts with {base} and should start with {proposed} afterwards. Change nothing outside the diff.",
    "decisions.handoff.proposal_replace":
      "Replace the vault note {ref} with the following content. Its current sha256 starts with {base} and should start with {proposed} afterwards.",
    "decisions.handoff.proposal_create":
      "Create the vault note {ref} with exactly the following content, and only if no note exists at {ref}. Its sha256 should start with {proposed} afterwards.",
    "decisions.handoff.drafted":
      "A draft is in the new conversation's input box. Check it before you send it.",
    "decisions.editor_full":
      "This window has reached its limit of open drafts. Check the save status, then reopen the window.",
  },
});
