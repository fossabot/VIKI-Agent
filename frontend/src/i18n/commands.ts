/**
 * `cmd.<id>` (every `CommandId` from `../commands.ts`, since `AppMenu` calls `t(\`cmd.${id}\`)`
 * for a dynamic id) and `reason.<...>` (every reason `commandFor` can return). Shortcut key
 * combinations (`Alt+N` and the like) are not catalog rows — they come from `SHORTCUTS`, or
 * `MAC_SHORTCUTS` on macOS, in `../commands.ts` verbatim.
 */

import { defineCatalog } from "./defineCatalog";

export const catalog = defineCatalog({
  en: {
    "cmd.session.new": "New session",
    "cmd.search.conversations": "Search conversations",
    "cmd.search.vault": "Search the vault",
    "cmd.command.run": "Run a command",
    "cmd.workspace.create": "Create a vault",
    "cmd.workspace.open": "Open a folder",
    "cmd.workspace.later": "Set up later",
    "cmd.workspace.select": "Switch workspace",
    "cmd.settings.open": "Settings",
    "cmd.settings.search": "Search settings",
    "cmd.diagnostics.open": "Diagnostics",
    "cmd.diagnostics.check": "Run check",
    "cmd.data.open": "Data locations",
    "cmd.data.export_diagnostics": "Export diagnostics",
    "cmd.data.import_settings": "Import settings",
    "cmd.data.reset": "Reset",
    "cmd.provider.setup": "Set up provider",
    "cmd.bots.open": "Bots",

    "reason.provider_not_ready": "Claude Code is not ready to start a session.",
    "reason.recovery": "Unavailable in recovery mode.",
    "reason.recovery_no_export": "Exports are unavailable in recovery mode.",
    "reason.vault_search_in_view": "Search notes in the Vault view.",
    "reason.storage_not_ready": "The app store is not ready.",
    "reason.no_workspaces": "No workspace is registered yet.",
    "reason.check_running": "Another check is already running.",
    "reason.cli_not_ready": "Claude Code is not ready to be checked.",
  },
});
