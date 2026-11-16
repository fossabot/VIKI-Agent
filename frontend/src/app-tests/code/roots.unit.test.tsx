/**
 * Session roots as App wires them: starting a session in a folder from the Code view (the
 * Workbench is stubbed to the `roots` prop it receives), and the folder labels on the rail row,
 * the tab and the header. The roots module, the API client and the Workbench have their own suites.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki, sessionRow, snapshotData } from "../../accept/m3b/fakes";
import type { CodeApi } from "../../clients/code/codeApi";
import {
  type RootsApi,
  RootsApiError,
  type SessionRootJson,
  type WorktreeJson,
} from "../../clients/code/rootsApi";
import type { SessionMetadataApi } from "../../clients/conversations/sessionMetadataApi";
import type { WorkbenchProps } from "../../components/Workbench";
import type { WorkbenchRoots } from "../../components/Workbench/Workbench";
import { refreshRoots, resetRoots } from "../../state/sessionRoots";
import { fakeDurable } from "../../testDurable";
import {
  capabilitiesFixture,
  fakeShellApi,
  listingFixture,
  settingsFixture,
  workspaceFixture,
} from "../../testShell";
import { openWorkbench } from "../../testWorkbench";

// The newest `roots` prop the stub Workbench was rendered with. It stays callable after the view
// leaves the Code view, because App memoizes the one object.
const shown: { roots: WorkbenchRoots | undefined } = { roots: undefined };

vi.mock("../../components/Workbench", () => ({
  Workbench: (props: WorkbenchProps) => {
    shown.roots = props.roots;
    return (
      <section>
        <h1>Workbench stub</h1>
        <p data-testid="roots">{props.roots ? "roots prop" : "no roots prop"}</p>
      </section>
    );
  },
}));

const CODE_ID = "55555555-5555-4555-8555-555555555555";
const OTHER_ID = "66666666-6666-4666-8666-666666666666";
const WORKTREE_ID = "77777777-7777-4777-8777-777777777777";
const OTHER_SESSION = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const TREE_SESSION = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const AT = "2026-10-03T00:00:00Z";

beforeEach(() => {
  window.sessionStorage.clear();
  shown.roots = undefined;
  resetRoots();
});

afterEach(() => {
  cleanup();
  delete (window.navigator as { platform?: string }).platform;
  window.sessionStorage.clear();
  vi.restoreAllMocks();
  resetRoots();
});

function codeApi(): CodeApi {
  const pending = () => new Promise<never>(() => {});
  return {
    tree: pending,
    file: pending,
    search: pending,
    changes: pending,
    diff: pending,
    save: pending,
  };
}

function organization(): SessionMetadataApi {
  return {
    list: async () => [],
    patch: async () => {
      throw new Error("unused");
    },
    remove: async () => true,
  };
}

const worktree: WorktreeJson = {
  worktree_id: WORKTREE_ID,
  workspace_id: CODE_ID,
  branch: "viki/a",
  base_oid: "a".repeat(40),
  path: "area/a",
  state: "ready",
  created_at: AT,
  updated_at: AT,
};

/** A roots client that lists what `record` was given, as the server does. */
function rootsApi(
  seed: SessionRootJson[] = [],
  record?: RootsApi["record"],
): RootsApi & { rows: SessionRootJson[] } {
  const rows = [...seed];
  return {
    rows,
    roots: async () => ({ roots: [...rows], truncated: false }),
    worktrees: async () => ({ worktrees: [worktree], truncated: false }),
    record:
      record ??
      (vi.fn(async (root: SessionRootJson) => {
        rows.push(root);
        return root;
      }) as RootsApi["record"]),
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
}

function shellApi() {
  const stored = settingsFixture({ "onboarding.completed": true });
  const code = workspaceFixture({ workspace_id: CODE_ID, kind: "code", name: "app code" });
  const other = workspaceFixture({
    workspace_id: OTHER_ID,
    kind: "code",
    name: "other code",
    root_physical_id: "12:34",
  });
  return fakeShellApi({
    capabilities: capabilitiesFixture({
      workspace: {
        shared_scratch: false,
        chat_only: false,
        active: {
          id: CODE_ID,
          kind: "code",
          name: "app code",
          policy: code.policy,
          layout: "unknown",
          source: "created",
          aliased: false,
          root_available: true,
        },
      },
    }),
    settings: stored,
    workspaces: { ...listingFixture([code, other]), active_id: CODE_ID, chat_only: false },
  });
}

async function settled(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 8; i++) await Promise.resolve();
  });
}

function mount(roots: RootsApi | undefined, options: { listed?: boolean } = {}) {
  const fake = fakeViki();
  const rest = fakeRest(
    options.listed
      ? {
          sessions: [
            sessionRow({ session_id: OTHER_SESSION, title: "Other session" }),
            sessionRow({ session_id: TREE_SESSION, title: "Tree session" }),
          ],
        }
      : {},
  );
  render(
    <App
      viki={fake.viki}
      rest={rest}
      durable={fakeDurable()}
      shell={shellApi()}
      organization={organization()}
      codeApi={codeApi()}
      {...(roots ? { rootsApi: roots } : {})}
    />,
  );
  act(() => fake.socket.open());
  return fake;
}

/** Calls the Workbench's `onStartSession` as its button would. */
async function startIn(workspaceId: string, worktreeId: string | null = null) {
  let answer: string | null | undefined;
  await act(async () => {
    answer = await shown.roots?.onStartSession({
      workspace_id: workspaceId,
      worktree_id: worktreeId,
    });
  });
  return answer;
}

const inCodeView = () => screen.queryByText("Workbench stub") !== null;

test("starting in a folder records the root before opening the session", async () => {
  let settle: (root: SessionRootJson) => void = () => {};
  const api = rootsApi(
    [],
    vi.fn(
      () =>
        new Promise<SessionRootJson>((resolve) => {
          settle = resolve;
        }),
    ),
  );
  Object.defineProperty(window.navigator, "platform", { value: "Win32", configurable: true });
  const fake = mount(api);
  const newSession = vi.spyOn(fake.viki.actions, "newSession");
  await settled();
  await openWorkbench();

  let answer: string | null | undefined;
  await act(async () => {
    void shown.roots?.onStartSession({ workspace_id: OTHER_ID, worktree_id: null }).then((code) => {
      answer = code;
    });
    await Promise.resolve();
  });
  const record = vi.mocked(api.record);
  expect(record).toHaveBeenCalledTimes(1);
  const sent = record.mock.calls[0]?.[0];
  expect(Object.keys(sent ?? {}).sort()).toEqual(["session_id", "workspace_id", "worktree_id"]);
  expect(sent).toMatchObject({ workspace_id: OTHER_ID, worktree_id: null });
  // Nothing opens until the server has recorded the root.
  expect(newSession).not.toHaveBeenCalled();
  expect(inCodeView()).toBe(true);

  const id = sent?.session_id ?? "";
  await act(async () => {
    settle({ session_id: id, workspace_id: OTHER_ID, worktree_id: null });
    await Promise.resolve();
  });
  await settled();
  expect(answer).toBeNull();
  expect(newSession).toHaveBeenCalledTimes(1);
  expect(newSession).toHaveBeenCalledWith(id);
  expect(inCodeView()).toBe(false);
  expect(screen.getByLabelText("Message")).toBeTruthy();
  const tab = document.querySelector('.session-tab-select[aria-selected="true"]');
  expect(tab?.id).toBe(`session-tab-${id}`);
  expect(document.querySelector(".conversation-title")?.parentElement?.textContent).toContain(
    "other code",
  );
});

test("a refusal returns its code, keeps the Code view and opens no session", async () => {
  const refuse = vi.fn(async () => {
    throw new RootsApiError("trust_required");
  });
  const api = rootsApi([], refuse);
  const fake = mount(api);
  const newSession = vi.spyOn(fake.viki.actions, "newSession");
  await settled();
  await openWorkbench();

  expect(await startIn(OTHER_ID)).toBe("trust_required");
  expect(inCodeView()).toBe(true);
  expect(newSession).not.toHaveBeenCalled();
  expect(fake.socket.sent.filter((message) => message.type === "session.open")).toEqual([]);

  refuse.mockRejectedValueOnce(new Error("network") as never);
  expect(await startIn(OTHER_ID)).toBe("unavailable");
  expect(inCodeView()).toBe(true);
  expect(newSession).not.toHaveBeenCalled();
});

test("at three live sessions a start answers too_many_sessions and records nothing", async () => {
  const api = rootsApi();
  mount(api);
  await settled();
  await openWorkbench();
  for (let i = 0; i < 3; i++) {
    expect(await startIn(OTHER_ID)).toBeNull();
    await openWorkbench();
  }
  expect(api.record).toHaveBeenCalledTimes(3);

  expect(await startIn(OTHER_ID)).toBe("too_many_sessions");
  expect(api.record).toHaveBeenCalledTimes(3);
  expect(inCodeView()).toBe(true);
});

test("a Code start uses the advertised eight-session limit before recording a root", async () => {
  const api = rootsApi();
  const fake = mount(api);
  const newSession = vi.spyOn(fake.viki.actions, "newSession");
  const synchronize = fake.socket.synchronize.bind(fake.socket);
  vi.spyOn(fake.socket, "synchronize").mockImplementation(
    (id, generation, seq, data = snapshotData()) => {
      data.state.max_live_sessions = 8;
      synchronize(id, generation, seq, data);
    },
  );
  await settled();
  await openWorkbench();
  for (let i = 1; i <= 8; i++) {
    expect(await startIn(OTHER_ID)).toBeNull();
    expect(newSession).toHaveBeenCalledTimes(i);
    await openWorkbench();
  }
  expect(api.record).toHaveBeenCalledTimes(8);
  expect(await startIn(OTHER_ID)).toBe("too_many_sessions");
  expect(api.record).toHaveBeenCalledTimes(8);
  expect(newSession).toHaveBeenCalledTimes(8);
  expect(inCodeView()).toBe(true);
});

test("a folder or worktree label reaches the rail row, the tab and the header", async () => {
  const api = rootsApi([
    { session_id: OTHER_SESSION, workspace_id: OTHER_ID, worktree_id: null },
    { session_id: TREE_SESSION, workspace_id: CODE_ID, worktree_id: WORKTREE_ID },
  ]);
  mount(api, { listed: true });
  await settled();

  const row = (id: string) => document.querySelector(`.sess[data-session-id="${id}"]`);
  expect(row(OTHER_SESSION)?.querySelector(".root-label")?.textContent).toBe("other code");
  expect(row(TREE_SESSION)?.querySelector(".root-label")?.textContent).toBe("app code · viki/a");

  await act(async () => {
    fireEvent.click(row(OTHER_SESSION) as Element);
  });
  await act(async () => {
    fireEvent.click(row(TREE_SESSION) as Element);
  });
  const tabs = [...document.querySelectorAll(".session-tab")].map(
    (tab) => tab.querySelector(".root-label")?.textContent,
  );
  expect(tabs).toEqual(["other code", "app code · viki/a"]);
  const header = document.querySelector(".conversation-title")?.parentElement;
  expect(header?.querySelector(".root-label")?.textContent).toBe("app code · viki/a");
});

test("a failing roots read leaves the app as it was", async () => {
  const api = rootsApi();
  api.roots = async () => {
    throw new RootsApiError("unavailable");
  };
  mount(api, { listed: true });
  await settled();
  expect(document.querySelector(".root-label")).toBeNull();
  expect(document.querySelector('[role="alert"]')).toBeNull();
});

test("without a roots client App renders as before: no labels and no roots prop", async () => {
  // A root held in the store would label both rows if App passed the labels on.
  const api = rootsApi([{ session_id: OTHER_SESSION, workspace_id: OTHER_ID, worktree_id: null }]);
  await refreshRoots(api);
  mount(undefined, { listed: true });
  await settled();
  expect(document.querySelectorAll(".sess").length).toBe(2);
  expect(document.querySelector(".root-label")).toBeNull();
  await openWorkbench();
  expect(screen.getByTestId("roots").textContent).toBe("no roots prop");
});
