/**
 * Wrap up — the one prompt the app puts in the user's mouth, and the rule that decides when it
 * may be offered (src/accept/m4/wrapUp.accept.test.ts pins both).
 *
 * Why a fixed prompt: the vault's CLAUDE.md ends every session that changed it with a log line
 * and a commit. That is VIKI's own convention, not something the user should retype, so the
 * menu sends it verbatim through the ordinary turn path — the app never writes the vault or runs
 * git itself, exactly as it never does for anything else.
 *
 * Why it can be refused: CLAUDE.md's session-end commit stages with `git add -A`. If another
 * claude session has the vault as its cwd, that commit would sweep up its half-finished edits.
 * So Wrap up is disabled while ANY other live session sits in the vault — VS Code's, the CLI's,
 * or another window of this app's — and the tooltip names them, so the user knows what to
 * finish rather than being told a flat no.
 *
 * `others` is the vault.sessions list the server polls: sessions whose cwd is the vault that
 * this app does not own. Its titles come from transcripts, so every caller renders them as text.
 *
 * `ownLive` is the other half: `others` is built server-side by subtracting own_pids() — every
 * process this app runs — so a sibling session of the one being wrapped up can never appear in
 * it, by construction. Without `ownLive`, Wrap up, whose entire reason to exist is that
 * `git add -A` would sweep another session's half-finished edits, would be blind to the one
 * other session it is actually in a position to know about. A session of ours is not
 * privileged over a VS Code one: both hold the vault, both have to finish first.
 */
import type { OtherSession } from "../../protocol";
import { CLI_ENTRYPOINT, entrypointBadge, VSCODE_ENTRYPOINT } from "../../state/entrypoint";

/** Sent verbatim as a normal turn, worded the way the user would say it. */
export const WRAP_UP_PROMPT =
  "Wrap up this session. Review the notes you changed, add 1 line to system/log.md, " +
  "and run git add -A && git commit with a message that follows CLAUDE.md.";

export interface WrapUpState {
  enabled: boolean;
  /** "" when enabled; otherwise the tooltip naming who has to finish first. */
  tooltip: string;
}

/** Untitled transcripts are worded the way every other row words them. */
function label(session: OtherSession): string {
  return `${entrypointBadge(session.entrypoint)}: ${session.title || "Untitled session"}`;
}

export function wrapUpState(
  others: readonly OtherSession[],
  ownLive: readonly string[] = [],
): WrapUpState {
  const waiting = [...others.map(label), ...ownLive.map(ownLabel)];
  if (waiting.length === 0) return { enabled: true, tooltip: "" };
  return {
    enabled: false,
    tooltip: `Other sessions are running in the vault. Use this after they finish.\n${waiting.join("\n")}`,
  };
}

/** Our own sessions have no entrypoint badge worth printing — it would read "viki-agent",
 * which tells the user nothing they cannot see from the window they are in. */
function ownLabel(title: string): string {
  return `This app: ${title || "Untitled session"}`;
}

/** The banner line, or "" when nothing else is live in the vault. */
export function vaultBannerText(others: readonly OtherSession[]): string {
  const first = others[0];
  if (!first) return "";
  const title = first.title || "Untitled session";
  // The only sentence here that asserts a program, so it is the only one that must not guess: an
  // SDK script or a second window of this app is "in another session", not VS Code. The trailing
  // space before VIKI is the sentence's, not the label's, so every branch reads the same way.
  const where =
    first.entrypoint === VSCODE_ENTRYPOINT
      ? "in VS Code"
      : first.entrypoint === CLI_ENTRYPOINT
        ? "in the terminal"
        : "in another session";
  const more = others.length > 1 ? `, ${others.length - 1} more` : "";
  return (
    `VIKI is also running ${where} (${title}${more}).` +
    " Wrap up (log + commit) is available after it finishes."
  );
}
