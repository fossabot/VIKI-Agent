import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki, sessionRow } from "../../accept/m3b/fakes";
import type { CodeApi } from "../../clients/code/codeApi";
import { type RootsApi, RootsApiError, type SessionRootJson } from "../../clients/code/rootsApi";
import type { WorkbenchProps } from "../../components/Workbench";
import type { WorkbenchRoots } from "../../components/Workbench/Workbench";
import type { SessionRow } from "../../protocol";
import { resetRoots } from "../../state/sessionRoots";
import { setUnsavedWork } from "../../state/unsavedWork";
import { fakeDurable } from "../../testDurable";
import {
  capabilitiesFixture,
  fakeShellApi,
  listingFixture,
  settingsFixture,
  workspaceFixture,
} from "../../testShell";
import { openWorkbench } from "../../testWorkbench";

const CODE_ID = "55555555-5555-4555-8555-555555555555";
const OTHER_ID = "66666666-6666-4666-8666-666666666666";
const THIRD_ID = "77777777-7777-4777-8777-777777777777";
const WORKTREE_ID = "88888888-8888-4888-8888-888888888888";
const SOURCE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const FORK_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const PERMISSIONS =
  "Selected folders provide conversation context. Claude may ask before accessing them; selection does not grant tool permissions.";
const shown: { roots: WorkbenchRoots | undefined } = { roots: undefined };
const connections: ReturnType<typeof fakeViki>[] = [];

vi.mock("../../components/Workbench", () => ({
  Workbench: (props: WorkbenchProps) => {
    shown.roots = props.roots;
    return (
      <section>
        <h1>Workbench stub</h1>
        {props.renderBack?.(props.onBack)}
      </section>
    );
  },
}));

beforeEach(() => {
  window.sessionStorage.clear();
  shown.roots = undefined;
  resetRoots();
});
afterEach(() => {
  cleanup();
  for (const { viki } of connections.splice(0)) viki.client.close();
  window.sessionStorage.clear();
  vi.restoreAllMocks();
  resetRoots();
});

function rootsApi(seed: SessionRootJson[] = [], record?: RootsApi["record"]) {
  const rows = [...seed];
  const api: RootsApi & { rows: SessionRootJson[] } = {
    rows,
    roots: vi.fn(async () => ({ roots: [...rows], truncated: false })),
    worktrees: vi.fn(async () => ({ worktrees: [], truncated: false })),
    record:
      record ??
      vi.fn(async (root: SessionRootJson) => {
        rows.push(root);
        return root;
      }),
    create: vi.fn(),
    remove: vi.fn(),
    archives: vi.fn(async () => {
      throw new RootsApiError("unavailable");
    }),
    cleanup: vi.fn(async () => {
      throw new RootsApiError("unavailable");
    }),
    restore: vi.fn(async () => {
      throw new RootsApiError("unavailable");
    }),
  };
  return api;
}

function mount(api: RootsApi, sessions: SessionRow[] = []) {
  const fake = fakeViki();
  connections.push(fake);
  const folders = [
    workspaceFixture({ workspace_id: CODE_ID, kind: "code", name: "app code" }),
    workspaceFixture({ workspace_id: OTHER_ID, kind: "code", name: "other code" }),
    workspaceFixture({ workspace_id: THIRD_ID, kind: "code", name: "third code" }),
  ];
  const pending = () => new Promise<never>(() => {});
  const view = render(
    <App
      viki={fake.viki}
      rest={fakeRest({ sessions })}
      durable={fakeDurable()}
      shell={fakeShellApi({
        settings: settingsFixture({ "onboarding.completed": true }),
        capabilities: capabilitiesFixture({
          workspace: {
            chat_only: false,
            shared_scratch: false,
            active: {
              id: CODE_ID,
              kind: "code",
              name: "app code",
              policy: "ordinary",
              layout: "unknown",
              source: "created",
              aliased: false,
              root_available: true,
            },
          },
        }),
        workspaces: { ...listingFixture(folders), active_id: CODE_ID, chat_only: false },
      })}
      organization={{ list: async () => [], patch: vi.fn(), remove: async () => true }}
      codeApi={
        {
          tree: pending,
          file: pending,
          diff: pending,
          changes: pending,
          search: pending,
          save: pending,
        } as CodeApi
      }
      rootsApi={api}
    />,
  );
  act(() => fake.socket.open());
  return { ...fake, ...view };
}

const settled = () =>
  act(async () => {
    for (let index = 0; index < 8; index++) await Promise.resolve();
  });
const inCode = () => screen.queryByText("Workbench stub") !== null;

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

test("test_multi_folder_record_precedes_single_session_open_and_restore", async () => {
  const waiting = deferred<SessionRootJson>();
  const record = vi.fn<RootsApi["record"]>(() => waiting.promise);
  const api = rootsApi([], record);
  const fake = mount(api);
  const open = vi.spyOn(fake.viki.actions, "newSession");
  await settled();
  await openWorkbench();
  const extra = [OTHER_ID, THIRD_ID];
  let result: Promise<string | null> | undefined;
  act(() => {
    result = shown.roots?.onStartSession({
      workspace_id: CODE_ID,
      worktree_id: null,
      additional_workspace_ids: extra,
    });
  });
  const sent = record.mock.calls[0]?.[0];
  expect(sent).toEqual({
    session_id: expect.any(String),
    workspace_id: CODE_ID,
    worktree_id: null,
    additional_workspace_ids: [OTHER_ID, THIRD_ID],
  });
  expect(sent?.additional_workspace_ids).not.toBe(extra);
  extra.splice(0, 2);
  expect(sent?.additional_workspace_ids).toEqual([OTHER_ID, THIRD_ID]);
  expect(record).toHaveBeenCalledTimes(1);
  expect(open).not.toHaveBeenCalled();
  expect(fake.socket.sent.filter((message) => message.type === "session.open")).toEqual([]);
  expect(inCode()).toBe(true);

  const id = sent?.session_id ?? "";
  const snapshot = {
    session_id: id,
    workspace_id: CODE_ID,
    worktree_id: null,
    additional_workspace_ids: [OTHER_ID, THIRD_ID],
  };
  api.rows.push(snapshot);
  await act(async () => {
    waiting.resolve(snapshot);
    expect(await result).toBeNull();
  });
  expect(open).toHaveBeenCalledExactlyOnceWith(id);
  expect(fake.socket.sent.filter((message) => message.type === "session.open")).toHaveLength(1);
  expect(inCode()).toBe(false);
  expect(
    document.querySelector(".conversation-title")?.parentElement?.querySelector(".root-label")
      ?.textContent,
  ).toBe("app code · other code · third code");
  expect(screen.getByText(PERMISSIONS)).toBeTruthy();

  cleanup();
  fake.viki.client.close();
  resetRoots();
  const restored = mount(api, [sessionRow({ session_id: id, title: "Restored conversation" })]);
  await settled();
  expect(document.querySelector(`.sess[data-session-id="${id}"] .root-label`)?.textContent).toBe(
    "app code · other code · third code",
  );
  await act(async () => {
    fireEvent.click(document.querySelector(`.sess[data-session-id="${id}"]`) as HTMLElement);
  });
  expect(
    document.querySelector(".conversation-title")?.parentElement?.querySelector(".root-label")
      ?.textContent,
  ).toBe("app code · other code · third code");
  expect(screen.getByText(PERMISSIONS)).toBeTruthy();
  expect(restored.socket.sent.filter((message) => message.type === "session.open")).toHaveLength(1);
  expect(record).toHaveBeenCalledTimes(1);
});

test.each(["single", "empty extras", "worktree"])(
  "%s starts preserve the exact legacy root body",
  async (kind) => {
    const api = rootsApi();
    mount(api);
    await settled();
    await openWorkbench();
    await act(async () => {
      expect(
        await shown.roots?.onStartSession({
          workspace_id: CODE_ID,
          worktree_id: kind === "worktree" ? WORKTREE_ID : null,
          ...(kind === "empty extras" ? { additional_workspace_ids: [] } : {}),
        }),
      ).toBeNull();
    });
    expect(api.record).toHaveBeenCalledExactlyOnceWith({
      session_id: expect.any(String),
      workspace_id: CODE_ID,
      worktree_id: kind === "worktree" ? WORKTREE_ID : null,
    });
    expect(screen.queryByText(PERMISSIONS)).toBeNull();
  },
);

test("a multi-folder refusal keeps Code open and clears the starter guard for another attempt", async () => {
  const record = vi
    .fn<RootsApi["record"]>()
    .mockRejectedValueOnce(new RootsApiError("trust_required"))
    .mockRejectedValueOnce(new Error("offline"));
  const fake = mount(rootsApi([], record));
  const open = vi.spyOn(fake.viki.actions, "newSession");
  await settled();
  await openWorkbench();
  for (const expected of ["trust_required", "unavailable"]) {
    await act(async () => {
      expect(
        await shown.roots?.onStartSession({
          workspace_id: CODE_ID,
          worktree_id: null,
          additional_workspace_ids: [OTHER_ID],
        }),
      ).toBe(expected);
    });
  }
  expect(record).toHaveBeenCalledTimes(2);
  expect(inCode()).toBe(true);
  expect(open).not.toHaveBeenCalled();
  expect(fake.socket.sent.filter((message) => message.type === "session.open")).toEqual([]);
});

test("selector and worktree starts share one synchronous guard until the first start settles", async () => {
  const waiting = deferred<SessionRootJson>();
  const record = vi
    .fn<RootsApi["record"]>()
    .mockImplementationOnce(() => waiting.promise)
    .mockImplementation(async (root) => root);
  const fake = mount(rootsApi([], record));
  const open = vi.spyOn(fake.viki.actions, "newSession");
  await settled();
  await openWorkbench();
  let first: Promise<string | null> | undefined;
  let second: Promise<string | null> | undefined;
  act(() => {
    first = shown.roots?.onStartSession({
      workspace_id: CODE_ID,
      worktree_id: null,
      additional_workspace_ids: [OTHER_ID],
    });
    second = shown.roots?.onStartSession({ workspace_id: CODE_ID, worktree_id: WORKTREE_ID });
  });
  expect(await second).toBe("unavailable");
  expect(record).toHaveBeenCalledTimes(1);
  const sent = record.mock.calls[0]?.[0] as SessionRootJson;
  await act(async () => {
    waiting.resolve(sent);
    expect(await first).toBeNull();
  });
  expect(open).toHaveBeenCalledExactlyOnceWith(sent.session_id);
  await openWorkbench();
  await act(async () => {
    expect(
      await shown.roots?.onStartSession({ workspace_id: CODE_ID, worktree_id: WORKTREE_ID }),
    ).toBeNull();
  });
  expect(record.mock.calls[1]?.[0]).toEqual({
    session_id: expect.any(String),
    workspace_id: CODE_ID,
    worktree_id: WORKTREE_ID,
  });
  expect(open).toHaveBeenCalledTimes(2);
});

test.each([false, true])(
  "a pending multi-folder start does not open after leaving Code (return=%s)",
  async (returnToCode) => {
    const waiting = deferred<SessionRootJson>();
    const record = vi.fn<RootsApi["record"]>(() => waiting.promise);
    const fake = mount(rootsApi([], record));
    const open = vi.spyOn(fake.viki.actions, "newSession");
    await settled();
    await openWorkbench();
    let result: Promise<string | null> | undefined;
    act(() => {
      result = shown.roots?.onStartSession({
        workspace_id: CODE_ID,
        worktree_id: null,
        additional_workspace_ids: [OTHER_ID],
      });
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Back" }));
    });
    expect(inCode()).toBe(false);
    if (returnToCode) await openWorkbench();
    await act(async () => {
      waiting.resolve(record.mock.calls[0]?.[0] as SessionRootJson);
      expect(await result).toBeNull();
    });
    expect(inCode()).toBe(returnToCode);
    if (!returnToCode) expect(screen.getByLabelText("Message")).toBeTruthy();
    expect(open).not.toHaveBeenCalled();
    expect(fake.socket.sent.filter((message) => message.type === "session.open")).toEqual([]);
  },
);

test("an unmounted App does not open a pending multi-folder conversation", async () => {
  const waiting = deferred<SessionRootJson>();
  const record = vi.fn<RootsApi["record"]>(() => waiting.promise);
  const fake = mount(rootsApi([], record));
  const open = vi.spyOn(fake.viki.actions, "newSession");
  await settled();
  await openWorkbench();
  let result: Promise<string | null> | undefined;
  act(() => {
    result = shown.roots?.onStartSession({
      workspace_id: CODE_ID,
      worktree_id: null,
      additional_workspace_ids: [OTHER_ID],
    });
  });
  fake.unmount();
  waiting.resolve(record.mock.calls[0]?.[0] as SessionRootJson);
  expect(await result).toBeNull();
  expect(open).not.toHaveBeenCalled();
  expect(fake.socket.sent.filter((message) => message.type === "session.open")).toEqual([]);
});

test("restored source and fork rows use their durable folder snapshots on rows, tabs and headers", async () => {
  const api = rootsApi(
    [SOURCE_ID, FORK_ID].map((session_id) => ({
      session_id,
      workspace_id: CODE_ID,
      worktree_id: null,
      additional_workspace_ids: [OTHER_ID],
    })),
  );
  mount(api, [
    sessionRow({ session_id: SOURCE_ID, title: "Source conversation" }),
    sessionRow({ session_id: FORK_ID, title: "Fork conversation" }),
  ]);
  await settled();
  for (const id of [SOURCE_ID, FORK_ID]) {
    const row = document.querySelector(`.sess[data-session-id="${id}"]`) as HTMLElement;
    expect(row.querySelector(".root-label")?.textContent).toBe("app code · other code");
    await act(async () => fireEvent.click(row));
    expect(
      document.querySelector(".conversation-title")?.parentElement?.querySelector(".root-label")
        ?.textContent,
    ).toBe("app code · other code");
    expect(screen.getByText(PERMISSIONS)).toBeTruthy();
  }
  expect(
    [...document.querySelectorAll(".session-tab .root-label")].map((label) => label.textContent),
  ).toEqual(["app code · other code", "app code · other code"]);
  expect(api.record).not.toHaveBeenCalled();
});

test("three live conversations refuse a fourth multi-folder start before recording", async () => {
  const api = rootsApi();
  const fake = mount(api);
  const open = vi.spyOn(fake.viki.actions, "newSession");
  await settled();
  for (let index = 0; index < 3; index++) {
    await openWorkbench();
    await act(async () => {
      expect(
        await shown.roots?.onStartSession({
          workspace_id: CODE_ID,
          worktree_id: null,
          additional_workspace_ids: [OTHER_ID],
        }),
      ).toBeNull();
    });
  }
  await openWorkbench();
  await act(async () => {
    expect(
      await shown.roots?.onStartSession({
        workspace_id: CODE_ID,
        worktree_id: null,
        additional_workspace_ids: [OTHER_ID],
      }),
    ).toBe("too_many_sessions");
  });
  expect(api.record).toHaveBeenCalledTimes(3);
  expect(open).toHaveBeenCalledTimes(3);
  expect(inCode()).toBe(true);
});

test("declining the unsaved-work prompt leaves a prepared multi-folder root unused", async () => {
  const api = rootsApi();
  const fake = mount(api);
  const open = vi.spyOn(fake.viki.actions, "newSession");
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  await settled();
  await openWorkbench();
  act(() => setUnsavedWork("profile", true));
  try {
    await act(async () => {
      expect(
        await shown.roots?.onStartSession({
          workspace_id: CODE_ID,
          worktree_id: null,
          additional_workspace_ids: [OTHER_ID],
        }),
      ).toBeNull();
    });
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(api.record).toHaveBeenCalledTimes(1);
    expect(inCode()).toBe(true);
    expect(open).not.toHaveBeenCalled();
    expect(fake.socket.sent.filter((message) => message.type === "session.open")).toEqual([]);
  } finally {
    act(() => setUnsavedWork("profile", false));
  }
});
