import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki } from "../../accept/m3b/fakes";
import {
  type CodeApi,
  CodeApiError,
  type CodeChanges,
  type CodeSaveResult,
} from "../../clients/code/codeApi";
import type { EditorHandle, EditorOptions } from "../../components/Workbench/codemirror";
import { dirtyRefs, dropBuffer, getBuffer } from "../../state/codeBuffers";
import { hasUnsavedWork } from "../../state/unsavedWork";
import type { VaultApi, VaultRecoveryStatus } from "../../state/vaultApi";
import type { VaultOpsApi } from "../../state/vaultOpsApi";
import { fakeDurable } from "../../testDurable";
import {
  capabilitiesFixture,
  fakeShellApi,
  listingFixture,
  settingsFixture,
  workspaceFixture,
} from "../../testShell";

// Keep CodePreview and CodeEditor real. Only replace their lazy, layout-dependent editor facade.
const loader = vi.hoisted(() => ({ create: vi.fn() }));
vi.mock("../../components/Workbench/codemirror", () => ({
  createEditor: loader.create,
  codeTokens: async () => [],
}));

const A = "55555555-5555-4555-8555-555555555555";
const B = "66666666-6666-4666-8666-666666666666";
const BASE = "const value = 1;\n";
const HASH = "a".repeat(64);
const alpha = workspaceFixture({
  workspace_id: A,
  kind: "code",
  name: "Alpha",
  root: "D:\\Code\\Alpha",
  root_physical_id: "12:34",
  policy_revision: 3,
});
const beta = { ...alpha, workspace_id: B, name: "Beta", root: "D:\\Code\\Beta" };

function fakeEditor(options: EditorOptions): EditorHandle {
  const area = document.createElement("textarea");
  area.setAttribute("aria-label", options.label);
  area.value = options.text;
  let base = options.base;
  let reported = false;
  const report = () => {
    const dirty = area.value !== base;
    if (dirty === reported) return;
    reported = dirty;
    options.onDirty(dirty);
  };
  const changed = () => {
    report();
    options.onChange?.(area.value);
  };
  area.addEventListener("input", changed);
  options.parent.append(area);
  report();
  return {
    text: () => area.value,
    setText: (text) => {
      area.value = text;
      changed();
    },
    setBase: (text) => {
      base = text;
      report();
    },
    setWrap: vi.fn(),
    setFontSize: vi.fn(),
    focus: () => area.focus(),
    destroy: () => area.remove(),
  };
}

beforeEach(() => {
  sessionStorage.clear();
  vi.spyOn(navigator, "platform", "get").mockReturnValue("Win32");
  loader.create.mockReset();
  loader.create.mockImplementation(async (options: EditorOptions) => fakeEditor(options));
});
afterEach(() => {
  cleanup();
  for (const id of [A, B]) for (const ref of dirtyRefs(id)) dropBuffer(id, ref);
  sessionStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const settled = () => act(async () => {});
async function click(element: HTMLElement) {
  await act(async () => {
    element.focus();
    fireEvent.click(element);
  });
  await settled();
}
const content = () => screen.getByRole("region", { name: "Code content" });
const sidebar = () => screen.getByRole("region", { name: "Code sidebar" });
const area = () => screen.getByRole("textbox", { name: "Edit a.ts" }) as HTMLTextAreaElement;
const composer = () => {
  const box = document.querySelector<HTMLTextAreaElement>('textarea[aria-label="Message"]');
  if (!box) throw new Error("missing retained composer");
  return box;
};
function unload() {
  const event = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(event);
  return event.defaultPrevented;
}

async function mount({ narrow = false, recovery = false } = {}) {
  if (narrow)
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: false,
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
    }));
  const fake = fakeViki();
  const durable = fakeDurable();
  const shell = fakeShellApi({
    settings: settingsFixture({ "onboarding.completed": true }),
    capabilities: capabilitiesFixture({
      workspace: {
        shared_scratch: false,
        chat_only: false,
        active: {
          id: A,
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
    workspaces: { ...listingFixture([{ ...alpha }, { ...beta }]), active_id: A, chat_only: false },
  });
  let disk = BASE;
  const code: CodeApi = {
    tree: vi.fn(async (_id, path) => ({
      path,
      entries: ["a.ts", "b.ts"].map((name) => ({ name, type: "file" as const, size: 17 })),
      truncated: false,
    })),
    file: vi.fn(async (_id, path) => ({
      path,
      text: disk,
      sha256: disk === BASE ? HASH : "b".repeat(64),
      size: disk.length,
      encoding: "utf-8" as const,
      newline: "lf" as const,
      reason: null,
    })),
    save: vi.fn(async (_id, _path, body) => {
      disk = body.content;
      return {
        operation_id: "saved",
        status: "saved" as const,
        sha256: "b".repeat(64),
        cleanup_pending: false,
      };
    }),
    search: vi.fn(),
    // The status bar's Git chip reads `changes` for a chat in a code folder; these tests are not about it.
    changes: vi.fn(() => new Promise<CodeChanges>(() => {})),
    diff: vi.fn(),
  };
  const list = vi.fn<VaultApi["recovery"]>().mockResolvedValue([]);
  const resolve = vi.fn<VaultOpsApi["resolve"]>();
  const pending = () => new Promise<never>(() => {});
  const vault: VaultApi = {
    entries: pending,
    note: pending,
    backlinks: pending,
    history: pending,
    bookmarks: pending,
    addBookmark: pending,
    removeBookmark: pending,
    createNote: pending,
    saveNote: pending,
    recovery: list,
  };
  const ops: VaultOpsApi = {
    preview: pending,
    apply: pending,
    recovery: pending,
    forgetOperation: pending,
    resolve,
  };
  const props = {
    viki: fake.viki,
    rest: fakeRest(),
    durable,
    shell,
    codeApi: code,
    ...(recovery ? { vaultApi: vault, vaultOpsApi: ops } : {}),
  };
  let rendered!: ReturnType<typeof render>;
  await act(async () => {
    rendered = render(<App {...props} />);
    fake.socket.open();
  });
  await settled();
  fake.socket.sent.length = 0;
  return { ...rendered, ...fake, code, shell, props, durable, list, resolve };
}

async function openFile() {
  await click(
    within(screen.getByRole("tablist", { name: "Right sidebar" })).getByRole("tab", {
      name: "Code",
    }),
  );
  await click(within(sidebar()).getByRole("button", { name: "a.ts" }));
}
async function editFile() {
  await openFile();
  await click(within(content()).getByRole("button", { name: "Edit" }));
  return area();
}

test("center Edit stays beside the explorer and keeps chat/draft", async () => {
  const t = await mount();
  await click(screen.getByRole("button", { name: "New session" }));
  const selected = sessionStorage.getItem("viki.selected");
  const chat = t.container.querySelector('[data-region="conversation"]');
  const box = composer();
  fireEvent.change(box, { target: { value: "keep this chat draft" } });
  t.socket.sent.length = 0;
  await openFile();
  expect(loader.create).not.toHaveBeenCalled();
  expect(t.code.save).not.toHaveBeenCalled();
  const explorer = sidebar();
  await click(within(content()).getByRole("button", { name: "Edit" }));
  const editor = area();
  expect(t.container.querySelector('[data-region="center-pane"]')?.contains(editor)).toBe(true);
  expect(sidebar()).toBe(explorer);
  expect(explorer.contains(editor)).toBe(false);
  expect(t.container.querySelector('[data-region="conversation"]')).toBe(chat);
  expect(composer()).toBe(box);
  expect(box.value).toBe("keep this chat draft");
  fireEvent.input(editor, { target: { value: "const value = 2;\n" } });
  await click(within(content()).getByRole("button", { name: "Save" }));
  expect(t.code.save).toHaveBeenCalledExactlyOnceWith(A, "a.ts", {
    policy_revision: 3,
    root_physical_id: "12:34",
    content: "const value = 2;\n",
    expected_sha256: HASH,
    encoding: "utf-8",
    newline: "lf",
  });
  await click(within(content()).getByRole("button", { name: "Close" }));
  expect(screen.queryByRole("textbox", { name: "Edit a.ts" })).toBeNull();
  expect([...content().querySelectorAll(".code-text")].map((line) => line.textContent)).toEqual([
    "const value = 2;",
  ]);
  expect(t.code.file).toHaveBeenCalledTimes(2);
  expect(document.activeElement).toBe(within(content()).getByRole("button", { name: "Edit" }));
  await click(within(content()).getByRole("button", { name: "Back to chat" }));
  expect(screen.getByRole("textbox", { name: "Message" })).toBe(box);
  expect(box.value).toBe("keep this chat draft");
  expect(sessionStorage.getItem("viki.selected")).toBe(selected);
  expect(t.socket.sent).toEqual([]);
  expect(t.durable.submissions).toEqual([]);
  expect(t.shell.calls.filter((call) => call.method === "selectWorkspace")).toEqual([]);
});

test.each(["MacIntel", "Linux x86_64"])("center Edit is offered on %s", async (platform) => {
  vi.spyOn(navigator, "platform", "get").mockReturnValue(platform);
  await mount();
  await openFile();
  const edit = within(content()).getByRole("button", { name: "Edit" });
  expect(edit.getAttribute("aria-disabled")).toBe("false");
  expect(within(content()).queryByText("Editing works on Windows in this version.")).toBeNull();
  await click(edit);
  expect(loader.create).toHaveBeenCalled();
});

test("center editing receives the existing memoized stopped-save recovery client", async () => {
  const t = await mount({ recovery: true });
  await openFile();
  expect(t.list).not.toHaveBeenCalled();
  await click(within(content()).getByRole("button", { name: "Edit" }));
  expect(t.list).toHaveBeenCalledTimes(1);
  fireEvent.input(area(), { target: { value: "dirty code" } });
  expect(t.list).toHaveBeenCalledTimes(1);
  const row: VaultRecoveryStatus = {
    operation_id: "77777777-7777-4777-8777-777777777777",
    workspace_id: A,
    ref: "a.ts",
    state: "prepared",
    reason: "recovery_required",
    resolvable: ["forget"],
  };
  t.list.mockResolvedValue([row, { ...row, workspace_id: B, ref: "other.ts" }]);
  vi.mocked(t.code.save).mockRejectedValueOnce(new CodeApiError("publication_busy"));
  await click(within(content()).getByRole("button", { name: "Save" }));
  expect(t.list).toHaveBeenCalledTimes(2);
  expect(within(content()).getByRole("heading", { name: "Stopped saves" })).toBeTruthy();
  expect(within(content()).queryByText("other.ts")).toBeNull();
  expect(area().value).toBe("dirty code");
  expect(t.resolve).not.toHaveBeenCalled();
});

test.each([
  "back",
  "close",
  "escape",
  "refresh",
  "file",
  "folder",
  "navigation",
  "session",
  "api",
  "registration",
  "unmount",
] as const)(
  "dirty center editing survives %s without a leave prompt and resumes only after explicit Edit",
  async (exit) => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const t = await mount();
    expect(unload()).toBe(false);
    const editor = await editFile();
    fireEvent.input(editor, { target: { value: "dirty first" } });
    editor.value = "latest retained code";
    expect(unload()).toBe(true);
    if (exit === "back" || exit === "close") {
      await click(
        within(content()).getByRole("button", {
          name: exit === "back" ? "Back to chat" : "Close code preview",
        }),
      );
    } else if (exit === "escape") {
      const close = within(content()).getByRole("button", { name: "Close code preview" });
      close.focus();
      await act(async () => fireEvent.keyDown(close, { key: "Escape" }));
    } else if (exit === "refresh") {
      await click(within(sidebar()).getByRole("button", { name: "Refresh" }));
    } else if (exit === "file") {
      await click(within(sidebar()).getByRole("button", { name: "b.ts" }));
    } else if (exit === "folder") {
      fireEvent.change(screen.getByLabelText("Code folder"), { target: { value: B } });
      await settled();
    } else if (exit === "navigation") {
      await act(async () => fireEvent.keyDown(window, { key: ",", ctrlKey: true }));
      await click(screen.getByRole("button", { name: "Back" }));
    } else if (exit === "session") {
      await click(screen.getByRole("button", { name: "New session" }));
    } else if (exit === "api") {
      const { codeApi: _codeApi, ...without } = t.props;
      await act(async () => t.rerender(<App {...without} />));
      await act(async () => t.rerender(<App {...t.props} />));
    } else if (exit === "registration") {
      const folder = t.shell.data.workspaces.workspaces[0];
      if (!folder) throw new Error("missing registered folder");
      folder.policy_revision += 1;
      await act(async () => {
        t.viki.store.setStatus("reconnecting");
        t.viki.store.setStatus("open");
      });
      await settled();
      folder.policy_revision -= 1;
      await act(async () => {
        t.viki.store.setStatus("reconnecting");
        t.viki.store.setStatus("open");
      });
    } else {
      t.unmount();
      render(<App {...t.props} />);
    }
    await settled();
    expect(screen.queryByRole("textbox", { name: "Edit a.ts" })).toBeNull();
    expect(getBuffer(A, "a.ts")?.text).toBe("latest retained code");
    expect(hasUnsavedWork()).toBe(false);
    expect(confirm).not.toHaveBeenCalled();
    expect(unload()).toBe(true);
    await click(
      within(screen.getByRole("tablist", { name: "Right sidebar" })).getByRole("tab", {
        name: "Code",
      }),
    );
    fireEvent.change(screen.getByLabelText("Code folder"), { target: { value: A } });
    await settled();
    await click(
      within(screen.getByRole("group", { name: "Unsaved changes" })).getByRole("button", {
        name: "a.ts",
      }),
    );
    expect(screen.queryByRole("textbox", { name: "Edit a.ts" })).toBeNull();
    expect(within(content()).getByText(/Choose Edit to resume them/)).toBeTruthy();
    await click(within(content()).getByRole("button", { name: "Edit" }));
    expect(area().value).toBe("latest retained code");
    expect(t.code.save).not.toHaveBeenCalled();
    expect(t.durable.submissions).toEqual([]);
    expect(t.socket.sent.some((message) => message.type === "turn.send")).toBe(false);
    expect(t.shell.calls.filter((call) => call.method === "selectWorkspace")).toEqual([]);
  },
);

test("narrow selection edits in center and returns focus to the drawer opener", async () => {
  const t = await mount({ narrow: true });
  const box = composer();
  fireEvent.change(box, { target: { value: "narrow chat draft" } });
  await click(screen.getByRole("button", { name: "VIKI panel" }));
  await editFile();
  expect(screen.queryByRole("dialog", { name: "VIKI panel" })).toBeNull();
  expect(document.activeElement).toBe(area());
  fireEvent.input(area(), { target: { value: "narrow retained code" } });
  await click(within(content()).getByRole("button", { name: "Back to chat" }));
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "VIKI panel" }));
  expect(composer()).toBe(box);
  expect(box.value).toBe("narrow chat draft");
  expect(getBuffer(A, "a.ts")?.text).toBe("narrow retained code");
  expect(t.code.save).not.toHaveBeenCalled();
});

test("old Save preserves newest RAM text and unload protection in a still-dirty remount", async () => {
  const t = await mount();
  let resolve!: (result: CodeSaveResult) => void;
  vi.mocked(t.code.save).mockReturnValue(
    new Promise<CodeSaveResult>((done) => {
      resolve = done;
    }),
  );
  fireEvent.input(await editFile(), { target: { value: "sent" } });
  await click(within(content()).getByRole("button", { name: "Save" }));
  await click(within(sidebar()).getByRole("button", { name: "b.ts" }));
  await click(within(content()).getByRole("button", { name: "Edit" }));
  await click(
    within(screen.getByRole("group", { name: "Unsaved changes" })).getByRole("button", {
      name: "a.ts",
    }),
  );
  await click(within(content()).getByRole("button", { name: "Edit" }));
  expect(
    within(content()).getByRole("button", { name: "Close" }).getAttribute("aria-disabled"),
  ).toBe("true");
  fireEvent.input(area(), { target: { value: "newest" } });
  expect(unload()).toBe(true);
  await act(async () =>
    resolve({
      operation_id: "saved",
      status: "saved",
      sha256: "b".repeat(64),
      cleanup_pending: false,
    }),
  );
  expect(area().value).toBe("newest");
  expect(unload()).toBe(true);
  expect(getBuffer(A, "a.ts")).toEqual({
    workspaceId: A,
    ref: "a.ts",
    text: "newest",
    base: { text: "sent", sha256: "b".repeat(64), encoding: "utf-8", newline: "lf" },
  });
});
