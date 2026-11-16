/** The English text for a bot delivery receipt's `reason` code. `messaging/bots.py`'s
 * `_delivery_values` validates that field against the backend's own reason vocabulary, so the
 * map below carries exactly those codes. `owner_report` is not one of them, so a bot receipt
 * can never hold it. */
export function reasonText(code: string): string {
  const reasons: Record<string, string> = {
    muted_by_owner: "You muted this topic.",
    later_by_owner: "You chose Later.",
    show_all: "Shown because VIKI was showing every routine result.",
    waiting_on_you: "Something is waiting for your answer.",
    due_or_overdue: "A task is due today or overdue.",
    due_soon: "Tasks are due within 7 days, none today.",
    runs_to_review: "An agent run finished and is ready to review.",
    runs_working: "Agents are still working; nothing to review yet.",
    inbox_pressure: "The inbox has more than 3 items or one older than 3 days.",
    inbox_below_nudge: "The inbox is under 3 items and 3 days.",
    uncommitted_changes: "The vault has uncommitted changes.",
    routines_paused: "Routines are paused.",
    routine_soon: "A routine runs within 12 hours.",
    routine_later: "The next routine is more than 12 hours away.",
  };
  return reasons[code] ?? "";
}
