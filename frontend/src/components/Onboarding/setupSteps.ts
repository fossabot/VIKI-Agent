/**
 * The setup guide's three step states, read from the shell snapshot alone (no I/O, nothing new
 * on the wire). A step never claims more than the snapshot proves: the login is "unknown" until
 * a chat has started, so only `login: "signed_in"` reads as done.
 */
import type { Key } from "../../i18n/index";
import type { ShellSnapshot } from "../../state/shell";
import type { ConnectionLabels } from "../Profiles/modelLabel";
import { selectedRow, selectedVaultProblem } from "../Shell/ChatOnlyBanner";

export const CLAUDE_SETUP_URL = "https://code.claude.com/docs/en/setup";

export type ClaudeStep =
  | { state: "checking" }
  | {
      state: "needed";
      problem:
        | "workspaces.setup.missing"
        | "workspaces.setup.unusable"
        | "workspaces.setup.config_dir";
    }
  | { state: "done"; version: string; older: boolean; tested: readonly [string, string] };

export type ModelStep =
  | { state: "checking" | "waiting" | "unchecked" }
  /** The default is a model connection whose list could not be read, so it is not confirmed. */
  | { state: "unchecked"; connection: "unread" }
  | { state: "needed"; billing: boolean; connectionMin?: string }
  /** The default names a model connection that is no longer listed. */
  | { state: "needed"; billing: false; connection: "missing" }
  | { state: "done"; route: "subscription" | "connection" };

export type VaultStep =
  | { state: "checking" | "restart" }
  | { state: "needed"; problem: Key | null }
  | { state: "done"; name: string };

const CLI_PROBLEM = {
  missing: "workspaces.setup.missing",
  unusable: "workspaces.setup.unusable",
  config_dir_override: "workspaces.setup.config_dir",
} as const;

/**
 * `connections` is the model connections list, read by the caller only while the default model
 * is a connection: a default whose connection is not listed is not a route chats can take.
 */
export function setupSteps(
  snapshot: ShellSnapshot,
  connections: ConnectionLabels = { status: "failed", labels: [] },
): {
  claude: ClaudeStep;
  model: ModelStep;
  vault: VaultStep;
} {
  const capabilities = snapshot.capabilities;
  if (capabilities === null) {
    return {
      claude: { state: "checking" },
      model: { state: "checking" },
      vault: { state: "checking" },
    };
  }
  const provider = capabilities.provider.claude;
  const workspace = capabilities.workspace;

  const claude: ClaudeStep =
    provider.cli === "ready"
      ? {
          state: "done",
          version: provider.version,
          older: provider.version_status === "older",
          tested: provider.tested,
        }
      : { state: "needed", problem: CLI_PROBLEM[provider.cli] };

  const defaultModel = snapshot.settings?.effective["chat.default_model"];
  let model: ModelStep;
  if (provider.cli !== "ready") model = { state: "waiting" };
  else if (typeof defaultModel === "string" && defaultModel.startsWith("viki-llm/"))
    // The server refuses connection chats below the tested minimum, so only a checked version
    // reads as done, and only for a connection the list still holds.
    model =
      provider.problems.length > 0
        ? { state: "needed", billing: true }
        : connections.status === "loading"
          ? { state: "checking" }
          : connections.status === "failed"
            ? { state: "unchecked", connection: "unread" }
            : !connections.labels.some((row) => row.model_ref === defaultModel)
              ? { state: "needed", billing: false, connection: "missing" }
              : provider.version_status === "tested" || provider.version_status === "newer"
                ? { state: "done", route: "connection" }
                : { state: "needed", billing: false, connectionMin: provider.tested[0] };
  else if (provider.billing === "blocked") model = { state: "needed", billing: true };
  else if (provider.login === "signed_in") model = { state: "done", route: "subscription" };
  else if (provider.login === "signed_out") model = { state: "needed", billing: false };
  else model = { state: "unchecked" };

  let vault: VaultStep;
  if (workspace.active?.kind === "vault" && !workspace.chat_only)
    // A vault whose folder cannot be reached runs no chat in it.
    vault = workspace.active.root_available
      ? { state: "done", name: workspace.active.name }
      : { state: "needed", problem: "error.root_unavailable" };
  else if (
    workspace.chat_only &&
    workspace.restart_required &&
    selectedRow(snapshot)?.kind !== "code" &&
    selectedVaultProblem(snapshot) === null
  )
    vault = { state: "restart" };
  else vault = { state: "needed", problem: selectedVaultProblem(snapshot) };

  return { claude, model, vault };
}
