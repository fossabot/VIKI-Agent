/**
 * The unload guard: closing the window while VIKI is mid-turn or waiting on the user asks
 * first. Vault git state never does: closing the window loses no file, so there is nothing to
 * ask about. Pure functions; App.tsx registers the listener (unload.unit.test.ts pins the rules).
 */
import type { AppSnapshot } from "./store";
import { presence } from "./view";

/** Sorted; RetryReview also reads this list, and session.py holds the same names as a
 * frozenset. */
export const WRITE_TOOLS: readonly string[] = [
  "Bash",
  "Edit",
  "NotebookEdit",
  "PowerShell",
  "Write",
];

/** True when any view the page holds, and has not seen closed, is not idle — a running turn or
 * a card waiting for the user. A closed view never counts on its own: session.closed leaves its
 * last turn's status as it was, so a session closed mid-turn must not block the window for
 * good. */
export function unsavedWork(app: AppSnapshot): boolean {
  for (const view of Object.values(app.sessions)) {
    if (view.closed) continue;
    if (presence(view) !== "idle") return true;
  }
  return false;
}
