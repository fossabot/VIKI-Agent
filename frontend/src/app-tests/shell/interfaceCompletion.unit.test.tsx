import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App, type AppProps } from "../../App";
import { FakeSocket, fakeRest, sessionRow } from "../../accept/m3b/fakes";
import type { CodeApi, CodeTree } from "../../clients/code/codeApi";
import { type RootsApi, RootsApiError } from "../../clients/code/rootsApi";
import type { ChatArchivesApi } from "../../clients/conversations/chatArchivesApi";
import type {
  SessionMetadata,
  SessionMetadataApi,
} from "../../clients/conversations/sessionMetadataApi";
import { SessionWindowsError } from "../../clients/conversations/sessionWindowsApi";
import type { AssetsApi } from "../../clients/media/assetsApi";
import type {
  PreparedProfilePreview,
  ProfileLaunchApi,
} from "../../clients/profiles/profileLaunchApi";
import { parseLayout, serializeLayout } from "../../state/paneFormat";
import { groups, openTab, singlePane } from "../../state/panes";
import { noteWorktree, resetRoots } from "../../state/sessionRoots";
import { PANES_STORAGE_KEY } from "../../state/usePanes";
import { createViki, type Viki } from "../../state/viki";
import { fakeDurable } from "../../testDurable";
import {
  capabilitiesFixture,
  fakeShellApi,
  listingFixture,
  settingsFixture,
  workspaceFixture,
} from "../../testShell";

const ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const OTHER = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const CODE = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const WORKTREE = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
const clients: Viki[] = [];

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  resetRoots();
});
afterEach(() => {
  cleanup();
  for (const client of clients.splice(0)) client.client.close();
  sessionStorage.clear();
  localStorage.clear();
  resetRoots();
  vi.restoreAllMocks();
  vi.useRealTimers();
  delete (navigator as { platform?: string }).platform;
});

function metadata(extra: Partial<SessionMetadata> = {}): SessionMetadataApi {
  let row: SessionMetadata = {
    session_id: ID,
    revision: 4,
    title: "Target",
    pinned: false,
    archived: false,
    unread: false,
    color: null,
    ...extra,
  };
  return {
    list: vi.fn(async () => [row]),
    patch: vi.fn(async (id, revision, patch) => {
      row = { ...row, ...patch, session_id: id, revision: revision + 1 };
      return row;
    }),
    remove: vi.fn(),
  };
}

async function mount(props: Partial<AppProps> = {}, automatic = true) {
  const socket = new FakeSocket(automatic);
  const viki = createViki({
    url: "ws://127.0.0.1:8765/ws",
    token: "synthetic",
    createSocket: () => socket,
  });
  clients.push(viki);
  viki.client.connect();
  const durable = props.durable ?? fakeDurable();
  const organization = props.organization ?? metadata();
  let view!: ReturnType<typeof render>;
  await act(async () => {
    view = render(
      <App
        viki={viki}
        rest={fakeRest({ sessions: [sessionRow({ session_id: ID, title: "Target" })] })}
        durable={durable}
        organization={organization}
        inventoryApi={{ get: vi.fn() }}
        {...props}
      />,
    );
    socket.open();
  });
  return { ...view, socket, viki, durable, organization };
}
// usePanes' mirror of the tab layout, seeded into both browser storages and read from either,
// so these tests hold whichever one it keeps the layout in.
function storeTabs(id: string) {
  const text = serializeLayout(openTab(singlePane(), id));
  sessionStorage.setItem(PANES_STORAGE_KEY, text);
  localStorage.setItem(PANES_STORAGE_KEY, text);
}
function storedTabs(): string[] {
  const layout = parseLayout(
    sessionStorage.getItem(PANES_STORAGE_KEY) ?? localStorage.getItem(PANES_STORAGE_KEY),
  );
  return layout ? groups(layout).flatMap((group) => group.tabs) : [];
}
async function click(target: HTMLElement) {
  await act(async () => {
    fireEvent.click(target);
  });
}
function row(container: HTMLElement) {
  const found = container.querySelector<HTMLElement>(`[data-session-id="${ID}"]`);
  if (!found) throw new Error("No synthetic row");
  return found;
}
async function menu(container: HTMLElement) {
  await click(within(row(container)).getByRole("button", { name: "Session menu" }));
  return screen.getByRole("menu");
}

async function history(socket: FakeSocket, error?: "transcript_missing") {
  const request = socket.sent.findLast((frame) => frame.type === "history.more");
  if (!request) throw new Error("No synthetic history request");
  await act(async () => {
    socket.receive({
      v: 2,
      type: "history.page",
      session_id: ID,
      generation: request.generation,
      request_id: request.request_id,
      seq: 1,
      data: { events: [], next_before_uuid: null, ...(error ? { error } : {}) },
    });
  });
}

test("launch target suppresses restored navigation and tabs and subscribes existing-only without a turn", async () => {
  sessionStorage.setItem("viki.selected", OTHER);
  storeTabs(OTHER);
  sessionStorage.setItem("viki.view", JSON.stringify({ view: "settings" }));
  const { socket, container } = await mount({ sessionHandoff: { kind: "target", sessionId: ID } });
  expect(
    socket.sent
      .filter((frame) => frame.type === "session.open")
      .map((frame) => [frame.session_id, frame.existing_only]),
  ).toEqual([[ID, true]]);
  expect(container.querySelector(".sess-selected")?.getAttribute("data-session-id")).toBe(ID);
  expect(container.querySelector(`#session-tab-${OTHER}`)).toBeNull();
  expect(socket.sent.some((frame) => frame.type === "turn.send")).toBe(false);
});

test("an admitted launch target gains the normal selected tab only after synchronization", async () => {
  const { container, socket } = await mount(
    { sessionHandoff: { kind: "target", sessionId: ID } },
    false,
  );
  expect(container.querySelector('.session-tabs [role="tab"]')).toBeNull();
  expect(sessionStorage.getItem("viki.selected")).not.toBe(ID);
  await act(async () => {
    socket.synchronize(ID);
  });
  expect(container.querySelector('.session-tabs [role="tab"][aria-selected="true"]')?.id).toBe(
    `session-tab-${ID}`,
  );
  expect(sessionStorage.getItem("viki.selected")).toBe(ID);
  expect(storedTabs()).toEqual([ID]);
  expect(
    socket.sent
      .filter((frame) => frame.type === "session.open")
      .map((frame) => [frame.session_id, frame.existing_only]),
  ).toEqual([[ID, true]]);
  expect(socket.sent.some((frame) => frame.type === "turn.send")).toBe(false);
});

test("a launch target stays in Chat when incomplete onboarding would otherwise open automatically", async () => {
  const shell = fakeShellApi({
    capabilities: capabilitiesFixture({
      workspace: { active: null, chat_only: true, shared_scratch: false },
    }),
    settings: settingsFixture({ "onboarding.completed": false }),
    workspaces: listingFixture([]),
  });
  const { container, socket } = await mount({
    shell,
    sessionHandoff: { kind: "target", sessionId: ID },
  });
  expect(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" }))
      .getByRole("button", { name: "Chat" })
      .getAttribute("aria-current"),
  ).toBe("page");
  expect(container.querySelector(".sess-selected")?.getAttribute("data-session-id")).toBe(ID);
  expect(
    socket.sent
      .filter((frame) => frame.type === "session.open")
      .map((frame) => [frame.session_id, frame.existing_only]),
  ).toEqual([[ID, true]]);
  expect(socket.sent.some((frame) => frame.type === "turn.send")).toBe(false);
});

test("refused and invalid handoffs show a truthful unavailable view without composer or automatic fallback", async () => {
  const { socket, viki, container } = await mount(
    { sessionHandoff: { kind: "target", sessionId: ID } },
    false,
  );
  const request = socket.sent.find((frame) => frame.type === "session.open");
  await act(async () => {
    socket.receive({
      v: 2,
      type: "session.open.refused",
      session_id: ID,
      generation: "",
      request_id: request?.request_id,
      seq: 0,
      data: { reason: "unavailable" },
    });
  });
  expect(screen.getByText("This conversation is unavailable.")).toBeTruthy();
  expect(container.querySelector('.session-tabs [role="tab"]')).toBeNull();
  expect(sessionStorage.getItem("viki.selected")).not.toBe(ID);
  expect(screen.queryByRole("textbox", { name: "Message" })).toBeNull();
  expect(Object.keys(viki.store.getSnapshot().sessions)).toEqual([ID]);
  expect(socket.sent.filter((frame) => frame.type === "session.open")).toHaveLength(1);
  cleanup();
  const invalid = await mount({ sessionHandoff: { kind: "invalid" } });
  expect(screen.getByText("This conversation link is invalid.")).toBeTruthy();
  expect(screen.queryByRole("textbox", { name: "Message" })).toBeNull();
  expect(invalid.socket.sent.some((frame) => frame.type === "session.open")).toBe(false);
});

test("new chat shows the current draft and attachment without starter buttons or a turn", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  const durable = fakeDurable();
  const original = durable.getDraft;
  const getDraft = vi.spyOn(durable, "getDraft").mockImplementation(async (key) => ({
    ...(await original(key)),
    text: "Current draft",
    attachments: [
      {
        asset_id: OTHER,
        media_type: "image/png",
        byte_size: 1,
        width: 1,
        height: 1,
        name: "synthetic.png",
        modified_at: null,
        source: "picker",
      },
    ],
  }));
  const assets: AssetsApi = { upload: vi.fn(), read: vi.fn(() => new Promise<Blob>(() => {})) };
  const { socket } = await mount({ durable, assets });
  const key = getDraft.mock.calls[0]?.[0];
  const box = screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
  expect(box.value).toBe("Current draft");
  expect(screen.getByText("New conversation")).toBeTruthy();
  expect(screen.queryByText(/You are talking to an AI assistant/)).toBeNull();
  for (const name of ["Explore an idea", "Review a note", "Work on code"]) {
    expect(screen.queryByRole("button", { name })).toBeNull();
  }
  expect(getDraft.mock.calls.map((call) => call[0])).toEqual([key]);
  expect(screen.getByText("synthetic.png")).toBeTruthy();
  expect(
    socket.sent.some((frame) => frame.type === "session.open" || frame.type === "turn.send"),
  ).toBe(false);
});

test("unread changes use CAS and clear only after an explicit successful visit", async () => {
  const organization = metadata({ unread: true, color: "purple" });
  const { container, socket } = await mount({ organization }, false);
  expect(row(container).getAttribute("data-unread")).toBe("true");
  await click(row(container));
  expect(organization.patch).not.toHaveBeenCalled();
  await act(async () => {
    socket.synchronize(ID);
  });
  expect(organization.patch).toHaveBeenCalledExactlyOnceWith(ID, 4, { unread: false });
  await click(within(await menu(container)).getByRole("menuitem", { name: "Mark as unread" }));
  expect(organization.patch).toHaveBeenLastCalledWith(ID, 5, { unread: true });
  await act(async () => {
    socket.synchronize(ID);
  });
  expect(organization.patch).toHaveBeenCalledTimes(2);
  expect(row(container).getAttribute("data-unread")).toBe("true");
  await click(row(container));
  expect(organization.patch).toHaveBeenLastCalledWith(ID, 6, { unread: false });
});

test("a refused handoff keeps its unread label", async () => {
  const organization = metadata({ unread: true });
  const { socket } = await mount(
    { organization, sessionHandoff: { kind: "target", sessionId: ID } },
    false,
  );
  const request = socket.sent.find((frame) => frame.type === "session.open");
  await act(async () => {
    socket.receive({
      v: 2,
      type: "session.open.refused",
      session_id: ID,
      generation: "",
      request_id: request?.request_id,
      seq: 0,
      data: { reason: "storage_unavailable" },
    });
  });
  expect(organization.patch).not.toHaveBeenCalled();
  expect(screen.queryByRole("textbox", { name: "Message" })).toBeNull();
});

test("a source that disappears after handoff sync keeps its ID and shows the missing-record feedback", async () => {
  const { socket, viki, container } = await mount({
    sessionHandoff: { kind: "target", sessionId: ID },
  });
  await history(socket, "transcript_missing");
  expect(
    screen.getByText(
      "Can't find the conversation record. Claude Code may have deleted it or moved it.",
    ),
  ).toBeTruthy();
  expect(container.querySelector(".empty")).toBeNull();
  expect(container.querySelector(".sess-selected")?.getAttribute("data-session-id")).toBe(ID);
  expect(Object.keys(viki.store.getSnapshot().sessions)).toEqual([ID]);
  expect(
    socket.sent
      .filter((frame) => frame.type === "session.open")
      .map((frame) => frame.existing_only),
  ).toEqual([true]);
  expect(socket.sent.some((frame) => frame.type === "turn.send")).toBe(false);
});

test("handoff refusal is retried only by its explicit button and never creates another target", async () => {
  const durable = fakeDurable();
  const getDraft = vi.spyOn(durable, "getDraft");
  const { socket, viki } = await mount(
    { durable, sessionHandoff: { kind: "target", sessionId: ID } },
    false,
  );
  const request = socket.sent.find((frame) => frame.type === "session.open");
  await act(async () => {
    socket.receive({
      v: 2,
      type: "session.open.refused",
      session_id: ID,
      generation: "",
      request_id: request?.request_id,
      seq: 0,
      data: { reason: "unavailable" },
    });
  });
  expect(getDraft).not.toHaveBeenCalled();
  await click(screen.getByRole("button", { name: "Try this conversation again" }));
  expect(
    socket.sent
      .filter((frame) => frame.type === "session.open")
      .map((frame) => [frame.session_id, frame.existing_only]),
  ).toEqual([
    [ID, true],
    [ID, true],
  ]);
  await act(async () => {
    socket.synchronize(ID);
  });
  expect(Object.keys(viki.store.getSnapshot().sessions)).toEqual([ID]);
  expect(getDraft).toHaveBeenCalledExactlyOnceWith(ID);
  expect(socket.sent.some((frame) => frame.type === "turn.send")).toBe(false);
});

test("restored selection is not an explicit visit and keeps its unread label", async () => {
  sessionStorage.setItem("viki.selected", ID);
  const organization = metadata({ unread: true });
  const { container, viki } = await mount({ organization });
  await act(async () => {
    viki.actions.openSession(ID);
  });
  expect(container.querySelector(".sess-selected")?.getAttribute("data-session-id")).toBe(ID);
  expect(row(container).getAttribute("data-unread")).toBe("true");
  expect(organization.patch).not.toHaveBeenCalled();
});

test("the color submenu writes the current metadata revision and updates the actual row", async () => {
  const organization = metadata();
  const { container } = await mount({ organization });
  const trigger = within(await menu(container)).getByRole("menuitem", { name: "Color label" });
  await act(async () => {
    fireEvent.keyDown(trigger, { key: "ArrowRight" });
  });
  await click(screen.getByRole("menuitemradio", { name: "Blue color label" }));
  expect(organization.patch).toHaveBeenCalledExactlyOnceWith(ID, 4, { color: "blue" });
  expect(within(row(container)).getByRole("img", { name: "Blue color label" })).toBeTruthy();
  expect(container.querySelector(".sess-selected")).toBeNull();
});

test("window requests disable repeated clicks and are aborted on unmount", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  let resolve!: () => void;
  const open = vi.fn(
    (_id: string, _signal?: AbortSignal) =>
      new Promise<void>((done) => {
        resolve = done;
      }),
  );
  const { container, unmount } = await mount({ sessionWindowsApi: { open } });
  await click(within(await menu(container)).getByRole("menuitem", { name: "New window" }));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1);
  });
  await act(async () => {
    fireEvent.keyDown(within(row(container)).getByRole("button", { name: "Session menu" }), {
      key: "ArrowDown",
    });
  });
  const pending = screen.getByRole("menuitem", { name: "Opening window…" });
  expect(pending.getAttribute("aria-disabled")).toBe("true");
  await click(pending);
  expect(open).toHaveBeenCalledOnce();
  const signal = open.mock.calls[0]?.[1];
  unmount();
  expect(signal?.aborted).toBe(true);
  await act(async () => {
    resolve();
  });
  expect(screen.queryByRole("alert")).toBeNull();
});

test("an empty profile chat shows its bound identity and current editor", async () => {
  const preview: PreparedProfilePreview = {
    launchable: true,
    fields: [],
    blockers: [],
    warnings: [],
    prefs: { mode: "default", effort: "high", model: null },
    session_id: ID,
    profile_id: OTHER,
    profile_revision: 2,
    generation: null,
    workspace_id: CODE,
    workspace_revision: 1,
    policy: "ordinary",
    policy_revision: 1,
    settings_revision: 1,
    runtime: "claude",
    capability_key: "a".repeat(64),
    draft: { key: ID, revision: 1, text: "", saved_at: null, attachments: [] },
    identity: {
      name: "Synthetic identity",
      avatar_asset_id: null,
      accent: "orange",
      shape: "rounded",
      profile_revision: 2,
    },
  };
  const launch: ProfileLaunchApi = {
    capabilities: vi.fn(),
    models: vi.fn(),
    prepare: vi.fn(),
    inspect: vi.fn(async () => preview),
  };
  const durable = fakeDurable();
  const getDraft = vi.spyOn(durable, "getDraft");
  const { container, socket } = await mount({
    profileLaunchApi: launch,
    durable,
    sessionHandoff: { kind: "target", sessionId: ID },
  });
  await history(socket);
  expect((screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement).value).toBe("");
  expect(container.querySelector('[data-region="conversation-header"]')?.textContent).toContain(
    "Synthetic identity · r2",
  );
  expect(container.querySelector(".empty-brand")).not.toBeNull();
  expect(container.querySelector(".empty-brand-face")).toBeNull();
  expect(container.querySelector(".empty-sub")?.textContent).toBe(
    "Talk to Synthetic identity. Use / to call a skill.",
  );
  expect(getDraft).toHaveBeenCalledExactlyOnceWith(ID);
  expect(socket.sent.filter((frame) => frame.type === "session.open")).toHaveLength(1);
  expect(socket.sent.some((frame) => frame.type === "turn.send")).toBe(false);
});

test("landing tiles use at most six actual organized rows and open only their chosen session", async () => {
  const ids = [
    ID,
    OTHER,
    CODE,
    WORKTREE,
    "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
    "ffffffff-ffff-4fff-8fff-ffffffffffff",
    "00000000-0000-4000-8000-000000000000",
  ];
  const organization = metadata({ pinned: true });
  const { container, socket } = await mount({
    organization,
    rest: fakeRest({
      sessions: ids.map((session_id, index) =>
        sessionRow({ session_id, title: `Synthetic ${index}` }),
      ),
    }),
  });
  const tiles = container.querySelectorAll<HTMLButtonElement>("[data-landing-session-id]");
  expect(tiles).toHaveLength(6);
  expect(
    Array.from(tiles).every((tile) =>
      ids.includes(tile.getAttribute("data-landing-session-id") ?? ""),
    ),
  ).toBe(true);
  const tile = tiles[0];
  if (!tile) throw new Error("No synthetic tile");
  const sessionId = tile.getAttribute("data-landing-session-id");
  await click(tile);
  expect(
    socket.sent.filter((frame) => frame.type === "session.open").map((frame) => frame.session_id),
  ).toEqual([sessionId]);
  expect(socket.sent.some((frame) => frame.type === "turn.send")).toBe(false);
});

test("new-window action preserves selection, reports failure and retries only on a new click", async () => {
  const open = vi
    .fn()
    .mockRejectedValueOnce(new SessionWindowsError("unavailable"))
    .mockResolvedValueOnce(undefined);
  const { container, socket } = await mount({ sessionWindowsApi: { open } });
  await click(within(await menu(container)).getByRole("menuitem", { name: "New window" }));
  expect(open).toHaveBeenCalledOnce();
  expect(container.querySelector(".sess-selected")).toBeNull();
  expect(socket.sent.some((frame) => frame.type === "session.open")).toBe(false);
  const panel = await menu(container);
  expect(screen.getByText(/conversation is unavailable/)).toBeTruthy();
  expect(open).toHaveBeenCalledOnce();
  await click(within(panel).getByRole("menuitem", { name: "New window" }));
  expect(open).toHaveBeenCalledTimes(2);
});

test("export reveals existing selected-turn controls without claiming an export or sending", async () => {
  const archives: ChatArchivesApi = {
    list: vi.fn(async () => []),
    get: vi.fn(),
    save: vi.fn(),
    remove: vi.fn(),
  };
  const assets: AssetsApi = { upload: vi.fn(), read: vi.fn() };
  const { container, socket } = await mount({ archives, assets });
  await click(
    within(await menu(container)).getByRole("menuitem", { name: "Select turns to export" }),
  );
  const panel = container.querySelector<HTMLElement>(".conversation-tools");
  expect(panel?.hidden).toBe(false);
  expect(document.activeElement).toBe(panel);
  expect(
    screen
      .getByRole("button", { name: "Find, select and export (Ctrl+F)" })
      .getAttribute("aria-expanded"),
  ).toBe("true");
  expect(archives.save).not.toHaveBeenCalled();
  expect(socket.sent.some((frame) => frame.type === "turn.send")).toBe(false);
});

test.each(["Win32", "MacIntel", "Linux x86_64"])(
  "terminal on %s uses the pinned worktree and workspace action opens the chooser",
  async (platform) => {
    Object.defineProperty(navigator, "platform", { value: platform, configurable: true });
    const code = workspaceFixture({ workspace_id: CODE, kind: "code", name: "Code lab" });
    const shell = fakeShellApi({
      capabilities: capabilitiesFixture({
        workspace: {
          active: {
            id: CODE,
            kind: "code",
            name: "Code lab",
            policy: "ordinary",
            layout: "unknown",
            source: "created",
            aliased: false,
            root_available: true,
          },
          chat_only: false,
          shared_scratch: false,
        },
      }),
      settings: settingsFixture({ "onboarding.completed": true }),
      workspaces: { ...listingFixture([code]), active_id: CODE, chat_only: false },
    });
    const rootsApi: RootsApi = {
      roots: async () => ({
        roots: [{ session_id: ID, workspace_id: CODE, worktree_id: WORKTREE }],
        truncated: false,
      }),
      worktrees: async () => ({
        worktrees: [
          {
            worktree_id: WORKTREE,
            workspace_id: CODE,
            state: "ready",
            branch: "synthetic",
            base_oid: "a".repeat(40),
            path: "<workspace>/worktree",
            created_at: "",
            updated_at: "",
          },
        ],
        truncated: false,
      }),
      record: vi.fn(),
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
    const terminalApi = { open: vi.fn(async () => {}) };
    const codeApi: CodeApi = {
      tree: vi.fn(() => new Promise<CodeTree>(() => {})),
      file: vi.fn(),
      search: vi.fn(),
      changes: vi.fn(),
      diff: vi.fn(),
      save: vi.fn(),
    };
    const { container, socket } = await mount({ shell, rootsApi, terminalApi, codeApi });
    await click(within(await menu(container)).getByRole("menuitem", { name: "Open in terminal" }));
    expect(terminalApi.open).toHaveBeenCalledExactlyOnceWith(
      CODE,
      WORKTREE,
      expect.any(AbortSignal),
    );
    expect(await screen.findByText("Opened a terminal in this session's folder.")).toBeTruthy();
    const worktree = (await rootsApi.worktrees()).worktrees[0];
    if (!worktree) throw new Error("No synthetic worktree");
    await act(async () => {
      noteWorktree({ ...worktree, state: "removed" });
    });
    const choices = await menu(container);
    expect(within(choices).queryByRole("menuitem", { name: "Open in terminal" })).toBeNull();
    await click(within(choices).getByRole("menuitem", { name: "New chat in workspace…" }));
    expect(screen.getByRole("combobox", { name: "Code folder" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "New session in this folder" })).toBeTruthy();
    expect(rootsApi.record).not.toHaveBeenCalled();
    expect(
      socket.sent.some((frame) => frame.type === "session.open" || frame.type === "turn.send"),
    ).toBe(false);
  },
);
