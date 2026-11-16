/**
 * File panes as App wires them: a file tab's pane shows its file through this window's
 * registered code folder; the Code tree, the Git chip and a dropped file open files in panes when
 * one fits, else in the middle preview; restored tabs outside this window's code folders, or whose
 * file is gone, drop quietly; the Message box stays under the chat pane; one pane at a time edits
 * a file; and a file tab's open saves the layout while a pane focus does not. The layout model,
 * the preview and the tab strip have their own suites.
 */
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki, sessionRow } from "../../accept/m3b/fakes";
import type { KeySpec, SettingsView } from "../../clients/app/shellApi";
import { type CodeApi, CodeApiError } from "../../clients/code/codeApi";
import type { SessionMetadataApi } from "../../clients/conversations/sessionMetadataApi";
import type { SessionWindowsApi } from "../../clients/conversations/sessionWindowsApi";
import { CHAT_DRAG_TYPE, FILE_DRAG_TYPE } from "../../components/Panes/paneDrop";
import type { EditorHandle, EditorOptions } from "../../components/Workbench/codemirror";
import { dirtyRefs, dropBuffer, editorOwner, putBuffer } from "../../state/codeBuffers";
import { parseLayout, serializeLayout } from "../../state/paneFormat";
import {
  focusGroup,
  moveTab,
  openFile,
  openTab,
  type PaneLayout,
  singlePane,
  splitFile,
} from "../../state/panes";
import { setUnsavedWork } from "../../state/unsavedWork";
import { PANES_STORAGE_KEY } from "../../state/usePanes";
import { fakeDurable } from "../../testDurable";
import {
  capabilitiesFixture,
  type FakeShellApi,
  fakeShellApi,
  listingFixture,
  settingsFixture,
  workspaceFixture,
} from "../../testShell";

// Keep CodePreview and CodeEditor real; only the lazy, layout-dependent editor facade is faked.
vi.mock("../../components/Workbench/codemirror", () => ({
  createEditor: async (options: EditorOptions): Promise<EditorHandle> => {
    const area = document.createElement("textarea");
    area.setAttribute("aria-label", options.label);
    area.value = options.text;
    options.parent.append(area);
    return {
      text: () => area.value,
      setText: (text) => {
        area.value = text;
      },
      setBase: () => {},
      setWrap: () => {},
      setFontSize: () => {},
      focus: () => area.focus(),
      destroy: () => area.remove(),
    };
  },
  codeTokens: async () => [],
}));

const CHAT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const CHAT2 = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const TITLES: Record<string, string> = { [CHAT]: "Chat", [CHAT2]: "Second" };
const W = "55555555-5555-4555-8555-555555555555";
const OTHER = "66666666-6666-4666-8666-666666666666";
const TEXT = "first line\nsecond line";
const alpha = workspaceFixture({
  workspace_id: W,
  kind: "code",
  name: "Alpha",
  root: "D:\\Code\\Alpha",
  root_physical_id: "physical-Alpha",
});

/** `useLayoutMode`'s own registered listeners, kept across `stubWidth` calls so a later call in
 * the same test can notify them, as a real browser fires a `MediaQueryList`'s "change" event. */
const mediaListeners = new Map<string, Set<(event: MediaQueryListEvent) => void>>();
let currentWidth = 1440;
/** One matchMedia for the whole App, answering `(min-width: Npx)` for a viewport of `width`.
 * Calling it again after mount changes the width live: changed queries notify subscribers, so
 * `useLayoutMode`'s `useSyncExternalStore` re-polls it. */
function stubWidth(width: number): void {
  const matches = (query: string) => {
    const min = /min-width:\s*(\d+)px/.exec(query)?.[1];
    return min !== undefined && currentWidth >= Number(min);
  };
  const previous = new Map([...mediaListeners.keys()].map((query) => [query, matches(query)]));
  currentWidth = width;
  vi.stubGlobal("matchMedia", (query: string) => ({
    media: query,
    get matches() {
      return matches(query);
    },
    addEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => {
      const listeners = mediaListeners.get(query) ?? new Set();
      listeners.add(listener);
      mediaListeners.set(query, listeners);
    },
    removeEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => {
      mediaListeners.get(query)?.delete(listener);
    },
  }));
  for (const [query, listeners] of mediaListeners) {
    if (previous.get(query) === matches(query)) continue;
    const event = Object.assign(new Event("change"), { media: query, matches: matches(query) });
    for (const listener of [...listeners]) listener(event);
  }
}

beforeEach(() => {
  window.sessionStorage.clear();
  mediaListeners.clear();
  // Wide enough to dock both rails, so the panes may sit side by side.
  stubWidth(1440);
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  for (const ref of dirtyRefs(W)) dropBuffer(W, ref);
  window.sessionStorage.clear();
});

function organization(): SessionMetadataApi {
  return {
    list: async () => [],
    patch: async () => {
      throw new Error("unused");
    },
    remove: async () => true,
  };
}

/** A code client whose tree lists a.ts and b.ts, whose files all read `TEXT` except `gone`, which
 * answer `not_found`, and whose one change is a.ts. */
function codeApi(gone: readonly string[] = []): CodeApi {
  return {
    tree: vi.fn<CodeApi["tree"]>(async (_id, path) => ({
      path,
      entries: ["a.ts", "b.ts"].map((name) => ({ name, type: "file" as const, size: 10 })),
      truncated: false,
    })),
    file: vi.fn<CodeApi["file"]>(async (_id, path) => {
      if (gone.includes(path)) throw new CodeApiError("not_found");
      return {
        path,
        size: 10,
        sha256: "a".repeat(64),
        encoding: "utf-8",
        newline: "lf",
        text: TEXT,
        reason: null,
      };
    }),
    changes: vi.fn<CodeApi["changes"]>(async () => ({
      state: "ready",
      branch: "main",
      head: "abcdef123456",
      detached: false,
      upstream: null,
      ahead: null,
      behind: null,
      entries: [
        {
          path: "a.ts",
          orig_path: null,
          index: ".",
          worktree: "M",
          kind: "tracked",
          private: false,
        },
      ],
      truncated: false,
    })),
    diff: vi.fn<CodeApi["diff"]>(async (_id, path) => ({
      path,
      text: "@@ -1 +1 @@\n+changed line",
      reason: null,
    })),
    search: vi.fn(),
    save: vi.fn(),
  };
}

/** `testShell.ts`'s settings fixture predates `layout.panes`; a test that saves the layout adds
 * the key itself, as `src/app-tests/shell/panesSettings.unit.test.tsx` does. */
function withPanesKey(view: SettingsView): SettingsView {
  const spec: KeySpec = {
    scope: "app",
    applies: "now",
    kind: "string",
    allowed: null,
    default: "",
    personal: true,
  };
  return {
    ...view,
    keys: { ...view.keys, "layout.panes": spec },
    values: { ...view.values, "layout.panes": "" },
    effective: { ...view.effective, "layout.panes": "" },
    sources: { ...view.sources, "layout.panes": "default" },
  };
}

async function settled(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 8; i++) await Promise.resolve();
  });
}

async function mount(options: {
  stored?: PaneLayout;
  code?: CodeApi;
  settings?: SettingsView;
  windows?: SessionWindowsApi;
  /** The chats the session listing holds; CHAT alone by default. */
  chats?: readonly string[];
}) {
  if (options.stored)
    window.sessionStorage.setItem(PANES_STORAGE_KEY, serializeLayout(options.stored));
  const fake = fakeViki();
  const shell = fakeShellApi({
    settings: options.settings ?? settingsFixture({ "onboarding.completed": true }),
    capabilities: capabilitiesFixture({
      workspace: {
        shared_scratch: false,
        chat_only: false,
        active: {
          id: W,
          kind: "code",
          name: "Alpha",
          policy: "ordinary",
          layout: "unknown",
          source: "created",
          aliased: false,
          root_available: true,
        },
      },
    }),
    workspaces: { ...listingFixture([{ ...alpha }]), active_id: W, chat_only: false },
  });
  const code = options.code ?? codeApi();
  await act(async () => {
    render(
      <App
        viki={fake.viki}
        rest={fakeRest({
          sessions: (options.chats ?? [CHAT]).map((id) =>
            sessionRow({ session_id: id, title: TITLES[id] ?? "" }),
          ),
        })}
        durable={fakeDurable()}
        organization={organization()}
        shell={shell}
        codeApi={code}
        {...(options.windows ? { sessionWindowsApi: options.windows } : {})}
      />,
    );
    fake.socket.open();
  });
  await settled();
  await settled();
  return { ...fake, shell, code };
}

async function click(element: Element): Promise<void> {
  await act(async () => {
    fireEvent.click(element);
  });
  await settled();
}
/** A real press: the pointerdown that focuses a pane, then the click on the same element. */
async function press(element: Element): Promise<void> {
  await act(async () => {
    fireEvent.pointerDown(element);
    fireEvent.click(element);
  });
  await settled();
}
async function pointerDown(element: Element): Promise<void> {
  await act(async () => {
    fireEvent.pointerDown(element);
  });
}
/** Advances the faked `setTimeout` clock, so the 1 s `layout.panes` debounce fires on command
 * rather than after a real sleep. */
async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

/** jsdom never measures the shell frame: include both docked rails in the requested center room. */
function stubRoom(room: number): void {
  const frame = document.querySelector(".shell-body") as HTMLElement;
  Object.defineProperty(frame, "clientWidth", { configurable: true, value: room + 272 + 320 + 1 });
  Object.defineProperty(frame, "clientHeight", { configurable: true, value: 800 });
  act(() => {
    window.dispatchEvent(new Event("resize"));
  });
}

/** jsdom's DragEvent carries neither clientX nor dataTransfer through fireEvent, so a MouseEvent
 * near the right edge stands in, as in `src/app-tests/shell/panes.unit.test.tsx`. */
function fileDrag(type: string, payload: unknown): Event {
  const event = new MouseEvent(type, {
    bubbles: true,
    cancelable: true,
    clientX: 990,
    clientY: 400,
  });
  const text = JSON.stringify(payload);
  Object.defineProperty(event, "dataTransfer", {
    configurable: true,
    value: {
      types: [FILE_DRAG_TYPE],
      getData: (kind: string) => (kind === FILE_DRAG_TYPE ? text : ""),
    },
  });
  return event;
}
async function dropFile(target: HTMLElement, payload: unknown): Promise<void> {
  act(() => {
    target.dispatchEvent(fileDrag("dragover", payload));
  });
  await act(async () => {
    target.dispatchEvent(fileDrag("drop", payload));
  });
  await settled();
}
/** A tab dragged as a strip drags one: its id under `CHAT_DRAG_TYPE`, at (x, y). */
function tabDrag(type: string, id: string, x: number, y: number): Event {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: y });
  Object.defineProperty(event, "dataTransfer", {
    configurable: true,
    value: {
      types: [CHAT_DRAG_TYPE],
      getData: (kind: string) => (kind === CHAT_DRAG_TYPE ? id : ""),
    },
  });
  return event;
}
async function dropTab(target: HTMLElement, id: string, x: number, y: number): Promise<void> {
  act(() => {
    target.dispatchEvent(tabDrag("dragover", id, x, y));
  });
  await act(async () => {
    target.dispatchEvent(tabDrag("drop", id, x, y));
  });
  await settled();
}
/** jsdom lays nothing out: give `element` a box for the drop zones. */
function stubRect(element: HTMLElement, width: number, height: number): void {
  element.getBoundingClientRect = () =>
    ({ left: 0, top: 0, right: width, bottom: height, width, height, x: 0, y: 0 }) as DOMRect;
}
/** A key pressed on the page; false when App took it (default prevented). */
function key(init: KeyboardEventInit): boolean {
  let notPrevented = true;
  act(() => {
    notPrevented = fireEvent.keyDown(document.body, init);
  });
  return notPrevented;
}

const paneSections = () => [...document.querySelectorAll<HTMLElement>("section.conversation.pane")];
const pane = (index: number) => paneSections()[index] as HTMLElement;
const centerPane = () => document.querySelector('[data-region="center-pane"]');
const fileTexts = (root: ParentNode) =>
  [...root.querySelectorAll(".code-text")].map((line) => line.textContent);
const tabNames = (root: ParentNode) =>
  [...root.querySelectorAll(".session-tab-label")].map((label) => label.textContent);
const layoutPatches = (shell: FakeShellApi) =>
  shell.calls.filter(
    (call) =>
      call.method === "patchSettings" &&
      Object.keys(call.args[2] as Record<string, unknown>).includes("layout.panes"),
  );
async function openTree(): Promise<void> {
  await click(
    within(screen.getByRole("tablist", { name: "Right sidebar" })).getByRole("tab", {
      name: "Code",
    }),
  );
}
const treeFile = (name: string) =>
  within(screen.getByRole("region", { name: "Code sidebar" })).getByRole("button", { name });

/** The chat alone in one pane. */
const chatAlone = () => openTab(singlePane(), CHAT);
/** The chat, and beside it a file pane holding `ref` of the code folder (the file pane focused). */
function chatAndFile(file = { workspaceId: W, ref: "src/a.ts" }): PaneLayout {
  return withFile(chatAlone(), file.ref, file.workspaceId);
}
/** `layout` with `ref` opened in its focused file pane, else in a new one beside the chat. */
function withFile(layout: PaneLayout, ref: string, workspaceId = W): PaneLayout {
  const next = openFile(layout, { workspaceId, ref }, () => true);
  if (!next) throw new Error("no file pane");
  return next;
}
/** The layout this window mirrors to its session storage, as text. */
const storedLayout = () => window.sessionStorage.getItem(PANES_STORAGE_KEY);

async function openWholeCodeWithSidebar() {
  window.sessionStorage.setItem(
    "viki.view",
    JSON.stringify({ view: "code", page: "general", panel: "locations" }),
  );
  const mounted = await mount({ stored: chatAlone() });
  await openTree();
  return mounted;
}

test("a file chosen from whole Code opens a visible center preview in Chat", async () => {
  await openWholeCodeWithSidebar();
  expect(document.querySelectorAll('[data-region="right-rail"]')).toHaveLength(1);
  await click(treeFile("a.ts"));
  expect(document.querySelector('[data-region="view"]')).toBeNull();
  expect(centerPane()?.textContent).toContain("first line");
  expect(screen.getByRole("region", { name: "Code content" })).toBeTruthy();
});

test("a stored file pane too wide for both rails cannot swallow a Code sidebar file", async () => {
  window.sessionStorage.setItem(
    "viki.view",
    JSON.stringify({ view: "code", page: "general", panel: "locations" }),
  );
  await mount({ stored: chatAndFile() });
  await openTree();
  stubRoom(500);
  await click(treeFile("b.ts"));
  expect(document.querySelector('[data-region="view"]')).toBeNull();
  expect(paneSections()).toHaveLength(0);
  expect(centerPane()?.textContent).toContain("first line");
  expect(document.getElementById("session-tab-f1")).not.toBeNull();
});

test("the whole Code view opens its right sidebar as a compact drawer", async () => {
  stubWidth(700);
  window.sessionStorage.setItem(
    "viki.view",
    JSON.stringify({ view: "code", page: "general", panel: "locations" }),
  );
  await mount({ stored: chatAlone() });
  expect(document.querySelector('[data-region="right-rail"]')).toBeNull();
  await click(screen.getByRole("button", { name: "VIKI panel" }));
  const drawer = screen.getByRole("dialog", { name: "VIKI panel" });
  await openTree();
  expect(within(drawer).getByRole("region", { name: "Code sidebar" })).toBeTruthy();
  await click(within(drawer).getByRole("button", { name: "Close" }));
  expect(screen.queryByRole("dialog", { name: "VIKI panel" })).toBeNull();
});

test("refused leave from whole Code keeps its view and file selection unchanged", async () => {
  const { code } = await openWholeCodeWithSidebar();
  const before = storedLayout();
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  act(() => setUnsavedWork("routine", true));
  try {
    await click(treeFile("a.ts"));
    expect(confirm).toHaveBeenCalledOnce();
    expect(document.querySelector('[data-region="view"]')).not.toBeNull();
    expect(centerPane()).toBeNull();
    expect(storedLayout()).toBe(before);
    expect(code.file).not.toHaveBeenCalled();
  } finally {
    act(() => setUnsavedWork("routine", false));
  }
});

test("a stored file tab shows its file in a pane beside the chat, named by its file alone, and the Message box stays under the chat", async () => {
  const { code } = await mount({ stored: chatAndFile() });

  expect(paneSections()).toHaveLength(2);
  const tab = within(pane(1)).getByRole("tab");
  expect(tab.querySelector(".session-tab-stem")?.textContent).toBe("a");
  expect(tab.querySelector(".session-tab-ext")?.textContent).toBe(".ts");
  expect(tab.querySelector(".session-tab-label")?.getAttribute("title")).toBe("src/a.ts");
  // No folder after the name: the code folder's name would only squeeze the stem.
  expect(tab.querySelector(".root-label")).toBeNull();
  expect(tab.textContent).not.toContain("Alpha");
  const content = within(pane(1)).getByRole("region", { name: "Code content" });
  expect(fileTexts(content)).toContain("first line");
  expect(code.file).toHaveBeenCalledWith(W, "src/a.ts", expect.any(AbortSignal));
  // The stored layout focused the file pane; the one Message box is the chat pane's all the same.
  expect(screen.getAllByLabelText("Message")).toHaveLength(1);
  expect(screen.getByLabelText("Message").closest("section.pane")).toBe(pane(0));

  await pointerDown(pane(0));
  await pointerDown(pane(1));

  expect(screen.getAllByLabelText("Message")).toHaveLength(1);
  expect(screen.getByLabelText("Message").closest("section.pane")).toBe(pane(0));
  expect(within(pane(1)).getByRole("tab", { selected: true })).toBe(tab);
});

test("a chat tab the session listing drops leaves the file pane's tabs in place", async () => {
  // GONE is neither listed, held nor live, so the listing drops its tab.
  const GONE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
  const layout = openFile(
    openTab(openTab(singlePane(), GONE), CHAT),
    { workspaceId: W, ref: "src/a.ts" },
    () => true,
  );
  if (!layout) throw new Error("no file pane");
  await mount({ stored: layout });

  expect(paneSections()).toHaveLength(2);
  expect(
    within(pane(0))
      .getAllByRole("tab")
      .map((tab) => tab.id),
  ).toEqual([`session-tab-${CHAT}`]);
  expect(tabNames(pane(1))).toEqual(["a.ts"]);
});

test("a new chat while a file pane has the focus opens in the chat pane", async () => {
  await mount({ stored: chatAndFile() });

  await click(screen.getByRole("button", { name: "New session" }));

  expect(within(pane(0)).getAllByRole("tab")).toHaveLength(2);
  expect(tabNames(pane(1))).toEqual(["a.ts"]);
  expect(screen.getByLabelText("Message").closest("section.pane")).toBe(pane(0));
});

test("a file tab's menu splits off a second view of its file; moving and new windows stay with their own kind", async () => {
  await mount({ stored: chatAndFile(), windows: { open: vi.fn() } });
  // A closed Radix menu hands the focus back to its trigger on a timer. Real, that timer could fire
  // while the next menu opens and close it as a focus outside; faked, it fires on command first.
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  const menuOf = async (title: string) => {
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: `Tab actions: ${title}` }));
    });
    return screen.getAllByRole("menuitem").map((item) => item.textContent);
  };
  // No Move into the other pane, which holds the other kind of tab.
  expect(await menuOf("Chat")).toEqual(["Split right", "Split down", "Close pane", "New window"]);
  fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
  await advance(0);
  expect(screen.queryByRole("menu")).toBeNull();
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "Tab actions: Chat" }));
  pane(1).getBoundingClientRect = () =>
    ({ left: 0, top: 0, right: 600, bottom: 800, width: 600, height: 800, x: 0, y: 0 }) as DOMRect;

  expect(await menuOf("a.ts")).toEqual(["Split right", "Split down", "Close pane"]);
  await click(screen.getByRole("menuitem", { name: "Split down" }));

  expect(paneSections()).toHaveLength(3);
  expect(tabNames(pane(1))).toEqual(["a.ts"]);
  expect(tabNames(pane(2))).toEqual(["a.ts"]);
});

test("the Code tree opens a file beside the chat when a pane fits, else in the middle preview, and reuses that pane", async () => {
  await mount({ stored: chatAlone() });
  await openTree();
  // jsdom measures nothing, so there is no room for a pane: the file opens in the middle preview.
  await click(treeFile("a.ts"));
  expect(paneSections()).toHaveLength(0);
  expect(centerPane()?.textContent).toContain("first line");
  await click(screen.getByRole("button", { name: "Close code preview" }));

  stubRoom(1000);
  await click(treeFile("a.ts"));

  expect(centerPane()).toBeNull();
  expect(paneSections()).toHaveLength(2);
  expect(tabNames(pane(1))).toEqual(["a.ts"]);
  expect(fileTexts(pane(1))).toContain("first line");

  // Both rails stay docked; the Code tree adds to the same file pane.
  expect(document.querySelectorAll('[data-region="right-rail"]')).toHaveLength(1);
  expect(screen.queryByRole("button", { name: "VIKI panel" })).toBeNull();
  await click(treeFile("b.ts"));
  expect(paneSections()).toHaveLength(2);
  expect(tabNames(pane(1))).toEqual(["a.ts", "b.ts"]);
  expect(within(pane(1)).getByRole("heading", { name: "b.ts" })).toBeTruthy();

  // A file already open there is shown again rather than opened twice.
  await click(treeFile("a.ts"));
  expect(tabNames(pane(1))).toEqual(["a.ts", "b.ts"]);
  expect(within(pane(1)).getByRole("heading", { name: "a.ts" })).toBeTruthy();
});

test("the Git chip opens a changed file in a pane beside the chat, with its diff", async () => {
  await mount({ stored: chatAlone() });
  stubRoom(1000);

  await click(document.querySelector(".git-chip") as HTMLElement);
  await click(
    within(screen.getByRole("dialog", { name: "Git" })).getByRole("button", { name: ".M a.ts" }),
  );

  expect(centerPane()).toBeNull();
  expect(paneSections()).toHaveLength(2);
  expect(within(pane(1)).getByText("+changed line")).toBeTruthy();
});

test("a file dropped on the conversation's edge opens in a new pane; one from a folder this window has not registered is ignored", async () => {
  const { code } = await mount({ stored: chatAlone() });
  const main = document.querySelector("main.conversation") as HTMLElement;
  main.getBoundingClientRect = () => ({ left: 0, top: 0, width: 1000, height: 800 }) as DOMRect;

  await dropFile(main, { workspace_id: OTHER, ref: "x.ts" });
  expect(paneSections()).toHaveLength(0);
  expect(centerPane()).toBeNull();
  expect(code.file).not.toHaveBeenCalled();

  await dropFile(main, { workspace_id: W, ref: "a.ts" });
  expect(paneSections()).toHaveLength(2);
  expect(tabNames(pane(1))).toEqual(["a.ts"]);
  expect(fileTexts(pane(1))).toContain("first line");
});

test("restored file tabs drop when this window has not registered their folder, and quietly when their first read finds the file gone", async () => {
  const twoFiles = openFile(
    chatAndFile({ workspaceId: OTHER, ref: "x.ts" }),
    { workspaceId: W, ref: "gone.ts" },
    () => true,
  );
  if (!twoFiles) throw new Error("no file pane");
  const { code } = await mount({ stored: twoFiles, code: codeApi(["gone.ts"]) });
  await settled();

  expect(paneSections()).toHaveLength(0);
  expect(screen.queryByText("This file no longer exists.")).toBeNull();
  expect(code.file).toHaveBeenCalledWith(W, "gone.ts", expect.any(AbortSignal));
  expect(vi.mocked(code.file).mock.calls.some(([id]) => id === OTHER)).toBe(false);
  expect(parseLayout(window.sessionStorage.getItem(PANES_STORAGE_KEY))?.files).toEqual({});
});

test("an Edit in one pane holds the file's editor; a pane on the same file names it, and Show editor focuses it", async () => {
  vi.spyOn(navigator, "platform", "get").mockReturnValue("Win32");
  // Pane 2 holds f1; Pane 3, below it, holds f2: a second view of the same file.
  await mount({ stored: splitFile(chatAndFile(), "f1", "bottom") });
  expect(paneSections()).toHaveLength(3);

  await press(within(pane(1)).getByRole("button", { name: "Edit" }));
  expect(editorOwner(W, "src/a.ts")).toBe("f1");
  expect(within(pane(2)).getByText("Editing in Pane 2.")).toBeTruthy();

  await press(within(pane(2)).getByRole("button", { name: "Show editor" }));

  expect(within(pane(1)).getByRole("tab", { selected: true }).id).toBe("session-tab-f1");
  expect(within(pane(2)).queryByRole("tab", { selected: true })).toBeNull();
});

test("opening a file tab saves the layout; a file pane's focus alone does not", async () => {
  const { shell } = await mount({
    stored: chatAlone(),
    settings: withPanesKey(settingsFixture({ "onboarding.completed": true })),
  });
  await openTree();
  stubRoom(1000);
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });

  await click(treeFile("a.ts"));
  await advance(1000);

  const saves = layoutPatches(shell);
  expect(saves).toHaveLength(1);
  const changes = saves[0]?.args[2] as Record<string, unknown> | undefined;
  expect(Object.keys(changes ?? {})).toEqual(["layout.panes"]);
  expect(parseLayout(changes?.["layout.panes"])?.files).toEqual({
    f1: { workspaceId: W, ref: "a.ts" },
  });

  await pointerDown(pane(0));
  await advance(1000);
  await pointerDown(pane(1));
  await advance(1000);
  expect(layoutPatches(shell)).toHaveLength(1);
});

test("a file tab shows the unsaved-changes dot while its file has a code buffer", async () => {
  await mount({ stored: chatAndFile() });
  const tab = () => within(pane(1)).getByRole("tab");
  expect(within(tab()).queryByRole("img", { name: "Unsaved changes" })).toBeNull();

  act(() =>
    putBuffer({
      workspaceId: W,
      ref: "src/a.ts",
      base: { text: TEXT, sha256: "a".repeat(64), encoding: "utf-8", newline: "lf" },
      text: "edited",
    }),
  );
  expect(within(tab()).getByRole("img", { name: "Unsaved changes" })).toBeTruthy();

  act(() => dropBuffer(W, "src/a.ts"));
  expect(within(tab()).queryByRole("img", { name: "Unsaved changes" })).toBeNull();
});

test("a file pane reads its file again only when its folder's registration changes", async () => {
  const { code, viki, shell } = await mount({ stored: chatAndFile() });
  expect(code.file).toHaveBeenCalledTimes(1);
  const rerender = async () => {
    await act(async () => {
      viki.store.setStatus("reconnecting");
      viki.store.setStatus("open");
    });
    await settled();
  };

  await rerender();
  expect(code.file).toHaveBeenCalledTimes(1);

  const registered = shell.data.workspaces.workspaces.find((row) => row.workspace_id === W);
  if (!registered) throw new Error("missing code folder");
  registered.root = "D:\\Moved\\Alpha";
  await rerender();

  expect(code.file).toHaveBeenCalledTimes(2);
  expect(fileTexts(pane(1))).toContain("first line");
});

test("file tabs show no folder, except that two of one name in a strip show their parent directories", async () => {
  await mount({ stored: withFile(withFile(chatAndFile(), "lib/a.ts"), "b.ts") });

  const tabs = within(pane(1)).getAllByRole("tab");
  expect(tabNames(pane(1))).toEqual(["a.ts", "a.ts", "b.ts"]);
  expect(tabs.map((tab) => tab.querySelector(".root-label")?.textContent ?? null)).toEqual([
    "src",
    "lib",
    null,
  ]);
  expect(tabs.map((tab) => tab.querySelector(".session-tab-label")?.getAttribute("title"))).toEqual(
    ["src/a.ts", "lib/a.ts", "b.ts"],
  );
});

test("a file pane's preview closes its tab with Close file and offers no Back to chat", async () => {
  await mount({ stored: chatAndFile() });

  expect(within(pane(1)).queryByRole("button", { name: "Back to chat" })).toBeNull();
  expect(within(pane(1)).queryByRole("button", { name: "Close code preview" })).toBeNull();
  await click(within(pane(1)).getByRole("button", { name: "Close file" }));

  expect(paneSections()).toHaveLength(0);
  expect(document.getElementById("session-tab-f1")).toBeNull();
});

test("a file pane restored at start, or remounted by a layout change, leaves the keyboard focus alone", async () => {
  // Pane 2 holds a.ts; Pane 3, below it, b.ts.
  await mount({
    stored: moveTab(withFile(chatAndFile(), "src/b.ts"), "f2", { group: "g2", zone: "bottom" }),
  });
  expect(paneSections()).toHaveLength(3);
  expect(pane(1).contains(document.activeElement)).toBe(false);
  expect(pane(2).contains(document.activeElement)).toBe(false);

  // Pane 3 closes, so Pane 2 moves up out of their split: a new pane box with a new preview.
  await click(screen.getByRole("button", { name: "Close: b.ts" }));

  expect(paneSections()).toHaveLength(2);
  expect(fileTexts(pane(1))).toContain("first line");
  expect(pane(1).contains(document.activeElement)).toBe(false);
});

test("a file the Code tree or a drop opens takes the keyboard focus; the same preview mounting again, or a tab chosen in its pane, does not", async () => {
  await mount({ stored: chatAlone() });
  await openTree();
  stubRoom(1000);

  await click(treeFile("a.ts"));
  expect(document.activeElement).toBe(within(pane(1)).getByRole("heading", { name: "a.ts" }));

  // Too narrow for both panes, then wide again: the panes leave the screen and come back.
  act(() => (document.activeElement as HTMLElement).blur());
  stubRoom(500);
  expect(paneSections()).toHaveLength(0);
  stubRoom(1000);
  await settled();
  expect(fileTexts(pane(1))).toContain("first line");
  expect(pane(1).contains(document.activeElement)).toBe(false);

  // Two file floors are wider than this pane, so the drop lands in its middle, as a second tab.
  stubRect(pane(1), 600, 800);
  await dropFile(pane(1), { workspace_id: W, ref: "b.ts" });
  expect(tabNames(pane(1))).toEqual(["a.ts", "b.ts"]);
  expect(document.activeElement).toBe(within(pane(1)).getByRole("heading", { name: "b.ts" }));

  await click(document.getElementById("session-tab-f1") as HTMLElement);
  expect(within(pane(1)).getByRole("heading", { name: "a.ts" })).not.toBe(document.activeElement);
});

test("narrower than the panes need, choosing a file tab opens the middle preview and Alt+W and Ctrl+\\ stay on the chat shown", async () => {
  stubWidth(1000);
  // [Second, Chat (active) | a.ts], the chat pane focused.
  const stored = focusGroup(
    withFile(openTab(openTab(singlePane(), CHAT2), CHAT), "src/a.ts"),
    "g1",
  );
  await mount({ stored, chats: [CHAT, CHAT2] });
  expect(paneSections()).toHaveLength(0);

  await click(document.getElementById("session-tab-f1") as HTMLElement);
  expect(centerPane()?.textContent).toContain("first line");

  const before = storedLayout();
  key({ key: "\\", code: "Backslash", ctrlKey: true });
  expect(storedLayout()).toBe(before);
  expect(key({ key: "w", code: "KeyW", altKey: true })).toBe(false);
  await settled();

  expect(document.getElementById(`session-tab-${CHAT}`)).toBeNull();
  expect(document.getElementById(`session-tab-${CHAT2}`)).not.toBeNull();
  expect(document.getElementById("session-tab-f1")).not.toBeNull();
});

/** The chat, then a file pane holding a.ts and b.ts, b.ts active and focused. */
const chatAndTwoFiles = () => withFile(chatAndFile({ workspaceId: W, ref: "a.ts" }), "b.ts");

test("a file tab dragged onto a chat pane's edge splits off there; onto a file pane's middle it moves in", async () => {
  await mount({ stored: chatAndTwoFiles() });
  stubRect(pane(0), 1000, 800);

  await dropTab(pane(0), "f2", 990, 400);
  expect(paneSections().map(tabNames)).toEqual([["Chat"], ["b.ts"], ["a.ts"]]);

  // Too narrow for a chat-sized split beside it, so the middle: a.ts joins b.ts's pane.
  stubRect(pane(1), 600, 800);
  await dropTab(pane(1), "f1", 300, 400);
  expect(paneSections().map(tabNames)).toEqual([["Chat"], ["b.ts", "a.ts"]]);
});

test("only this window's own file tabs move: any other tab id dropped on a pane or its strip changes nothing", async () => {
  await mount({ stored: chatAndTwoFiles() });
  stubRect(pane(0), 1000, 800);
  const before = storedLayout();

  for (const id of ["constructor", "__proto__", "f0", "f65", "f1 ", "f9"])
    await dropTab(pane(0), id, 990, 400);
  expect(storedLayout()).toBe(before);

  // The strip's own drop hands App whatever id the drag carried.
  for (const id of ["constructor", "__proto__", "f9"]) {
    await act(async () => {
      fireEvent.drop(within(pane(1)).getByRole("tablist", { name: "Pane 2 tabs" }), {
        dataTransfer: { getData: (type: string) => (type === CHAT_DRAG_TYPE ? id : "") },
      });
    });
    await settled();
  }
  expect(storedLayout()).toBe(before);
  expect(paneSections().map(tabNames)).toEqual([["Chat"], ["a.ts", "b.ts"]]);
});

test("narrower than the panes need, a file tab dropped on the one pane changes nothing", async () => {
  stubWidth(1000);
  await mount({ stored: focusGroup(chatAndTwoFiles(), "g1") });
  const main = document.querySelector("main.conversation") as HTMLElement;
  stubRect(main, 1000, 800);
  const before = storedLayout();

  await dropTab(main, "f2", 500, 400);

  expect(storedLayout()).toBe(before);
});

test("a closed file tab takes its request along: the Git chip reopening that file in the same tab id shows its diff", async () => {
  await mount({ stored: chatAlone() });
  await openTree();
  stubRoom(1000);
  await click(treeFile("a.ts"));
  expect(within(pane(1)).queryByText("+changed line")).toBeNull();
  await click(screen.getByRole("button", { name: "Close: a.ts" }));
  expect(paneSections()).toHaveLength(0);

  await click(document.querySelector(".git-chip") as HTMLElement);
  await click(
    within(screen.getByRole("dialog", { name: "Git" })).getByRole("button", { name: ".M a.ts" }),
  );

  expect(document.getElementById("session-tab-f1")).not.toBeNull();
  expect(within(pane(1)).getByText("+changed line")).toBeTruthy();
});

test("narrower than the panes need, Ctrl+2 onto the file pane is ignored, so Alt+W still closes the chat on screen", async () => {
  stubWidth(1000);
  // [Second, Chat (active) | a.ts], the chat pane focused.
  const stored = focusGroup(
    withFile(openTab(openTab(singlePane(), CHAT2), CHAT), "src/a.ts"),
    "g1",
  );
  await mount({ stored, chats: [CHAT, CHAT2] });
  expect(paneSections()).toHaveLength(0);

  // Pane 2 in tree order is the file pane: Ctrl+2 must not land the focus there.
  const before = storedLayout();
  expect(key({ key: "2", code: "Digit2", ctrlKey: true })).toBe(true);
  expect(storedLayout()).toBe(before);

  expect(key({ key: "w", code: "KeyW", altKey: true })).toBe(false);
  await settled();

  expect(document.getElementById(`session-tab-${CHAT}`)).toBeNull();
  expect(document.getElementById(`session-tab-${CHAT2}`)).not.toBeNull();
  expect(document.getElementById("session-tab-f1")).not.toBeNull();
});

test("narrower than the panes need, moving a file tab to another pane still ends with the focus off any file pane, so Alt+W closes the chat on screen", async () => {
  stubWidth(1000);
  // g1 = chat [Second, Chat] (Chat active); g2 = file [a.ts]; g3 = file [b.ts]; chat pane focused.
  const chats = openTab(openTab(singlePane(), CHAT2), CHAT);
  const twoFilePanes = moveTab(withFile(withFile(chats, "a.ts"), "b.ts"), "f2", {
    group: "g2",
    zone: "bottom",
  });
  const stored = focusGroup(twoFilePanes, "g1");
  const { shell } = await mount({
    stored,
    chats: [CHAT, CHAT2],
    settings: withPanesKey(settingsFixture({ "onboarding.completed": true })),
  });
  // A closed Radix menu hands the focus back to its trigger on a timer; faked, as menuOf tests do.
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Tab actions: a.ts" }));
  });
  await click(screen.getByRole("menuitem", { name: "Move to pane 3" }));
  await advance(1000);

  // The move is a structural change and saves; the fix riding along with it means that save
  // never names a file pane as focused.
  const saves = layoutPatches(shell);
  expect(saves).toHaveLength(1);
  const changes = saves[0]?.args[2] as Record<string, unknown> | undefined;
  expect(parseLayout(changes?.["layout.panes"])?.focused).toBe("g1");

  expect(key({ key: "w", code: "KeyW", altKey: true })).toBe(false);
  await settled();

  expect(document.getElementById(`session-tab-${CHAT}`)).toBeNull();
  expect(document.getElementById(`session-tab-${CHAT2}`)).not.toBeNull();
  expect(document.getElementById("session-tab-f1")).not.toBeNull();
  expect(document.getElementById("session-tab-f2")).not.toBeNull();
});

test("a file pane focused at a wide width, then the window narrows, then Alt+W closes the chat on screen and the file tab stays", async () => {
  // [Second, Chat (active) | a.ts], the file pane focused (opening a file focuses its own pane).
  const stored = withFile(openTab(openTab(singlePane(), CHAT2), CHAT), "src/a.ts");
  const { shell } = await mount({
    stored,
    chats: [CHAT, CHAT2],
    settings: withPanesKey(settingsFixture({ "onboarding.completed": true })),
  });
  expect(paneSections()).toHaveLength(2);
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });

  act(() => stubWidth(1000)); // the window narrows below the right-rail breakpoint
  await settled();
  expect(paneSections()).toHaveLength(0);

  await advance(1000);
  expect(layoutPatches(shell)).toHaveLength(0); // the invariant fix alone never saves

  expect(key({ key: "w", code: "KeyW", altKey: true })).toBe(false);
  await settled();

  expect(document.getElementById(`session-tab-${CHAT}`)).toBeNull();
  expect(document.getElementById(`session-tab-${CHAT2}`)).not.toBeNull();
  expect(document.getElementById("session-tab-f1")).not.toBeNull();
});
