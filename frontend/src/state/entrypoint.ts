/**
 * Which process opened a session, and what to call it.
 *
 * This exists because four surfaces named the entrypoint and none of them agreed with the
 * server. `catalog.py`'s MAIN_ENTRYPOINTS is the authority — {"claude-vscode", "cli",
 * "viki-agent"} — the terminal writes `cli`. Every one of those
 * four compared against
 * `"claude-cli"`, a string nothing has ever produced. Three of them fell through harmlessly to
 * the raw value; the vault banner's else-branch asserts a program, so a terminal session, an SDK
 * script and a second window of this app were all told "VIKI is also running in VS Code".
 *
 * The label is here; the sentences that use it stay with the surface that owns their wording.
 * What is shared is the fact — which string means which program — because that is the part that
 * was wrong in four places at once.
 */
export const VSCODE_ENTRYPOINT = "claude-vscode";
export const CLI_ENTRYPOINT = "cli";

/**
 * The badge form, for a row, a tooltip or a card: "VS Code", "CLI", or the value as it came.
 *
 * An unfamiliar entrypoint (`sdk-py`, a future driver) is shown verbatim rather than guessed at.
 * It is not pretty, but it is true, and it is the one form that cannot tell the user that
 * something is running somewhere it is not.
 */
export function entrypointBadge(entrypoint: string): string {
  if (entrypoint === VSCODE_ENTRYPOINT) return "VS Code";
  if (entrypoint === CLI_ENTRYPOINT) return "CLI";
  return entrypoint;
}
