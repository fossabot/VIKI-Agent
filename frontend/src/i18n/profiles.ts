import { defineCatalog } from "./defineCatalog";

export const catalog = defineCatalog({
  en: {
    "profiles.catalog.title": "Profiles",
    "profiles.model": "Model",
    "profiles.model.appDefault": "App default",
    "profiles.model.claudeDefault": "Claude Code's default",
    "profiles.model.saved": "Saved value",
    "profiles.model.help": "Choose a model from Claude Code's available models.",
    "profiles.model.empty": "No Claude Code models are available.",
    "profiles.model.loading": "Loading Claude Code models…",
    "profiles.model.failed": "Claude Code models could not be loaded.",
    "profiles.model.unavailable": "This saved model is unavailable.",
    "profiles.model.connected": "Connected model",
    "profiles.model.unavailableSuffix": "unavailable",
    "profiles.model.defaultHelp":
      "App default is {model}. Change it in Settings › Chat › Default model.",
    "profiles.model.claudeDefaultHelp": "App default uses Claude Code's default for this profile.",
    "profiles.model.group": "Claude Code models",
    "profiles.model.notChecked": "Claude Code model availability has not been checked.",
    "profiles.model.unverified": "Claude Code models are unverified.",
    "profiles.model.noneReported": "Claude Code reported no models.",
    "profiles.model.connections": "Your connections",
    "profiles.model.connectionRemoved": "Saved connection (removed or unreadable)",
    "profiles.model.connectionLoading": "Loading saved connection names…",
    "profiles.model.connectionFailed": "Saved connection names could not be loaded.",
    "profiles.startSeat": "Start from a seat",
    "profiles.seatStarterNote":
      "Fills the name, purpose and instructions with that seat's job description, ready to edit. Seats work the cards in kanban/ of a vault made with Start with VIKI.",
    "profiles.hint.mcpSaved": "These servers are saved with the profile.",
    "profiles.runtime.useClaudeCode": "Use Claude Code",
    "profiles.execution.context": "Context items",
    "profiles.execution.nativeAssets": "Native assets",
    "profiles.execution.requiredTools": "Required tools",
    "profiles.execution.deniedTools": "Denied tools",
    "profiles.execution.memory": "Memory",
    "profiles.execution.limits": "Limits",
    "profiles.workspace.anyVault": "This profile may start in any registered vault.",
    "profiles.technicalEvidence": "Technical evidence",
    "profiles.available": "Available.",
    "profiles.notEvaluated": "Not evaluated yet",
    "profiles.cannotApplyChat": "Cannot apply in a chat",
    "profiles.savedRuntimeLine":
      "Saved runtime: {provider}; model {model}; effort {effort}; permission {permission}",
    "profiles.tryChat.starting": "Starting chat…",
    "profiles.tryChat.notEvaluated": "Launch readiness has not been evaluated.",
    "profiles.tryChat.unavailable": "Try it in a chat is unavailable.",
    "profiles.tryChat.workspaceRequired": "Select an active workspace before starting a chat.",
    "profiles.tryChat.saveFirst": "Save this revision before starting a chat.",
    "profiles.tryChat.archived": "Archived profiles cannot start a chat.",
    "profiles.tryChat.blockersLabel": "Why this profile cannot start a chat",
    "profiles.tryChat.blockerFields": "Blocker fields: {fields}",
    "profiles.identity": "Identity",
    "profiles.instructions": "Instructions",
    "profiles.botMemoryRule":
      "Every chat, task thread and routine run of this Bot gets these instructions, followed by VIKI Agent's memory rule and Bot rules, as part of Claude Code's system prompt. They are not put in the message box, and you don't send them.",
    "profiles.modelPermissions": "Model and permissions",
    "profiles.whatItCanUse": "What it can use",
    "profiles.advanced": "Advanced",
    "profiles.field.model": "Model",
    "profiles.field.effort": "Effort",
    "profiles.field.vaults": "Vaults",
    "profiles.field.mcp": "MCP servers",
    "profiles.field.instructions": "Instructions",
    "profiles.hint.model": "Uses Claude Code's available models.",
    "profiles.hint.effort": "Choose how much reasoning Claude Code uses.",
    "profiles.hint.permission": "Choose whether Claude Code plans before acting.",
    "profiles.hint.vaults": "Choose which registered vaults this profile may use.",
    "profiles.hint.mcp": "Configure MCP servers for this profile.",
    "profiles.hint.instructions": "Give this profile its role and working guidance.",
    "profiles.field.permission": "Permission mode",
    "profiles.runtime.runsOn": "Runs on",
    "profiles.runtime.claudeCode": "Claude Code",
    "profiles.overview.permissionSetting": "Permission setting",
    "profiles.duplicateAsClaudeCode": "Duplicate as Claude Code",
    "profiles.runtime.legacyProvider":
      "This saved provider is no longer supported. Choose Claude Code to continue.",
    "profiles.effort.low": "Low",
    "profiles.effort.medium": "Medium",
    "profiles.effort.high": "High",
    "profiles.effort.xhigh": "Extra high",
    "profiles.effort.max": "Maximum",
    "profiles.permission.default": "Use my default",
    "profiles.permission.plan": "Plan first",
    "profiles.permission.blocked": "This saved permission mode is blocked.",
    "profiles.cannotApply.title": "Some saved settings cannot be applied",
    "profiles.clear": "Clear",
    "profiles.clearAll": "Clear all",
    "profiles.tryChat": "Try it in a chat",
    "profiles.startChat": "Start a chat",
    "profiles.technicalDetails": "Technical details",
    "profiles.savedRuntime": "Saved runtime",
    "profiles.fieldReason": "This field cannot be used: {reason}",
    "profiles.chat.transcript_missing": "The Bot chat transcript is missing. Start a new chat.",
    "profiles.chat.profile_archived":
      "The profile is archived. Restore it before starting a Bot chat.",
    "profiles.chat.workspace_restart_required":
      "Restart the workspace before starting this Bot chat.",
    "profiles.chat.workspace_unavailable":
      "The workspace is unavailable. Choose an available workspace.",
    "profiles.chat.profile_blocked": "The Bot chat is archived. Create a new chat to continue.",
    "profiles.chat.launch_blocked":
      "This profile has settings that block launch. Review its saved settings.",
    "profiles.chat.capability_changed":
      "Available capabilities changed. Reload the profile and try again.",
    "profiles.chat.launch_changed": "The launch details changed. Reload the profile and try again.",
    "profiles.chat.unavailable": "The Bot chat is unavailable. Refresh Bots and try again.",
    "profiles.chat.app_closing": "VIKI Agent is closing. Try again after it restarts.",
    "profiles.chat.provider_unavailable":
      "Claude Code is unavailable. Check its setup and try again.",
    "profiles.chat.profile_missing": "The Bot profile is missing. Choose another profile.",
    "profiles.chat.workspace_missing": "The Bot workspace is missing. Choose another workspace.",
    "profiles.chat.session_exists":
      "A chat already exists for this Bot. Open it or create a new chat.",
    "profiles.chat.corrupt": "The saved Bot chat could not be read. Refresh Bots to try again.",
    "profiles.chat.profile_changed": "The profile changed. Refresh Bots and try again.",
    "profiles.chat.conflict": "The Bot changed elsewhere. Refresh Bots and try again.",
    "profiles.chat.command_conflict":
      "This Bot action conflicts with a recent change. Refresh Bots and try again.",
    "profiles.chat.capacity": "VIKI Agent has reached its Bot limit.",
    "profiles.chat.delivery_unavailable":
      "The Bot reply could not be delivered. Check the chat and try again.",
    "profiles.reason.invalid_input": "Some profile settings are invalid.",
    "profiles.reason.invalid_catalog": "The model catalog is invalid or unavailable.",
    "profiles.reason.newer_version": "This profile was saved by a newer version of VIKI Agent.",
    "profiles.reason.protection": "The profile could not be unlocked.",
    "profiles.reason.mcp_revoked":
      "An MCP server permission changed. Review the profile and try again.",
    "profiles.reason.mcp_config_unavailable": "The MCP configuration is unavailable.",
    "profiles.reason.settings_unavailable": "Claude Code settings are unavailable.",
    "profiles.reason.conflict": "The profile changed elsewhere. Reload it and try again.",
    "profiles.reason.command_conflict": "This profile action conflicts with a recent change.",
    "profiles.reason.capacity": "VIKI Agent has reached a profile limit.",
    "profiles.reason.not_found": "The profile could not be found.",
    "profiles.reason.corrupt": "The saved profile could not be read.",
    "profiles.reason.invalid_id": "The profile identifier is invalid.",
    "profiles.reason.invalid_revision": "The profile revision is invalid.",
    "profiles.reason.transcript_missing": "The chat transcript is missing.",
    "profiles.reason.profile_archived": "The profile is archived.",
    "profiles.reason.workspace_restart_required": "Restart the workspace before continuing.",
    "profiles.reason.workspace_unavailable": "The workspace is unavailable.",
    "profiles.reason.profile_blocked": "The Bot chat is archived.",
    "profiles.reason.launch_blocked": "Saved profile settings block launch.",
    "profiles.reason.launch_changed": "The launch details changed.",
    "profiles.reason.capability_changed": "Available capabilities changed.",
    "profiles.reason.provider_unsupported":
      "This profile uses a provider that Claude Code cannot run.",
    "profiles.reason.model_unobserved":
      "Claude Code has not confirmed that this model is available.",
    "profiles.reason.effort_unsupported": "Claude Code does not support this effort setting.",
    "profiles.reason.effort_unverified":
      "Claude Code has not confirmed that this effort setting can be applied.",
    "profiles.reason.plan_mode_unsupported": "Claude Code does not support this plan mode.",
    "profiles.reason.plan_mode_unverified":
      "Claude Code has not confirmed that this plan mode can be applied.",
    "profiles.reason.permission_unverified":
      "Claude Code has not confirmed that this permission mode can be applied.",
    "profiles.reason.workspace_resolution_required":
      "Choose an available workspace so the selected folder can be resolved.",
    "profiles.reason.context_unresolved":
      "The selected context could not be resolved and will not be applied.",
    "profiles.reason.native_asset_unresolved":
      "The selected Claude Code asset could not be resolved and will not be applied.",
    "profiles.reason.required_tools_unproven":
      "The required tool list has not been verified as enforceable.",
    "profiles.reason.denied_tools_unproven":
      "The denied tool list has not been verified as enforceable.",
    "profiles.reason.mcp_config_invalid": "The saved MCP server configuration is invalid.",
    "profiles.reason.mcp_config_unproven":
      "The saved MCP server configuration has not been verified as usable.",
    "profiles.reason.memory_isolation_unproven":
      "The custom memory setting has not been verified as applied. Bot memory uses a separate setting.",
    "profiles.reason.timeout_unproven": "The timeout limit has not been verified as enforceable.",
    "profiles.reason.max_root_tasks_unproven":
      "The maximum root tasks limit has not been verified as enforceable.",
    "profiles.reason.token_budget_unproven":
      "The token budget limit has not been verified as enforceable.",
    "profiles.reason.currency_budget_unproven":
      "The currency budget limit has not been verified as enforceable.",
    "profiles.reason.evidence_unavailable":
      "This Claude Code version cannot verify whether this setting can be applied.",
    "profiles.reason.adapter_unsupported": "This profile setting is not supported.",
    "profiles.reason.workspace_not_allowed": "This workspace is not allowed by the profile.",
    "profiles.reason.mcp_policy_blocked":
      "This profile's MCP servers cannot run in this workspace.",
    "profiles.reason.connection_bot_only":
      "Only this Bot's own chats, task threads and Bot-thread turns can use its model connection.",
    "profiles.reason.connection_missing": "This Bot's saved model connection was removed.",
    "profiles.reason.connections_unavailable":
      "Your model connections could not be read, so this Bot cannot start.",
    "profiles.reason.connection_invalid": "This Bot's saved model connection is not valid.",
    "profiles.reason.connection_resolution_required":
      "This profile's model connection has not been checked yet.",
    "profiles.permission.blockedMode": "The saved permission mode ({mode}) is unavailable.",
    "profiles.problem.unknown": "This setting cannot be used ({code}).",
  },
});
