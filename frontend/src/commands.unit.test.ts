import { describe, expect, it } from "vitest";
import { type CommandId, commandFor, type Navigation, SHORTCUTS } from "./commands";
import type { ShellSnapshot } from "./state/shell";
import { capabilitiesFixture, fakeShellApi, listingFixture, workspaceFixture } from "./testShell";

const ALL_IDS: CommandId[] = [
  "session.new",
  "search.conversations",
  "search.vault",
  "command.run",
  "workspace.create",
  "workspace.open",
  "workspace.later",
  "workspace.select",
  "settings.open",
  "settings.search",
  "diagnostics.open",
  "diagnostics.check",
  "data.open",
  "data.export_diagnostics",
  "data.import_settings",
  "data.reset",
  "provider.setup",
];

function snap(overrides: Partial<ShellSnapshot> = {}): ShellSnapshot {
  return {
    capabilities: null,
    capabilitiesStatus: "unavailable",
    settings: null,
    settingsStatus: "unavailable",
    workspaces: null,
    workspacesStatus: "unavailable",
    errors: { capabilities: "", settings: "", workspaces: "" },
    recovery: false,
    checkRunning: null,
    ...overrides,
  };
}

const READY = snap({ capabilities: capabilitiesFixture(), capabilitiesStatus: "ready" });
const RECOVERY = snap({
  capabilities: capabilitiesFixture({
    app: { mode: "recovery" },
    storage: { state: "unavailable", reason: "corrupt" },
    // `can_start` is false whenever the store is not ready (src/viki_agent/app/diagnostics.py:207-211).
    provider: { can_start: false },
  }),
  capabilitiesStatus: "ready",
  recovery: true,
});

function reasonsFor(shell: ShellSnapshot): Record<string, string | null> {
  const table: Record<string, string | null> = {};
  for (const id of ALL_IDS) {
    const state = commandFor(id, shell);
    table[id] = state.enabled ? "enabled" : state.reason;
  }
  return table;
}

describe("the commandFor table", () => {
  it("unknown capabilities keep the core commands enabled and hold the rest", () => {
    expect(reasonsFor(snap())).toEqual({
      "session.new": "enabled",
      "search.conversations": "enabled",
      "search.vault": "reason.vault_search_in_view",
      "command.run": "enabled",
      "workspace.create": "reason.storage_not_ready",
      "workspace.open": "reason.storage_not_ready",
      "workspace.later": null,
      "workspace.select": "reason.no_workspaces",
      "settings.open": "enabled",
      "settings.search": null,
      "diagnostics.open": "enabled",
      "diagnostics.check": "enabled",
      "data.open": "enabled",
      "data.export_diagnostics": "enabled",
      "data.import_settings": "reason.storage_not_ready",
      "data.reset": "reason.storage_not_ready",
      "provider.setup": null,
    });
  });

  it("a ready launch opens the vault and data commands", () => {
    expect(reasonsFor(READY)).toEqual({
      "session.new": "enabled",
      "search.conversations": "enabled",
      "search.vault": "reason.vault_search_in_view",
      "command.run": "enabled",
      "workspace.create": "enabled",
      "workspace.open": "enabled",
      "workspace.later": null,
      "workspace.select": "reason.no_workspaces",
      "settings.open": "enabled",
      "settings.search": null,
      "diagnostics.open": "enabled",
      "diagnostics.check": "enabled",
      "data.open": "enabled",
      "data.export_diagnostics": "enabled",
      "data.import_settings": "enabled",
      "data.reset": "enabled",
      "provider.setup": null,
    });
  });

  it("recovery leaves diagnostics and says why about everything else", () => {
    expect(reasonsFor(RECOVERY)).toEqual({
      "session.new": "reason.provider_not_ready",
      "search.conversations": "reason.recovery",
      "search.vault": "reason.vault_search_in_view",
      "command.run": "reason.provider_not_ready",
      "workspace.create": "reason.recovery",
      "workspace.open": "reason.recovery",
      "workspace.later": null,
      "workspace.select": "reason.recovery",
      "settings.open": "reason.recovery",
      "settings.search": null,
      "diagnostics.open": "enabled",
      "diagnostics.check": "enabled",
      "data.open": "reason.recovery",
      "data.export_diagnostics": "reason.recovery_no_export",
      "data.import_settings": "reason.storage_not_ready",
      "data.reset": "reason.storage_not_ready",
      "provider.setup": "enabled",
    });
  });

  it("a native runtime billing refusal keeps the new-chat editor and blocks native commands", () => {
    const blocked = snap({
      capabilities: capabilitiesFixture({ provider: { can_start: false, billing: "blocked" } }),
      capabilitiesStatus: "ready",
    });
    expect(commandFor("session.new", blocked)).toEqual({
      enabled: true,
      reason: null,
    });
    expect(commandFor("command.run", blocked)).toEqual({
      enabled: false,
      reason: "reason.provider_not_ready",
    });
    expect(commandFor("provider.setup", blocked)).toEqual({ enabled: true, reason: null });
  });

  it("a real provider preflight problem still blocks the new-chat editor", () => {
    const blocked = snap({
      capabilities: capabilitiesFixture({
        provider: {
          can_start: false,
          billing: "blocked",
          problems: ["billing_configuration"],
        },
      }),
      capabilitiesStatus: "ready",
    });
    expect(commandFor("session.new", blocked).enabled).toBe(false);
  });

  it("a store that is not ready closes the writes, not the reads", () => {
    const readOnly = snap({
      capabilities: capabilitiesFixture({ storage: { state: "read_only", reason: "protection" } }),
      capabilitiesStatus: "ready",
    });
    for (const id of ["data.import_settings", "data.reset", "workspace.create"] as const)
      expect([id, commandFor(id, readOnly)]).toEqual([
        id,
        { enabled: false, reason: "reason.storage_not_ready" },
      ]);
    // Search reads the transcript catalog, and the locations and export panels read only; a
    // read-only app store closes none of them.
    expect(commandFor("search.conversations", readOnly).enabled).toBe(true);
    expect(commandFor("data.open", readOnly).enabled).toBe(true);
    expect(commandFor("data.export_diagnostics", readOnly).enabled).toBe(true);
  });

  it("vault search is enabled only by a feature the backend does not serve yet", () => {
    const later = snap({
      capabilities: capabilitiesFixture({ features: { vault_search: true } }),
      capabilitiesStatus: "ready",
    });
    expect(commandFor("search.vault", later)).toEqual({ enabled: true, reason: null });
  });

  it("workspace.select needs a registration", () => {
    const registered = snap({
      capabilities: capabilitiesFixture(),
      capabilitiesStatus: "ready",
      workspaces: listingFixture([workspaceFixture()]),
      workspacesStatus: "ready",
    });
    expect(commandFor("workspace.select", registered)).toEqual({ enabled: true, reason: null });
    expect(
      commandFor(
        "workspace.select",
        snap({ workspaces: listingFixture(), workspacesStatus: "ready" }),
      ),
    ).toEqual({ enabled: false, reason: "reason.no_workspaces" });
  });

  it("the in-view commands are available only from their own view", () => {
    const withSettings = snap({ settingsStatus: "ready" });
    expect(commandFor("workspace.later", withSettings, "onboarding").enabled).toBe(true);
    expect(commandFor("workspace.later", withSettings, "chat").enabled).toBe(false);
    // "Set up later" saves a setting, so it waits for a settings document.
    expect(commandFor("workspace.later", snap(), "onboarding").enabled).toBe(false);
    expect(commandFor("settings.search", READY, "settings").enabled).toBe(true);
    expect(commandFor("settings.search", READY, "chat").enabled).toBe(false);
  });

  it("the two checks have their own gates and one of them runs at a time", () => {
    expect(commandFor("diagnostics.check", READY, undefined, { check: "git" }).enabled).toBe(true);
    expect(
      commandFor("diagnostics.check", READY, undefined, { check: "provider_cli" }).enabled,
    ).toBe(true);
    const missing = snap({
      capabilities: capabilitiesFixture({ provider: { cli: "missing", can_start: false } }),
      capabilitiesStatus: "ready",
    });
    expect(commandFor("diagnostics.check", missing, undefined, { check: "provider_cli" })).toEqual({
      enabled: false,
      reason: "reason.cli_not_ready",
    });
    expect(commandFor("diagnostics.check", missing, undefined, { check: "git" }).enabled).toBe(
      true,
    );
    const busy = snap({ ...READY, checkRunning: "git" });
    for (const check of ["git", "provider_cli"] as const)
      expect(commandFor("diagnostics.check", busy, undefined, { check })).toEqual({
        enabled: false,
        reason: "reason.check_running",
      });
  });

  it("an enabled command never carries a reason", () => {
    for (const shell of [snap(), READY, RECOVERY])
      for (const id of ALL_IDS) {
        const state = commandFor(id, shell, "chat");
        if (state.enabled) expect([id, state.reason]).toEqual([id, null]);
      }
  });

  it("the navigation type App restores from sessionStorage is this module's", () => {
    // Compile-time more than run-time: a renamed member breaks `tsc` before it breaks a view.
    const start: Navigation = {
      view: "chat",
      page: "general",
      panel: "locations",
      onboardingMode: "choose",
    };
    expect(Object.keys(start).sort()).toEqual(["onboardingMode", "page", "panel", "view"]);
  });

  it("shortcuts are key names, never server text", () => {
    expect(SHORTCUTS).toEqual({
      "session.new": "Alt+N",
      "search.conversations": "Ctrl+K",
      "search.vault": "Ctrl+Shift+K",
      "settings.open": "Ctrl+,",
    });
  });

  it("the table answers from a snapshot the store really built", async () => {
    // The hand-built snapshots above could drift from what `createShellStore` commits; this reads
    // the table off the store's own snapshot after a real read through the fake.
    const { createShellStore } = await import("./state/shell");
    const store = createShellStore(fakeShellApi(), { subscribe: () => () => {} } as never);
    await store.refreshCapabilities();
    const shell = store.getSnapshot();
    expect(reasonsFor(shell)["workspace.create"]).toBe("enabled");
    expect(reasonsFor(shell)["workspace.select"]).toBe("reason.no_workspaces");
    expect(reasonsFor(shell)["session.new"]).toBe("enabled");
    expect(reasonsFor(shell)["search.vault"]).toBe("reason.vault_search_in_view");
  });
});
