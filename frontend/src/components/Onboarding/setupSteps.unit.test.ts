import { describe, expect, it } from "vitest";
import type { ActiveSummary, WorkspaceJson } from "../../clients/app/shellApi";
import type { ShellSnapshot } from "../../state/shell";
import {
  capabilitiesFixture,
  listingFixture,
  settingsFixture,
  workspaceFixture,
} from "../../testShell";
import type { ConnectionLabels } from "../Profiles/modelLabel";
import { CLAUDE_SETUP_URL, setupSteps } from "./setupSteps";

type CapabilitiesArg = Parameters<typeof capabilitiesFixture>[0];

function snapshotWith(
  overrides: CapabilitiesArg = {},
  rows: WorkspaceJson[] | null = [],
  settings: Record<string, unknown> | null = {},
): ShellSnapshot {
  return {
    capabilities: capabilitiesFixture(overrides),
    capabilitiesStatus: "ready",
    settings: settings === null ? null : settingsFixture(settings),
    settingsStatus: settings === null ? "unavailable" : "ready",
    workspaces: rows === null ? null : listingFixture(rows),
    workspacesStatus: rows === null ? "unavailable" : "ready",
    errors: { capabilities: "", settings: "", workspaces: "" },
    recovery: false,
    checkRunning: null,
  };
}

const NO_CAPABILITIES: ShellSnapshot = { ...snapshotWith(), capabilities: null };

const LISTED: ConnectionLabels = {
  status: "ready",
  labels: [{ model_ref: "viki-llm/c1/m", name: "Local", model: "m" }],
};

const ACTIVE_VAULT: ActiveSummary = {
  id: "vault-1",
  kind: "vault",
  name: "My notes",
  policy: "ordinary",
  layout: "unknown",
  source: "registration",
  aliased: false,
  root_available: true,
};

describe("setupSteps constants", () => {
  it("names Anthropic's setup guide", () => {
    expect(CLAUDE_SETUP_URL).toBe("https://code.claude.com/docs/en/setup");
  });
});

describe("setupSteps claude", () => {
  it("is checking until capabilities are read", () => {
    expect(setupSteps(NO_CAPABILITIES).claude).toEqual({ state: "checking" });
  });

  it.each([
    ["missing", "workspaces.setup.missing"],
    ["unusable", "workspaces.setup.unusable"],
    ["config_dir_override", "workspaces.setup.config_dir"],
  ] as const)("is needed with the %s problem text", (cli, problem) => {
    const snapshot = snapshotWith({ provider: { cli } });
    expect(setupSteps(snapshot).claude).toEqual({ state: "needed", problem });
  });

  it("is done with the version and its tested range", () => {
    expect(setupSteps(snapshotWith({ provider: { version: "2.1.281" } })).claude).toEqual({
      state: "done",
      version: "2.1.281",
      older: false,
      tested: ["2.1.281", "2.1.282"],
    });
  });

  it("flags an older version", () => {
    const claude = setupSteps(snapshotWith({ provider: { version_status: "older" } })).claude;
    expect(claude).toMatchObject({ state: "done", older: true });
  });

  it("passes an unknown version through as empty", () => {
    const snapshot = snapshotWith({ provider: { version: "", version_status: "unknown" } });
    expect(setupSteps(snapshot).claude).toMatchObject({ state: "done", version: "", older: false });
  });
});

describe("setupSteps model", () => {
  it("is checking until capabilities are read", () => {
    expect(setupSteps(NO_CAPABILITIES).model).toEqual({ state: "checking" });
  });

  it.each(["missing", "unusable", "config_dir_override"] as const)(
    "waits for step 1 when the cli is %s, whatever else is true",
    (cli) => {
      const snapshot = snapshotWith({ provider: { cli, login: "signed_in" } }, [], {
        "chat.default_model": "viki-llm/c1/m",
      });
      expect(setupSteps(snapshot).model).toEqual({ state: "waiting" });
    },
  );

  it("is done on the subscription when signed in", () => {
    expect(setupSteps(snapshotWith()).model).toEqual({ state: "done", route: "subscription" });
  });

  it("is needed when signed out", () => {
    const snapshot = snapshotWith({ provider: { login: "signed_out" } });
    expect(setupSteps(snapshot).model).toEqual({ state: "needed", billing: false });
  });

  it("is not checked yet when the login is unknown", () => {
    const snapshot = snapshotWith({ provider: { login: "unknown" } });
    expect(setupSteps(snapshot).model).toEqual({ state: "unchecked" });
  });

  it("is needed with billing when billing is blocked, even signed in", () => {
    const snapshot = snapshotWith({ provider: { billing: "blocked", login: "signed_in" } });
    expect(setupSteps(snapshot).model).toEqual({ state: "needed", billing: true });
  });

  it("is needed with billing when billing is blocked and the login is unknown", () => {
    const snapshot = snapshotWith({ provider: { billing: "blocked", login: "unknown" } });
    expect(setupSteps(snapshot).model).toEqual({ state: "needed", billing: true });
  });

  it("is done on the connection when the default model is a connection, without problems", () => {
    const snapshot = snapshotWith({ provider: { login: "unknown" } }, [], {
      "chat.default_model": "viki-llm/c1/m",
    });
    expect(setupSteps(snapshot, LISTED).model).toEqual({ state: "done", route: "connection" });
  });

  it.each([
    [
      "is checking while the connections are being listed",
      { status: "loading", labels: [] },
      {
        state: "checking",
      },
    ],
    [
      "is unchecked when the connections cannot be listed",
      { status: "failed", labels: [] },
      {
        state: "unchecked",
        connection: "unread",
      },
    ],
    [
      "is needed when the default's connection is no longer listed",
      {
        status: "ready",
        labels: [{ model_ref: "viki-llm/c2/m", name: "Other", model: "m" }],
      },
      { state: "needed", billing: false, connection: "missing" },
    ],
  ] as const)("%s, never done from the setting alone", (_name, connections, expected) => {
    const snapshot = snapshotWith({ provider: { login: "signed_in" } }, [], {
      "chat.default_model": "viki-llm/c1/m",
    });
    expect(setupSteps(snapshot, connections).model).toEqual(expected);
  });

  it("is unchecked, not done, for a connection default when no list is given", () => {
    const snapshot = snapshotWith({}, [], { "chat.default_model": "viki-llm/c1/m" });
    expect(setupSteps(snapshot).model).toEqual({ state: "unchecked", connection: "unread" });
  });

  it.each(["tested", "newer"] as const)(
    "stays done on the connection when the version is %s",
    (version_status) => {
      const snapshot = snapshotWith({ provider: { version_status } }, [], {
        "chat.default_model": "viki-llm/c1/m",
      });
      expect(setupSteps(snapshot, LISTED).model).toEqual({ state: "done", route: "connection" });
    },
  );

  it.each([
    ["an older", { version: "2.0.0", version_status: "older" }],
    ["an unknown", { version: "", version_status: "unknown" }],
  ] as const)("is needed on the connection when the version is %s", (_name, provider) => {
    const snapshot = snapshotWith({ provider }, [], { "chat.default_model": "viki-llm/c1/m" });
    expect(setupSteps(snapshot, LISTED).model).toEqual({
      state: "needed",
      billing: false,
      connectionMin: "2.1.281",
    });
  });

  it("is needed with billing when the connection default has problems", () => {
    const snapshot = snapshotWith({ provider: { problems: ["ANTHROPIC_API_KEY"] } }, [], {
      "chat.default_model": "viki-llm/c1/m",
    });
    expect(setupSteps(snapshot, LISTED).model).toEqual({ state: "needed", billing: true });
  });

  it("ignores a connection default when settings are not read", () => {
    const snapshot = snapshotWith({ provider: { login: "signed_out" } }, [], null);
    expect(setupSteps(snapshot).model).toEqual({ state: "needed", billing: false });
  });

  it("ignores a native model default", () => {
    const snapshot = snapshotWith({ provider: { login: "unknown" } }, [], {
      "chat.default_model": "sonnet",
    });
    expect(setupSteps(snapshot).model).toEqual({ state: "unchecked" });
  });
});

describe("setupSteps vault", () => {
  it("is checking until capabilities are read", () => {
    expect(setupSteps(NO_CAPABILITIES).vault).toEqual({ state: "checking" });
  });

  it("is needed on the scratch folder", () => {
    expect(setupSteps(snapshotWith()).vault).toEqual({ state: "needed", problem: null });
  });

  it("is done with the vault's name when a vault is active", () => {
    const snapshot = snapshotWith({
      workspace: { active: ACTIVE_VAULT, chat_only: false, selected_id: "vault-1" },
    });
    expect(setupSteps(snapshot).vault).toEqual({ state: "done", name: "My notes" });
  });

  it("is needed, not done, while the active vault's folder cannot be reached", () => {
    const snapshot = snapshotWith({
      workspace: {
        active: { ...ACTIVE_VAULT, root_available: false },
        chat_only: false,
        selected_id: "vault-1",
      },
    });
    expect(setupSteps(snapshot).vault).toEqual({
      state: "needed",
      problem: "error.root_unavailable",
    });
  });

  it("is needed for an active code folder", () => {
    const snapshot = snapshotWith({
      workspace: {
        active: { ...ACTIVE_VAULT, kind: "code", name: "app" },
        chat_only: false,
        selected_id: "vault-1",
      },
    });
    expect(setupSteps(snapshot).vault).toEqual({ state: "needed", problem: null });
  });

  it("needs a restart once a vault is registered", () => {
    const snapshot = snapshotWith({ workspace: { restart_required: true, selected_id: "w1" } }, [
      workspaceFixture({ workspace_id: "w1" }),
    ]);
    expect(setupSteps(snapshot).vault).toEqual({ state: "restart" });
  });

  it("trusts the restart when the listing is unread", () => {
    const snapshot = snapshotWith(
      { workspace: { restart_required: true, selected_id: "w1" } },
      null,
    );
    expect(setupSteps(snapshot).vault).toEqual({ state: "restart" });
  });

  it("trusts the restart when the listing no longer has the selected row", () => {
    const snapshot = snapshotWith({ workspace: { restart_required: true, selected_id: "w1" } }, []);
    expect(setupSteps(snapshot).vault).toEqual({ state: "restart" });
  });

  it("is needed with the pending-bootstrap problem", () => {
    const snapshot = snapshotWith({ workspace: { restart_required: true, selected_id: "w1" } }, [
      workspaceFixture({ workspace_id: "w1", bootstrap: "pending" }),
    ]);
    expect(setupSteps(snapshot).vault).toEqual({
      state: "needed",
      problem: "workspaces.chip.bootstrap_pending",
    });
  });

  it("is needed with the root-unavailable problem", () => {
    const snapshot = snapshotWith({ workspace: { restart_required: true, selected_id: "w1" } }, [
      workspaceFixture({ workspace_id: "w1", root_available: false }),
    ]);
    expect(setupSteps(snapshot).vault).toEqual({
      state: "needed",
      problem: "error.root_unavailable",
    });
  });

  it("is needed, not restart, when the selected row is a code folder", () => {
    const snapshot = snapshotWith({ workspace: { restart_required: true, selected_id: "w1" } }, [
      workspaceFixture({ workspace_id: "w1", kind: "code" }),
    ]);
    expect(setupSteps(snapshot).vault).toEqual({ state: "needed", problem: null });
  });

  it("is needed while restart is not required, whatever the listing holds", () => {
    const snapshot = snapshotWith({ workspace: { selected_id: "w1" } }, [
      workspaceFixture({ workspace_id: "w1" }),
    ]);
    expect(setupSteps(snapshot).vault).toEqual({ state: "needed", problem: null });
  });
});
