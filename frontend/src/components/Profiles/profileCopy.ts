import { en, type Key } from "../../i18n";
import { t, tf } from "../../i18n/t";
import { reasonText as routineReasonText } from "../../state/routines";

type ProblemContext = {
  reason?: string | null;
  permissionMode?: string | null;
};

const fieldReason: Readonly<Record<string, string>> = {
  "runtime.provider": "provider_unsupported",
  "runtime.model": "model_unobserved",
  "runtime.effort": "evidence_unavailable",
  "runtime.permission_mode": "permission_unverified",
  "workspace.allowed_workspace_ids": "workspace_resolution_required",
  "context.items": "context_unresolved",
  native_assets: "native_asset_unresolved",
  "tools.required": "required_tools_unproven",
  "tools.denied": "denied_tools_unproven",
  "tools.mcp_configuration": "mcp_config_unproven",
  "memory.mode": "memory_isolation_unproven",
  "memory.sources": "memory_isolation_unproven",
  "limits.timeout_seconds": "timeout_unproven",
  "limits.max_root_tasks": "max_root_tasks_unproven",
  "limits.token_budget": "token_budget_unproven",
  "limits.currency_budget": "currency_budget_unproven",
};

function catalogText(code: string): string | null {
  const key = `profiles.reason.${code}` as Key;
  return key in en ? t(key) : null;
}

function reasonCopy(code: string): string {
  if (code !== "other" && `routines.reason.${code}` in en) {
    return routineReasonText(code);
  }
  return catalogText(code) ?? tf("profiles.problem.unknown", { code });
}

const chatCodes = new Set([
  "transcript_missing",
  "profile_archived",
  "workspace_restart_required",
  "workspace_unavailable",
  "profile_blocked",
  "launch_blocked",
  "launch_changed",
  "capability_changed",
  "app_closing",
  "provider_unavailable",
  "profile_missing",
  "workspace_missing",
  "session_exists",
  "corrupt",
]);

/** Plain explanation for a profile field, its evaluation reason, or a Bot chat blocker. */
export function problemText(code: string, context: ProblemContext = {}): string {
  if (code === "runtime.permission_mode" && context.permissionMode && !context.reason) {
    return tf("profiles.permission.blockedMode", { mode: context.permissionMode });
  }

  const field = Object.hasOwn(fieldReason, code) ? fieldReason[code] : undefined;
  if (field) {
    const reason = context.reason ?? field;
    return tf("profiles.fieldReason", { reason: reasonCopy(reason) });
  }

  if (chatCodes.has(code)) return t(`profiles.chat.${code}` as Key);
  const reason = catalogText(code);
  if (reason) return reason;
  if (`routines.reason.${code}` in en && code !== "other") return routineReasonText(code);
  return tf("profiles.problem.unknown", { code });
}
