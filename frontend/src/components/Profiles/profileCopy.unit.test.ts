import { describe, expect, it } from "vitest";
import { problemText } from "./profileCopy";

describe("problemText", () => {
  it("explains every profile field path", () => {
    const fields = [
      "runtime.provider",
      "runtime.model",
      "runtime.effort",
      "runtime.permission_mode",
      "workspace.allowed_workspace_ids",
      "context.items",
      "native_assets",
      "tools.required",
      "tools.denied",
      "tools.mcp_configuration",
      "memory.mode",
      "memory.sources",
      "limits.timeout_seconds",
      "limits.max_root_tasks",
      "limits.token_budget",
      "limits.currency_budget",
    ];
    for (const field of fields) {
      expect(problemText(field)).not.toBe(field);
      expect(problemText(field)).not.toContain("Other (");
    }
  });

  it("uses the reason for each evaluated field, including version evidence", () => {
    expect(problemText("runtime.model", { reason: "model_unobserved" })).toBe(
      "This field cannot be used: Claude Code has not confirmed that this model is available.",
    );
    expect(problemText("runtime.effort", { reason: "evidence_unavailable" })).toBe(
      "This field cannot be used: This Claude Code version cannot verify whether this setting can be applied.",
    );
    expect(problemText("runtime.model", { reason: "effort_unsupported" })).toBe(
      "This field cannot be used: Claude Code does not support this effort setting.",
    );
    expect(problemText("runtime.model", { reason: "adapter_unsupported" })).toBe(
      "This field cannot be used: This profile setting is not supported.",
    );
    expect(
      problemText("workspace.allowed_workspace_ids", { reason: "workspace_not_allowed" }),
    ).toBe("This field cannot be used: This workspace is not allowed by the profile.");
    expect(problemText("tools.mcp_configuration", { reason: "mcp_policy_blocked" })).toBe(
      "This field cannot be used: This profile's MCP servers cannot run in this workspace.",
    );

    const reasons = [
      "provider_unsupported",
      "adapter_unsupported",
      "model_unobserved",
      "effort_unsupported",
      "effort_unverified",
      "plan_mode_unsupported",
      "plan_mode_unverified",
      "permission_unverified",
      "workspace_resolution_required",
      "context_unresolved",
      "native_asset_unresolved",
      "required_tools_unproven",
      "denied_tools_unproven",
      "mcp_config_invalid",
      "mcp_config_unproven",
      "memory_isolation_unproven",
      "timeout_unproven",
      "max_root_tasks_unproven",
      "token_budget_unproven",
      "currency_budget_unproven",
      "evidence_unavailable",
    ];
    for (const reason of reasons) {
      const text = problemText("runtime.model", { reason });
      expect(text).not.toBe(`This field cannot be used: This setting cannot be used (${reason}).`);
      expect(text).not.toContain(`(${reason})`);
      expect(text).not.toContain(`Other (${reason})`);
    }
  });

  it("explains stored permission modes in plain language", () => {
    expect(problemText("runtime.permission_mode", { permissionMode: "plan" })).toBe(
      "The saved permission mode (plan) is unavailable.",
    );
    expect(
      problemText("runtime.permission_mode", {
        reason: "evidence_unavailable",
        permissionMode: "plan",
      }),
    ).toBe(
      "This field cannot be used: This Claude Code version cannot verify whether this setting can be applied.",
    );
  });

  it("explains every Bot status and reachable blocker", () => {
    const codes = [
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
      "mcp_revoked",
      "not_found",
      "newer_version",
      "protection",
    ];
    for (const code of codes) {
      const text = problemText(code);
      expect(text).not.toBe(code);
      expect(text).not.toBe(`This setting cannot be used (${code}).`);
      expect(text).not.toContain(`Other (${code})`);
    }
  });

  it("explains the model connection refusals in plain sentences, bare or on the model field", () => {
    const codes = [
      "connection_bot_only",
      "connection_missing",
      "connections_unavailable",
      "connection_invalid",
    ];
    const seen = new Set<string>();
    for (const code of codes) {
      const text = problemText(code);
      expect(text).not.toContain(code);
      expect(problemText("runtime.model", { reason: code })).toContain(text);
      seen.add(text);
    }
    expect(seen.size).toBe(codes.length);
    expect(problemText("connection_bot_only")).toBe(
      "Only this Bot's own chats, task threads and Bot-thread turns can use its model connection.",
    );
  });

  it("gives connection_resolution_required its own sentence", () => {
    const text = problemText("connection_resolution_required");
    expect(text).toBe("This profile's model connection has not been checked yet.");
    expect(problemText("runtime.model", { reason: "connection_resolution_required" })).toContain(
      text,
    );
  });

  it("uses routine copy for matching routine reasons", () => {
    expect(problemText("quota")).toBe("Usage limit reached");
  });

  it("renders an unknown identifier as text in the generic sentence", () => {
    expect(problemText("future_status")).toBe("This setting cannot be used (future_status).");
    for (const code of ["constructor", "toString", "__proto__"]) {
      expect(problemText(code)).toBe(`This setting cannot be used (${code}).`);
    }
  });
});
