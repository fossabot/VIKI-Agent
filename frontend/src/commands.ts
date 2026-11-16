/**
 * The one command table. Every entry point that can start a command — the app
 * menu, a banner button, the onboarding view, a settings page, a keyboard shortcut — asks
 * `commandFor` whether the command is available and, when it is not, which catalog key says
 * why. App's dispatcher then has exactly one case per id.
 *
 * Rules:
 * - Unknown capabilities leave the chat commands (new session, search, run a command) enabled
 *   until the first `/api/v2/capabilities` answer arrives. The commands that need a ready
 *   store (create, open, import, reset) stay off until a known-ready store admits them,
 *   because their surfaces only exist once capabilities are known anyway.
 * - A refused command is never hidden and never `disabled`: the caller renders a focusable
 *   `aria-disabled` button whose reason is this `reason` key. A `reason` of
 *   `null` means "there is no row to render here", not "refused without a reason".
 * - No state, no I/O, no logging: a pure function of the shell snapshot, the view the user is
 *   in and — for the two diagnostics checks, which have different gates — which check is asked
 *   about.
 */

import type { CheckName } from "./clients/app/shellApi";
import type { Key } from "./i18n/index";
import { isMac } from "./platform";
import { canStart, type ShellSnapshot } from "./state/shell";

/** The navigation states App switches between; declared here because the command table is what
 * every caller already imports, and App.tsx cannot be imported back by its own consumers. */
export type View =
  | "chat"
  | "capabilities"
  | "messaging"
  | "bots"
  | "kanban"
  | "bot_threads"
  | "onboarding"
  | "settings"
  | "data"
  | "recovery"
  | "vault"
  | "agents"
  | "routines"
  | "code"
  | "decision"
  | "context";
export type SettingsPage =
  | "general"
  | "appearance"
  | "chat"
  | "providers"
  | "workspaces"
  | "persona"
  | "privacy"
  | "diagnostics";
export type DataPanel = "locations" | "export" | "import" | "reset";

/** What App remembers about where the user is, and what `navigate` merges a partial into. */
export interface Navigation {
  view: View;
  page: SettingsPage;
  panel: DataPanel;
  onboardingMode: "choose" | "create" | "open";
}

export type CommandId =
  | "session.new"
  | "search.conversations"
  | "search.vault"
  | "command.run"
  | "workspace.create"
  | "workspace.open"
  | "workspace.later"
  | "workspace.select"
  | "settings.open"
  | "settings.search"
  | "diagnostics.open"
  | "diagnostics.check"
  | "data.open"
  | "data.export_diagnostics"
  | "data.import_settings"
  | "data.reset"
  | "provider.setup"
  | "bots.open";

/** One member, because one member is all anything builds: the Providers page's two check
 * buttons. A `{ panel }` or `{ query }` variant has no caller — the data commands navigate to
 * a panel named in the dispatcher, and no entry point carries a search query. Either comes
 * back the day something constructs it. */
export type CommandArg = { check: CheckName };

export interface CommandState {
  enabled: boolean;
  /** A catalog key, rendered through `t()`; never a server string. */
  reason: Key | null;
}

export const SHORTCUTS: Readonly<Partial<Record<CommandId, string>>> = {
  "session.new": "Alt+N",
  "search.conversations": "Ctrl+K",
  // Reserved, not bound: the row documents the shortcut the vault search will take.
  "search.vault": "Ctrl+Shift+K",
  "settings.open": "Ctrl+,",
};

/** The same rows, in the labels macOS shows: Cmd for Ctrl, Option for Alt. */
export const MAC_SHORTCUTS: Readonly<Partial<Record<CommandId, string>>> = {
  "session.new": "⌥N",
  "search.conversations": "⌘K",
  "search.vault": "⌘⇧K",
  "settings.open": "⌘,",
};

/** The shortcut label a caller should show for `id`, on the platform `mac` names. */
export function shortcutLabel(id: CommandId, mac: boolean = isMac()): string | undefined {
  return mac ? MAC_SHORTCUTS[id] : SHORTCUTS[id];
}

function state(enabled: boolean, reason: Key | null): CommandState {
  return { enabled, reason: enabled ? null : reason };
}

/**
 * Named `commandFor`, not `commandState`: App.tsx already imports a `commandState` from
 * DraftRecovery.tsx.
 *
 * `inView` is the view the user is in (the in-view commands are only available from their own
 * screen); `arg` is only `diagnostics.check`'s — the two checks have different gates and one
 * table must answer for both, so the Providers page names the one it is asking about. No
 * dispatched row carries an argument.
 */
export function commandFor(
  id: CommandId,
  shell: ShellSnapshot,
  inView?: View,
  arg?: CommandArg,
): CommandState {
  const capabilities = shell.capabilities;
  const recovery = shell.recovery;
  const storageReady = capabilities !== null && capabilities.storage.state === "ready";
  switch (id) {
    case "session.new": {
      const provider = capabilities?.provider.claude;
      // A refused subscription leaves the editor available for an explicit model connection.
      if (
        storageReady &&
        !recovery &&
        provider?.cli === "ready" &&
        provider.billing === "blocked" &&
        provider.problems.length === 0
      )
        return state(true, null);
      return state(canStart(shell) !== false, "reason.provider_not_ready");
    }
    case "command.run":
      // `canStart` is null while capabilities are unknown, which is not a refusal.
      return state(canStart(shell) !== false, "reason.provider_not_ready");
    case "search.conversations":
      // Search reads the provider's transcript catalog, never the app store, so a read-only or
      // damaged store does not close it; only a recovery window, which serves no search route.
      return state(!recovery, "reason.recovery");
    case "search.vault":
      // The backend serves no `vault_search` feature yet, so this row stays off; the Vault view's
      // own "Search notes" field is where the search already lives.
      return state(capabilities?.features.vault_search === true, "reason.vault_search_in_view");
    case "workspace.create":
    case "workspace.open":
    case "bots.open":
      if (recovery) return state(false, "reason.recovery");
      return state(storageReady, "reason.storage_not_ready");
    case "workspace.later":
      // "Set up later" PATCHes a setting, so it needs a settings document to have been read.
      return state(inView === "onboarding" && shell.settingsStatus !== "unavailable", null);
    case "workspace.select":
      if (recovery) return state(false, "reason.recovery");
      return state((shell.workspaces?.workspaces.length ?? 0) > 0, "reason.no_workspaces");
    case "settings.open":
      return state(!recovery, "reason.recovery");
    case "settings.search":
      return state(inView === "settings", null);
    case "diagnostics.open":
      // The one command a recovery window still has.
      return state(true, null);
    case "diagnostics.check": {
      if (shell.checkRunning !== null) return state(false, "reason.check_running");
      const check = arg?.check ?? "git";
      if (check === "provider_cli")
        return state(capabilities?.provider.claude.cli === "ready", "reason.cli_not_ready");
      return state(true, null);
    }
    case "data.open":
      // The locations panel is a read; only a recovery window, which does not mount the route,
      // keeps it shut.
      return state(!recovery, "reason.recovery");
    case "data.export_diagnostics":
      return state(!recovery, "reason.recovery_no_export");
    case "data.import_settings":
    case "data.reset":
      // A recovery window's store is never ready, so this reason stays true there too.
      return state(storageReady && !recovery, "reason.storage_not_ready");
    case "provider.setup":
      // Only offered when the app knows the provider cannot start; otherwise the row is absent.
      return state(capabilities !== null && !capabilities.provider.claude.can_start, null);
  }
}
