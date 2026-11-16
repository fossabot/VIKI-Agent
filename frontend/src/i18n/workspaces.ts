/**
 * The Workspaces and Vault page, `PolicyPanel`, `ProvidersPage`, and `DiagnosticsPage`/About.
 * Backend refusal codes for these screens render through `errors.workspaces.ts` /
 * `errors.settings.ts` (`stale_revision`) / `errors.diagnostics.ts`, not from here; this file
 * holds only the screens' own labels and the readiness facts that are not errors.
 */

import { defineCatalog } from "./defineCatalog";

export const catalog = defineCatalog({
  en: {
    "workspaces.chip.bootstrap_pending": "Creation is still in progress",
    "workspaces.active": "In use",
    "workspaces.select.already_active": "This workspace is already in use.",

    "workspaces.marker.claude_md": "CLAUDE.md",
    "workspaces.marker.agents_md": "AGENTS.md",
    "workspaces.marker.claude_dir": ".claude directory",
    "workspaces.marker.hooks": "Hooks configured",
    "workspaces.marker.git": "Git repository",
    "workspaces.marker.viki_manifest": "VIKI manifest",
    "workspaces.marker.mcp_json": ".mcp.json file",
    "workspaces.instruction.too_large": "Too large to preview",

    "workspaces.open_code.path": "Code folder path",
    "workspaces.code.trust": "Trust this folder",
    "workspaces.code.trust_open": "Trust…",
    "workspaces.code.untrust": "Stop trusting",
    "workspaces.code.chip.trusted": "Trusted",
    "workspaces.code.chip.untrusted": "Not trusted",
    "workspaces.code.untrusted_note":
      "Until you trust it, VIKI starts no session here and runs no Git command on its working tree. You can browse its files.",
    "workspaces.code.trusted_note":
      "Chats can start here, and a tick adds this folder to new chats.",
    "workspaces.code.trust_writes":
      "Trust covers VIKI Agent's own actions here: Code view saves, commits, Integrate, Push, worktree creation, archiving and removal, and the looks and saved checks your Decision settings allow, Bot code copies included.",
    "workspaces.code.trust_loads":
      "Chats that start here load its CLAUDE.md and skills, run its hooks and connect its MCP servers without asking.",
    "workspaces.code.trust_sandbox":
      "Trust is not a sandbox: Claude Code's permissions decide what a chat reads, edits or runs.",

    "workspaces.lead":
      "Your vault holds notes and is where chats start. Code folders below are additional places for chats to work.",
    "workspaces.vault.heading": "Your vault · chats start here",
    "workspaces.vault.change": "Change vault…",
    "workspaces.vault.new": "New vault…",
    "workspaces.vault.details": "Details",
    "workspaces.vault.pick_title": "Choose your vault folder",
    "workspaces.vault.next_launch":
      "Selected for next launch: {name}. Restart VIKI Agent to switch.",
    "workspaces.type_path": "Type a path instead",
    "workspaces.other.label": "Other vaults:",
    "workspaces.other.chat_only": "Chat only",
    "workspaces.other.no_folder": "(no folder)",
    "workspaces.other.use": "Use this vault",
    "workspaces.other.restart_note": "· Switching vaults applies after a restart.",
    "workspaces.list.heading": "Code folders · extra folders for new chats",
    "workspaces.list.empty": "No workspaces yet. Add folders to let chats work in them.",
    "workspaces.add.button": "Add folders…",
    "workspaces.add.pick_title": "Add folders to VIKI Agent",
    "workspaces.add.submit": "Add",
    "workspaces.add.result": "Added {added} of {total} folders",
    "workspaces.add.result_one": "Added {added} of 1 folder",
    "workspaces.add.added": "Added · {state}",
    "workspaces.add.already": "Already in your workspaces",
    "workspaces.add.refused": "Not added: {reason}",
    "workspaces.add.done": "Done",
    "workspaces.row.more": "More actions for {name}",
    "workspaces.row.details": "Details and trust",
    "workspaces.row.open_code": "Open in Code",
    "workspaces.row.remove_menu": "Remove from list…",
    "workspaces.row.remove": "Remove",
    "workspaces.row.remove_label": "Remove {name}",
    "workspaces.row.remove_confirm":
      "Remove {name} from VIKI Agent? Nothing on disk changes, and you can add the folder again later.",
    "workspaces.row.hint_untrusted": "Trust this folder to let chats work in it.",
    "workspaces.row.hint_missing":
      "This folder is gone from disk. Remove it, or put the folder back.",
    "workspaces.row.tick_cap": "Up to 7 folders. Untick one first.",
    "workspaces.summary.prefix": "New chats:",
    "workspaces.summary.one": "{vault} + 1 workspace",
    "workspaces.summary.many": "{vault} + {n} workspaces",
    "workspaces.summary.rule":
      "· up to 7 ticked code folders · applies to chats you start from now on; open chats keep their folders.",

    "workspaces.identity.established": "Git identity",
    "workspaces.identity.git_root_oids": "Git root commits found",
    "workspaces.identity.inherits_from": "Inherits from",
    "workspaces.overlaps.title": "Overlapping workspaces",
    "workspaces.excluded.title": "Paths note saves never write",
    "workspaces.excluded.title_code": "Files the Code editor won't save",

    "workspaces.policy.ordinary": "Ordinary",
    "workspaces.policy.existing_readonly": "Read-only (existing folder)",
    "workspaces.policy.protected": "Protected",
    "workspaces.policy.protected_locked": "A protected vault's policy cannot be changed.",
    "workspaces.policy.edit_in_vault":
      "Notes in this vault can be created and edited in the Vault view.",
    "workspaces.policy.read_only_notes":
      "VIKI edits notes only in a vault it created, so this folder stays read-only in the Vault view.",
    "workspaces.policy.bootstrap_pending":
      "This vault's setup has not finished, so its notes cannot be edited yet. Finish or cancel the setup first.",
    "workspaces.policy.form_title": "Change the policy",
    "workspaces.marker.present": "Present",
    "workspaces.marker.absent": "Not present",

    "workspaces.ack.instructions_present":
      "This folder already has CLAUDE.md/AGENTS.md content; the policy will not change it.",
    "workspaces.ack.hooks_present":
      "This folder already has Claude Code hooks configured; the policy will not change them.",
    "workspaces.ack.git_present":
      "This folder already has a Git repository; the policy will not change it.",
    "workspaces.ack.claude_config_present":
      "This folder supplies Claude Code configuration (.claude/ or .mcp.json). VIKI sessions run its hooks and helper commands and connect its MCP servers without asking.",

    /* Short labels for the three acknowledgement items, used to name them in a 409
     * acknowledgement_required's `extra.required` list — never the raw snake_case code. */
    "workspaces.ack_item.instructions_present": "Existing instructions",
    "workspaces.ack_item.hooks_present": "Existing hooks",
    "workspaces.ack_item.git_present": "Existing Git repository",
    "workspaces.ack_item.claude_config_present": "Claude Code configuration",

    "workspaces.admissible.protected_root": "A protected vault's policy cannot change.",
    "workspaces.admissible.root_unavailable":
      "The folder is not where it was registered, so its policy cannot change now.",

    "workspaces.provider.cli": "Claude Code",
    "workspaces.provider.version": "Version",
    "workspaces.provider.login": "Login",
    "workspaces.provider.billing": "Billing",
    "workspaces.provider.last_checked_at": "Last checked",
    "workspaces.provider.problems": "Problems",

    "workspaces.provider.cli.ready": "Ready",
    "workspaces.provider.cli.missing": "Not found",
    "workspaces.provider.cli.unusable": "Not runnable",
    "workspaces.provider.cli.config_dir_override": "Config dir override",

    "workspaces.provider.login.unknown": "Unknown",
    "workspaces.provider.login.signed_in": "Signed in",
    "workspaces.provider.login.signed_out": "Signed out",

    "workspaces.provider.billing.clear": "Clear",
    "workspaces.provider.billing.blocked": "Blocked",
    "workspaces.provider.billing.unknown": "Unknown",

    "workspaces.git.ready": "Ready",
    "workspaces.git.missing": "Not found",
    "workspaces.git.unknown": "Unknown",
    "workspaces.git.overridden": "Overridden",

    "workspaces.check.provider_cli": "Check Claude Code",
    "workspaces.check.git": "Check Git",
    "workspaces.check.status": "Status",
    "workspaces.check.status.ok": "OK",
    "workspaces.check.status.failed": "Failed",
    "workspaces.check.version": "Version",
    "workspaces.check.duration": "Duration",
    "workspaces.check.checked_at": "Checked at",

    "workspaces.setup.login":
      "If Claude Code asks you to sign in, choose Sign in in the chat, or run `claude auth login` in a terminal.",
    "workspaces.setup.config_dir": "Unset CLAUDE_CONFIG_DIR and restart VIKI Agent.",
    "workspaces.setup.missing":
      "Install Claude Code with Anthropic's installer, then restart VIKI Agent.",
    "workspaces.setup.unusable": "The configured path is a batch shim; point VIKI at claude.exe.",
    "workspaces.setup.billing":
      "Claude Code is signed in to, or set up to bill, an account other than your Claude subscription. Sign in with your Claude subscription, remove the setting that Settings › Providers lists, or turn on Let Claude Code use its other sign-in methods in Settings › Chat. Then restart VIKI Agent. A setting listed there also blocks chats on a model connection.",
    "workspaces.setup.terminal_note": "VIKI does not run this for you; do it in your own terminal.",

    "providers.problem.not_found": "Claude Code was not found on this machine.",
    "providers.problem.batch_shim": "The configured path is a batch wrapper, not claude.exe.",
    "providers.problem.config_dir_override":
      "CLAUDE_CONFIG_DIR is set; VIKI will not start sessions while it is.",
    "providers.problem.api_key_helper":
      "An apiKeyHelper is configured; the subscription login would not be used.",
    "providers.problem.billing_env_key":
      "An API key is set in the environment; the subscription login would not be used.",
    "providers.problem.federation_profile":
      "A federation profile is configured; the subscription login would not be used.",
    "providers.problem.other": "Claude Code's configuration blocks a subscription session.",

    "providers.version.older": "Older than tested ({min} to {max})",
    "providers.version.newer": "Newer than tested ({min} to {max})",
    "providers.source": "Installation",
    "providers.source.installed": "Your own installation",
    "providers.source.bundled": "SDK copy (source checkout)",
    "providers.source.override": "VIKI_CLAUDE_CLI",
    "providers.setup.update":
      "Update Claude Code to {min} or newer, then choose Check Claude Code:",
    "providers.setup.update_package":
      "Installed it with WinGet, Homebrew or a Linux package manager? Upgrade it there instead.",
    "providers.setup.guide": "Anthropic's setup guide",
    "providers.copy": "Copy the {method} command",
    "providers.method.powershell": "PowerShell",
    "providers.method.winget": "WinGet",
    "providers.method.terminal": "Terminal",
    "providers.method.homebrew": "Homebrew",
    "providers.method.claude": "Claude Code",

    "workspaces.diagnostics.reload": "Reload",
    "workspaces.diagnostics.paths_note":
      'Paths appear only when "Include folder paths in diagnostics" is on.',
    "workspaces.diagnostics.open_privacy": "Open Privacy settings",
    "workspaces.diagnostics.mode": "Mode",
    "workspaces.diagnostics.version": "Version",
    "workspaces.diagnostics.launch_id": "Launch",
    "workspaces.about.title": "About VIKI Agent",
    "workspaces.about.license": "VIKI Agent license",
    "workspaces.about.third_party_notices": "Third-party notices",
    "workspaces.about.whats_new": "What's new",
    "workspaces.about.help_unavailable": "Help is unavailable in this window.",
  },
});
