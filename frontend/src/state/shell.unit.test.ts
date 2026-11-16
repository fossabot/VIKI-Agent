import { describe, expect, it } from "vitest";
import type { Capabilities, CheckResult, SettingsView } from "../clients/app/shellApi";
import { ShellApiError } from "../clients/app/shellApi";
import {
  capabilitiesFixture,
  deferredShellApi,
  FAKE_WORKSPACE_ID,
  fakeShellApi,
  listingFixture,
  settingsFixture,
  workspaceFixture,
} from "../testShell";
import {
  canStart,
  createShellStore,
  type ShellSnapshot,
  type ShellStore,
  vaultSurfaces,
} from "./shell";
import { AppStore } from "./store";

const CHECK: CheckResult = {
  check: "git",
  status: "ok",
  result: { version: "2.45.0" },
  checked_at: "2026-09-17T00:00:00.000Z",
  duration_ms: 10,
};

function methodsOf(calls: { method: string }[]): string[] {
  return calls.map((call) => call.method);
}

/** Settle the microtask chain `start()` builds, with no timer and no sleep. */
async function flush(): Promise<void> {
  for (let turn = 0; turn < 10; turn += 1) await Promise.resolve();
}

/** A snapshot with nothing read yet, for the two pure derivations. */
function bareSnapshot(capabilities: Capabilities | null): ShellSnapshot {
  return {
    capabilities,
    capabilitiesStatus: capabilities === null ? "unavailable" : "ready",
    settings: null,
    settingsStatus: "unavailable",
    workspaces: null,
    workspacesStatus: "unavailable",
    errors: { capabilities: "", settings: "", workspaces: "" },
    recovery: false,
    checkRunning: null,
  };
}

async function refusal(promise: Promise<unknown>): Promise<ShellApiError> {
  try {
    await promise;
  } catch (error) {
    if (error instanceof ShellApiError) return error;
    throw error;
  }
  throw new Error("expected a ShellApiError");
}

function counted(store: ShellStore): { count: () => number } {
  let notifications = 0;
  store.subscribe(() => {
    notifications += 1;
  });
  return { count: () => notifications };
}

/** A settings document at `revision` with `stored` on top. `settingsFixture` fills in only its
 * own spec's keys, which predate `layout.panes`, so the keys are added here by hand. */
function documentAt(revision: number, stored: Record<string, unknown>): SettingsView {
  const view = settingsFixture({}, { revision });
  return {
    ...view,
    values: { ...view.values, ...stored },
    effective: { ...view.effective, ...stored },
  };
}

/** The server's 409 once the document has moved on: the document it holds now, if readable. */
function staleRevision(current: SettingsView | null): ShellApiError {
  return new ShellApiError(409, "stale_revision", {
    current,
    currentRevision: current?.revision ?? null,
  });
}

describe("shell store", () => {
  it("without a shell api nothing changes", () => {
    const store = createShellStore(null, new AppStore());
    const first = store.getSnapshot();
    expect(first).toEqual({
      capabilities: null,
      capabilitiesStatus: "unavailable",
      settings: null,
      settingsStatus: "unavailable",
      workspaces: null,
      workspacesStatus: "unavailable",
      errors: { capabilities: "", settings: "", workspaces: "" },
      recovery: false,
      checkRunning: null,
    });
    const notifications = counted(store);
    store.start();
    store.stop();
    return Promise.all([
      store.refreshCapabilities(),
      store.refreshSettings(),
      store.refreshWorkspaces(),
      store.afterMutation(["capabilities", "settings", "workspaces"]),
    ]).then(async () => {
      expect(store.getSnapshot()).toBe(first);
      expect(notifications.count()).toBe(0);
      // The two calls with no honest value to resolve with say so instead of inventing one.
      expect((await refusal(store.patch({}))).code).toBe("unavailable");
      expect((await refusal(store.runCheck("git"))).code).toBe("unavailable");
      expect(store.getSnapshot()).toBe(first);
    });
  });

  it("a slower earlier read never overwrites a fresher one", async () => {
    const api = deferredShellApi();
    const store = createShellStore(api, new AppStore());
    const first = store.refreshCapabilities();
    const second = store.refreshCapabilities();
    expect(methodsOf(api.pending)).toEqual(["capabilities", "capabilities"]);

    api.pending[1]?.resolve(capabilitiesFixture({ launch: { id: "newer" } }));
    await second;
    expect(store.getSnapshot().capabilities?.launch.id).toBe("newer");

    api.pending[0]?.resolve(capabilitiesFixture({ launch: { id: "older" } }));
    await first;
    expect(store.getSnapshot().capabilities?.launch.id).toBe("newer");
    expect(store.getSnapshot().capabilitiesStatus).toBe("ready");
  });

  it("an older read's failure never marks the fresher one broken", async () => {
    const api = deferredShellApi();
    const store = createShellStore(api, new AppStore());
    const first = store.refreshWorkspaces();
    const second = store.refreshWorkspaces();
    api.pending[1]?.resolve(listingFixture([workspaceFixture()]));
    await second;
    api.pending[0]?.reject(new ShellApiError(503, "corrupt"));
    await first;
    expect(store.getSnapshot().workspacesStatus).toBe("ready");
    expect(store.getSnapshot().errors.workspaces).toBe("");
    expect(store.getSnapshot().workspaces?.workspaces).toHaveLength(1);
  });

  it("a failure keeps what is on screen and names the code", async () => {
    const api = fakeShellApi();
    const store = createShellStore(api, new AppStore());
    await store.refreshCapabilities();
    api.fail("capabilities", new ShellApiError(503, "corrupt"));
    await store.refreshCapabilities();
    expect(store.getSnapshot().capabilities).not.toBeNull();
    expect(store.getSnapshot().capabilitiesStatus).toBe("error");
    expect(store.getSnapshot().errors.capabilities).toBe("corrupt");
    await store.refreshCapabilities();
    expect(store.getSnapshot().capabilitiesStatus).toBe("ready");
    expect(store.getSnapshot().errors.capabilities).toBe("");
  });

  it("a retry's loading state does not clear the stale failure code (a view must gate on status)", async () => {
    // Nothing renders `errors[kind]` in production; this pins the
    // contract a future view must not get wrong: the code alone is never proof of a live error,
    // since it survives into the next read's "loading" state -- only the status is.
    const api = deferredShellApi();
    const store = createShellStore(api, new AppStore());
    const first = store.refreshWorkspaces();
    api.pending[0]?.reject(new ShellApiError(503, "corrupt"));
    await first;
    expect(store.getSnapshot().workspacesStatus).toBe("error");
    expect(store.getSnapshot().errors.workspaces).toBe("corrupt");

    const retry = store.refreshWorkspaces();
    expect(store.getSnapshot().workspacesStatus).toBe("loading");
    expect(store.getSnapshot().errors.workspaces).toBe("corrupt");

    api.pending[1]?.resolve(listingFixture([workspaceFixture()]));
    await retry;
    expect(store.getSnapshot().workspacesStatus).toBe("ready");
    expect(store.getSnapshot().errors.workspaces).toBe("");
  });

  it("a reconnect re-reads capabilities and nothing else", async () => {
    const api = fakeShellApi();
    const appStore = new AppStore();
    const store = createShellStore(api, appStore);
    store.start();
    await flush();
    expect(methodsOf(api.calls)).toEqual(["capabilities", "settings", "workspaces"]);

    appStore.setStatus("open");
    expect(methodsOf(api.calls).slice(3)).toEqual(["capabilities"]);
    // Still open: nothing transitioned, so nothing is re-read.
    appStore.setStatus("open");
    expect(api.calls).toHaveLength(4);

    // AppStore notifies on every commit, not only on a status change. A live turn commits per
    // streamed frame, and each one would be a GET /api/v2/capabilities without the guard.
    for (let frame = 0; frame < 3; frame += 1)
      appStore.update("11111111-1111-4111-8111-111111111111", (view) => ({
        ...view,
        seq: view.seq + 1,
      }));
    expect(appStore.getSnapshot().status).toBe("open");
    expect(api.calls).toHaveLength(4);

    appStore.setStatus("reconnecting");
    appStore.setStatus("open");
    expect(methodsOf(api.calls).slice(4)).toEqual(["capabilities"]);

    store.stop();
    appStore.setStatus("reconnecting");
    appStore.setStatus("open");
    expect(api.calls).toHaveLength(5);
  });

  it("recovery mode is derived from the capabilities it read", async () => {
    const api = fakeShellApi({ capabilities: capabilitiesFixture({ app: { mode: "recovery" } }) });
    const store = createShellStore(api, new AppStore());
    await store.refreshCapabilities();
    expect(store.getSnapshot().recovery).toBe(true);
  });

  it.each(["normal", "recovery"] as const)(
    "a failed start waits for a successful manual %s retry before bootstrap",
    async (mode) => {
      const api = deferredShellApi();
      const store = createShellStore(api, new AppStore());
      store.start();
      expect(methodsOf(api.pending)).toEqual(["capabilities"]);
      api.pending[0]?.reject(new ShellApiError(503, "unavailable"));
      await flush();
      expect(methodsOf(api.pending)).toEqual(["capabilities"]);
      expect(store.getSnapshot().capabilitiesStatus).toBe("error");
      expect(store.getSnapshot().settingsStatus).toBe("unavailable");
      expect(store.getSnapshot().workspacesStatus).toBe("unavailable");

      const retry = store.refreshCapabilities();
      api.pending[1]?.resolve(capabilitiesFixture({ app: { mode } }));
      await retry;
      expect(store.getSnapshot().recovery).toBe(mode === "recovery");
      expect(methodsOf(api.pending)).toEqual(
        mode === "normal"
          ? ["capabilities", "capabilities", "settings", "workspaces"]
          : ["capabilities", "capabilities"],
      );
      if (mode === "normal") {
        api.pending[2]?.resolve(settingsFixture());
        api.pending[3]?.resolve(listingFixture());
        await flush();
        expect(store.getSnapshot().settingsStatus).toBe("ready");
        expect(store.getSnapshot().workspacesStatus).toBe("ready");
      }
      const before = api.pending.length;
      const refresh = store.refreshCapabilities();
      api.pending[before]?.resolve(capabilitiesFixture({ app: { mode } }));
      await refresh;
      expect(methodsOf(api.pending).slice(before)).toEqual(["capabilities"]);
      store.stop();
    },
  );

  it.each(["success", "failure"])(
    "stopping invalidates a pending capabilities %s and its startup",
    async (outcome) => {
      const api = deferredShellApi();
      const appStore = new AppStore();
      const store = createShellStore(api, appStore);
      store.start();
      store.stop();
      const stopped = store.getSnapshot();
      if (outcome === "success") api.pending[0]?.resolve(capabilitiesFixture());
      else api.pending[0]?.reject(new ShellApiError(503, "corrupt"));
      await flush();
      expect(store.getSnapshot()).toBe(stopped);
      appStore.setStatus("open");
      expect(methodsOf(api.pending)).toEqual(["capabilities"]);
    },
  );

  it("stopping invalidates pending settings and workspaces while explicit refreshes still work", async () => {
    const api = deferredShellApi();
    const store = createShellStore(api, new AppStore());
    store.start();
    api.pending[0]?.resolve(capabilitiesFixture());
    await flush();
    expect(methodsOf(api.pending)).toEqual(["capabilities", "settings", "workspaces"]);
    store.stop();
    const stopped = store.getSnapshot();
    api.pending[1]?.resolve(settingsFixture({}, { revision: 12 }));
    api.pending[2]?.reject(new ShellApiError(503, "corrupt"));
    await flush();
    expect(store.getSnapshot()).toBe(stopped);

    const refresh = store.afterMutation(["capabilities", "settings", "workspaces"]);
    api.pending[3]?.resolve(capabilitiesFixture({ launch: { id: "manual" } }));
    api.pending[4]?.resolve(settingsFixture({}, { revision: 15 }));
    api.pending[5]?.resolve(listingFixture([workspaceFixture()]));
    await refresh;
    expect(store.getSnapshot().capabilities?.launch.id).toBe("manual");
    expect(store.getSnapshot().settings?.revision).toBe(15);
    expect(store.getSnapshot().workspaces?.workspaces).toHaveLength(1);
    expect(api.pending).toHaveLength(6);
  });

  it.each(["success", "failure"])(
    "a restarted shell owns startup while the stopped capabilities %s settles",
    async (outcome) => {
      const api = deferredShellApi();
      const store = createShellStore(api, new AppStore());
      store.start();
      store.stop();
      store.start();
      const restarted = store.getSnapshot();
      if (outcome === "success")
        api.pending[0]?.resolve(capabilitiesFixture({ app: { mode: "recovery" } }));
      else api.pending[0]?.reject(new ShellApiError(503, "corrupt"));
      await flush();
      expect(store.getSnapshot()).toBe(restarted);
      expect(methodsOf(api.pending)).toEqual(["capabilities", "capabilities"]);

      api.pending[1]?.resolve(capabilitiesFixture({ launch: { id: "restarted" } }));
      await flush();
      expect(store.getSnapshot().capabilities?.launch.id).toBe("restarted");
      expect(store.getSnapshot().recovery).toBe(false);
      expect(methodsOf(api.pending)).toEqual([
        "capabilities",
        "capabilities",
        "settings",
        "workspaces",
      ]);
      api.pending[2]?.resolve(settingsFixture());
      api.pending[3]?.resolve(listingFixture());
      await flush();
      store.stop();
    },
  );

  it("a recovery launch asks for nothing the recovery app does not serve", async () => {
    const api = fakeShellApi({ capabilities: capabilitiesFixture({ app: { mode: "recovery" } }) });
    const store = createShellStore(api, new AppStore());
    store.start();
    await flush();
    expect(methodsOf(api.calls)).toEqual(["capabilities"]);
    expect(store.getSnapshot().settingsStatus).toBe("unavailable");
    expect(store.getSnapshot().workspacesStatus).toBe("unavailable");
  });

  it("a select the fake accepted is what the next capabilities read reports", async () => {
    const api = fakeShellApi();
    const store = createShellStore(api, new AppStore());
    const chosen = await api.selectWorkspace("command", FAKE_WORKSPACE_ID);
    expect(chosen.restart_required).toBe(true);
    await store.afterMutation(["capabilities", "workspaces"]);
    const shell = store.getSnapshot();
    expect(shell.capabilities?.workspace.selected_id).toBe(FAKE_WORKSPACE_ID);
    expect(shell.capabilities?.workspace.selected_by).toBe("registration");
    expect(shell.capabilities?.workspace.restart_required).toBe(true);
    expect(shell.workspaces?.selected_id).toBe(FAKE_WORKSPACE_ID);

    // Back to the shared scratch: nothing is selected, so nothing needs a restart.
    const cleared = await api.selectWorkspace("command", null);
    expect(cleared.restart_required).toBe(false);
    await store.refreshCapabilities();
    expect(store.getSnapshot().capabilities?.workspace.selected_by).toBe("builtin");
  });

  it("patch commits success and leaves a conflict for the panel", async () => {
    const api = fakeShellApi();
    const store = createShellStore(api, new AppStore());
    await store.refreshSettings();
    const result = await store.patch({ "appearance.density": "compact" });

    const patchCall = api.calls[1];
    expect(patchCall?.method).toBe("patchSettings");
    expect(patchCall?.args[1]).toBe(3);
    expect(String(patchCall?.args[0])).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/,
    );
    expect(result.revision).toBe(4);
    expect(store.getSnapshot().settings?.effective["appearance.density"]).toBe("compact");
    // A settings-only change does not re-read capabilities.
    expect(methodsOf(api.calls)).toEqual(["settings", "patchSettings"]);

    const other: SettingsView = settingsFixture({ "identity.display_name": "T" }, { revision: 9 });
    api.fail(
      "patchSettings",
      new ShellApiError(409, "stale_revision", { current: other, currentRevision: 9 }),
    );
    const before = store.getSnapshot();
    const conflict = await refusal(store.patch({ "appearance.density": "comfortable" }));
    expect(conflict.code).toBe("stale_revision");
    expect(conflict.current?.revision).toBe(9);
    expect(store.getSnapshot()).toBe(before);

    // The next attempt carries a fresh id, never the one the conflict refused.
    api.data.settings = other;
    await store.patch({ "appearance.density": "comfortable" });
    expect(api.calls[2]?.args[0]).not.toBe(api.calls[3]?.args[0]);
  });

  it("a conflict only another window's layout.panes save caused is sent once more on its revision", async () => {
    const current = documentAt(7, { "layout.panes": "two panes" });
    const api = fakeShellApi({ settings: documentAt(3, { "layout.panes": "one pane" }) });
    const store = createShellStore(api, new AppStore());
    await store.refreshSettings();
    api.data.settings = current;
    api.fail("patchSettings", staleRevision(current));
    const result = await store.patch({ "chat.default_mode": "plan" });

    // The capabilities re-read follows the attempt that succeeded, not the refused one.
    expect(methodsOf(api.calls)).toEqual([
      "settings",
      "patchSettings",
      "patchSettings",
      "capabilities",
    ]);
    const [refused, retry] = [api.calls[1], api.calls[2]];
    expect(refused?.args[1]).toBe(3);
    expect(retry?.args[1]).toBe(7);
    expect(retry?.args[0]).not.toBe(refused?.args[0]);
    expect(retry?.args[2]).toEqual({ "chat.default_mode": "plan" });
    expect(store.getSnapshot().settings).toBe(result);
    expect(result.revision).toBe(8);
    expect(result.effective["layout.panes"]).toBe("two panes");
    expect(result.effective["chat.default_mode"]).toBe("plan");
  });

  it.each([
    {
      when: "another key differs as well",
      changes: { "appearance.density": "compact" },
      current: documentAt(7, { "layout.panes": "two panes", "identity.display_name": "T" }),
    },
    {
      when: "the patch writes layout.panes itself",
      changes: { "layout.panes": "my panes" },
      current: documentAt(7, { "layout.panes": "two panes" }),
    },
    {
      when: "the server's document has a key the base lacks",
      changes: { "appearance.density": "compact" },
      current: documentAt(7, { "layout.panes": "two panes", "future.setting": true }),
    },
    {
      when: "the conflict carries no document",
      changes: { "appearance.density": "compact" },
      current: null,
    },
  ])("a conflict stays the caller's when $when", async ({ changes, current }) => {
    const api = fakeShellApi({ settings: documentAt(3, { "layout.panes": "one pane" }) });
    const store = createShellStore(api, new AppStore());
    await store.refreshSettings();
    const before = store.getSnapshot();
    const conflict = staleRevision(current);
    api.fail("patchSettings", conflict);
    expect(await refusal(store.patch(changes))).toBe(conflict);
    expect(methodsOf(api.calls)).toEqual(["settings", "patchSettings"]);
    expect(store.getSnapshot()).toBe(before);
  });

  it("a retry that meets a second conflict rejects with it and is not sent again", async () => {
    const api = deferredShellApi();
    const store = createShellStore(api, new AppStore());
    store.adoptSettings(documentAt(3, { "layout.panes": "one pane" }));
    const before = store.getSnapshot();
    const saving = refusal(store.patch({ "chat.default_mode": "plan" }));
    api.pending[0]?.reject(staleRevision(documentAt(7, { "layout.panes": "two panes" })));
    await flush();
    expect(methodsOf(api.pending)).toEqual(["patchSettings", "patchSettings"]);
    expect(api.pending[1]?.args[1]).toBe(7);

    const second = staleRevision(documentAt(8, { "layout.panes": "three panes" }));
    api.pending[1]?.reject(second);
    expect(await saving).toBe(second);
    await flush();
    expect(methodsOf(api.pending)).toEqual(["patchSettings", "patchSettings"]);
    expect(store.getSnapshot()).toBe(before);
  });

  it("a chat default also refreshes what the next session would start with", async () => {
    const api = fakeShellApi();
    const store = createShellStore(api, new AppStore());
    await store.refreshSettings();
    await store.patch({ "chat.default_mode": "plan" });
    expect(methodsOf(api.calls)).toEqual(["settings", "patchSettings", "capabilities"]);
  });

  it("a read-only refusal changes nothing", async () => {
    const api = fakeShellApi({
      settings: settingsFixture({}, { read_only: true, reason: "newer_version" }),
    });
    const store = createShellStore(api, new AppStore());
    await store.refreshSettings();
    const before = store.getSnapshot();
    api.fail("patchSettings", new ShellApiError(409, "read_only"));
    expect((await refusal(store.patch({ "appearance.density": "compact" }))).code).toBe(
      "read_only",
    );
    expect(store.getSnapshot()).toBe(before);
    expect(store.getSnapshot().settingsStatus).toBe("ready");
    expect(store.getSnapshot().errors.settings).toBe("");
  });

  it("adopting a document is one commit", async () => {
    const api = fakeShellApi();
    const store = createShellStore(api, new AppStore());
    await store.refreshSettings();
    const notifications = counted(store);
    const adopted = settingsFixture({ "appearance.density": "compact" }, { revision: 12 });
    store.adoptSettings(adopted);
    expect(notifications.count()).toBe(1);
    expect(store.getSnapshot().settings?.revision).toBe(12);
  });

  it("an adopted document outranks a read already in flight", async () => {
    const api = deferredShellApi();
    const store = createShellStore(api, new AppStore());
    const reading = store.refreshSettings();
    store.adoptSettings(settingsFixture({}, { revision: 12 }));
    api.pending[0]?.resolve(settingsFixture({}, { revision: 3 }));
    await reading;
    expect(store.getSnapshot().settings?.revision).toBe(12);
  });

  it("a check is one at a time", async () => {
    const api = deferredShellApi();
    const store = createShellStore(api, new AppStore());
    const running = store.runCheck("git");
    expect(store.getSnapshot().checkRunning).toBe("git");

    api.pending[0]?.resolve(CHECK);
    await Promise.resolve();
    await Promise.resolve();
    expect(store.getSnapshot().checkRunning).toBeNull();
    expect(methodsOf(api.pending)).toEqual(["runCheck", "capabilities"]);
    api.pending[1]?.resolve(capabilitiesFixture());
    expect((await running).check).toBe("git");
  });

  it("a refused check releases the button and reads no new readiness", async () => {
    const api = fakeShellApi();
    const store = createShellStore(api, new AppStore());
    api.fail("runCheck", new ShellApiError(409, "check_in_progress"));
    expect((await refusal(store.runCheck("provider_cli"))).code).toBe("check_in_progress");
    expect(store.getSnapshot().checkRunning).toBeNull();
    expect(methodsOf(api.calls)).toEqual(["runCheck"]);
  });

  it("a same-tick overlapping check is refused without changing the first check's ownership", async () => {
    const api = deferredShellApi();
    const store = createShellStore(api, new AppStore());
    const running = store.runCheck("git");
    const busy = store.getSnapshot();
    const notifications = counted(store);
    const overlapping = refusal(store.runCheck("provider_cli"));
    expect(methodsOf(api.pending)).toEqual(["runCheck"]);
    expect(store.getSnapshot()).toBe(busy);
    expect(notifications.count()).toBe(0);
    const error = await overlapping;
    expect(error.status).toBe(409);
    expect(error.code).toBe("check_in_progress");
    expect(store.getSnapshot().checkRunning).toBe("git");

    api.pending[0]?.resolve(CHECK);
    await flush();
    expect(store.getSnapshot().checkRunning).toBeNull();
    expect(methodsOf(api.pending)).toEqual(["runCheck", "capabilities"]);
    api.pending[1]?.resolve(capabilitiesFixture());
    expect(await running).toBe(CHECK);
  });

  it.each([false, true])(
    "a check settling after stop clears busy without a late capabilities read (restart: %s)",
    async (restart) => {
      const api = deferredShellApi();
      const store = createShellStore(api, new AppStore());
      const recovery = capabilitiesFixture({ app: { mode: "recovery" } });
      store.start();
      api.pending[0]?.resolve(recovery);
      await flush();
      const running = store.runCheck("git");
      store.stop();
      if (restart) {
        store.start();
        api.pending[2]?.resolve(recovery);
        await flush();
      }
      const before = methodsOf(api.pending);
      api.pending[1]?.resolve(CHECK);
      await flush();
      expect(store.getSnapshot().checkRunning).toBeNull();
      expect(methodsOf(api.pending)).toEqual(before);
      expect(await running).toBe(CHECK);
      store.stop();
    },
  );

  it("afterMutation reads exactly the kinds it was given", async () => {
    const api = fakeShellApi();
    const store = createShellStore(api, new AppStore());
    await store.afterMutation(["capabilities", "workspaces"]);
    expect(methodsOf(api.calls).sort()).toEqual(["capabilities", "workspaces"]);
    expect(store.getSnapshot().settingsStatus).toBe("unavailable");
  });

  it("vault surfaces hide only in the shared scratch", () => {
    expect(vaultSurfaces(bareSnapshot(null))).toBe(true);
    expect(
      vaultSurfaces(bareSnapshot(capabilitiesFixture({ workspace: { shared_scratch: true } }))),
    ).toBe(false);
    for (const layout of ["legacy_para", "unknown"] as const) {
      const capabilities = capabilitiesFixture({
        workspace: {
          shared_scratch: false,
          active: {
            id: "w",
            kind: "vault",
            name: "My notes",
            policy: "ordinary",
            layout,
            source: "created",
            aliased: false,
            root_available: true,
          },
        },
      });
      expect(vaultSurfaces(bareSnapshot(capabilities))).toBe(true);
    }
  });

  it("vault surfaces hide for an active code folder and stay for a vault or no active", () => {
    const active = {
      id: "w",
      kind: "code",
      name: "app",
      policy: "ordinary",
      layout: "unknown",
      source: "opened",
      aliased: false,
      root_available: true,
    } as const;
    const withActive = (next: Capabilities["workspace"]["active"]) =>
      bareSnapshot(capabilitiesFixture({ workspace: { shared_scratch: false, active: next } }));
    expect(vaultSurfaces(withActive(active))).toBe(false);
    expect(vaultSurfaces(withActive({ ...active, kind: "vault" }))).toBe(true);
    expect(vaultSurfaces(withActive(null))).toBe(true);
  });

  it("canStart reports unknown as null", () => {
    expect(canStart(bareSnapshot(null))).toBeNull();
    expect(canStart(bareSnapshot(capabilitiesFixture()))).toBe(true);
    expect(canStart(bareSnapshot(capabilitiesFixture({ provider: { can_start: false } })))).toBe(
      false,
    );
  });
});
