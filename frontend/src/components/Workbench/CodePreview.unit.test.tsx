import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { WorkspaceJson } from "../../clients/app/shellApi";
import {
  type CodeApi,
  CodeApiError,
  type CodeDiff,
  type CodeFile,
} from "../../clients/code/codeApi";
import { dirtyRefs, dropBuffer } from "../../state/codeBuffers";
import { CodePreview, type CodePreviewProps } from "./CodePreview";
import {
  type CodeOpenRequest,
  CodeSidebar,
  type CodeSidebarProps,
  codeFolderKey,
} from "./CodeSidebar";
import type { EditorHandle, EditorOptions } from "./codemirror";

const loader = vi.hoisted(() => ({ create: vi.fn() }));
vi.mock("./codemirror", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./codemirror")>()),
  createEditor: loader.create,
}));

type HarnessProps = Pick<CodeSidebarProps, "api" | "folders" | "activeId"> &
  Pick<CodePreviewProps, "onQuote" | "onOpenInEditor" | "fontSize" | "lineWrap">;

const A = "00000000-0000-4000-8000-00000000000a";
const B = "00000000-0000-4000-8000-00000000000b";

beforeEach(() => {
  loader.create.mockReset();
  loader.create.mockImplementation(async (options: EditorOptions): Promise<EditorHandle> => {
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
      setFontSize: vi.fn(),
      setWrap: vi.fn(),
      focus: () => area.focus(),
      destroy: () => area.remove(),
    };
  });
});
afterEach(() => {
  cleanup();
  for (const id of [A, B]) for (const ref of dirtyRefs(id)) dropBuffer(id, ref);
});

const folder = (workspaceId: string, name: string, over: Partial<WorkspaceJson> = {}) =>
  ({
    workspace_id: workspaceId,
    kind: "code",
    root: `registered-${name}`,
    root_physical_id: `physical-${name}`,
    root_available: true,
    name,
    policy: "ordinary",
    policy_revision: 3,
    persistent: true,
    ...over,
  }) as WorkspaceJson;

const file = (
  path: string,
  text = "first\nsecond\nthird",
  over: Partial<CodeFile> = {},
): CodeFile => ({
  path,
  text,
  size: text.length,
  sha256: "a".repeat(64),
  encoding: "utf-8",
  newline: "lf",
  reason: null,
  ...over,
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function fakeApi(over: Partial<CodeApi> = {}): CodeApi {
  return {
    tree: vi.fn().mockResolvedValue({
      path: "",
      entries: [{ name: "a.ts", type: "file", size: 3 }],
      truncated: false,
    }),
    file: vi.fn().mockImplementation(async (_id: string, path: string) => file(path)),
    changes: vi.fn().mockResolvedValue({
      state: "ready",
      branch: "main",
      head: "abcdef123456",
      detached: false,
      upstream: null,
      ahead: null,
      behind: null,
      truncated: false,
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
    }),
    diff: vi.fn().mockImplementation(async (_id: string, path: string) => ({
      path,
      text: "@@ -1 +1 @@\n+current diff",
      reason: null,
    })),
    search: vi.fn(),
    save: vi.fn(),
    ...over,
  };
}

function setup(over: Partial<CodeApi> = {}, extra: Partial<HarnessProps> = {}) {
  const props: HarnessProps = {
    api: fakeApi(over),
    folders: [folder(A, "Alpha"), folder(B, "Beta")],
    activeId: A,
    onQuote: vi.fn(),
    onOpenInEditor: vi.fn(),
    fontSize: 13,
    lineWrap: false,
    ...extra,
  };
  const view = render(<PreviewHarness {...props} />);
  return { props, ...view };
}

function PreviewHarness(props: HarnessProps) {
  const [request, setRequest] = useState<CodeOpenRequest | null>(null);
  return (
    <>
      <CodeSidebar
        api={props.api}
        folders={props.folders}
        activeId={props.activeId}
        selected={request}
        onOpen={setRequest}
        onOpenWorkbench={() => {}}
      />
      {request ? (
        <CodePreview {...props} request={request} onClose={() => setRequest(null)} />
      ) : null}
    </>
  );
}

const settle = () => act(async () => {});
const selectFolder = (id: string) =>
  fireEvent.change(screen.getByRole("combobox", { name: "Code folder" }), {
    target: { value: id },
  });
const content = () => screen.getByRole("region", { name: "Code content" });
const fileTexts = (root: ParentNode = document.body) =>
  [...root.querySelectorAll(".code-text")].map((line) => line.textContent);
async function openFile() {
  await settle();
  fireEvent.click(screen.getByRole("button", { name: "a.ts" }));
  await settle();
}
async function openChange() {
  fireEvent.click(screen.getByRole("tab", { name: "Changes" }));
  await settle();
  fireEvent.click(screen.getByRole("button", { name: ".M a.ts" }));
  await settle();
}

test("only the selected registered code folder is read, on demand, without stealing focus", async () => {
  const { props, container } = setup(
    {},
    {
      activeId: B,
      folders: [folder(A, "Alpha"), folder(B, "Beta"), folder("vault", "Vault", { kind: "vault" })],
    },
  );
  await settle();
  expect((screen.getByLabelText("Code folder") as HTMLSelectElement).value).toBe(B);
  expect(screen.getAllByRole("option").map((option) => option.textContent)).toEqual([
    "Alpha",
    "Beta",
  ]);
  expect(props.api.tree).toHaveBeenCalledTimes(1);
  expect(vi.mocked(props.api.tree).mock.calls[0]?.slice(0, 2)).toEqual([B, ""]);
  for (const method of [
    props.api.file,
    props.api.changes,
    props.api.diff,
    props.api.search,
    props.api.save,
  ])
    expect(method).not.toHaveBeenCalled();
  expect(document.activeElement).toBe(document.body);
  expect(container.querySelector(".workbench")).toBeNull();
  expect(screen.queryByRole("button", { name: /^(Edit|Save|Terminal)$/ })).toBeNull();
});

test("empty folders and unavailable roots show the settings path without a read", async () => {
  const { props, rerender } = setup({}, { folders: [] });
  expect(screen.getByText("No code folders yet. Open one in Settings → Workspaces.")).toBeTruthy();
  rerender(<PreviewHarness {...props} folders={[folder(A, "Alpha", { root_available: false })]} />);
  await settle();
  expect(screen.getByRole("alert").textContent).toBe("The folder is not where it was registered.");
  expect(props.api.tree).not.toHaveBeenCalled();
});

test("exact nested refs and selected lines reach the existing quote and editor actions", async () => {
  const tree = vi.fn<CodeApi["tree"]>(async (_id, path) => ({
    path,
    entries:
      path === ""
        ? [{ name: "src", type: "dir", size: null }]
        : [{ name: "a b.ts", type: "file", size: 3 }],
    truncated: false,
  }));
  const { props } = setup({ tree });
  await settle();
  fireEvent.click(screen.getByRole("button", { name: "src" }));
  await settle();
  fireEvent.click(screen.getByRole("button", { name: "a b.ts" }));
  await settle();
  expect(within(content()).getByRole("heading", { name: "src/a b.ts" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Open in VS Code" }));
  expect(props.onOpenInEditor).toHaveBeenLastCalledWith(A, "src/a b.ts", null);
  fireEvent.click(within(content()).getByText("2"));
  fireEvent.click(within(content()).getByText("3"), { shiftKey: true });
  fireEvent.click(screen.getByRole("button", { name: "Quote in chat" }));
  expect(props.onQuote).toHaveBeenLastCalledWith({ workspaceId: A, text: "`src/a b.ts:2-3`" });
  fireEvent.click(screen.getByRole("button", { name: "Open in VS Code" }));
  expect(props.onOpenInEditor).toHaveBeenLastCalledWith(A, "src/a b.ts", 2);
});

test("inactive folders cannot quote and a changed active ID revokes quoting", async () => {
  const { props, rerender } = setup();
  await openFile();
  fireEvent.click(within(content()).getByText("2"));
  rerender(<PreviewHarness {...props} activeId={B} />);
  const quote = screen.getByRole("button", { name: "Quote in chat" });
  expect(quote.getAttribute("aria-disabled")).toBe("true");
  expect(quote.getAttribute("title")).toBe("Only files in the active code folder can be quoted");
  fireEvent.click(quote);
  expect(props.onQuote).not.toHaveBeenCalled();
});

test("changes launch file and diff reads in parallel and let both views be selected", async () => {
  const pendingFile = deferred<CodeFile>();
  const pendingDiff = deferred<CodeDiff>();
  const { props } = setup({
    file: vi.fn().mockReturnValue(pendingFile.promise),
    diff: vi.fn().mockReturnValue(pendingDiff.promise),
  });
  await openChange();
  expect(props.api.file).toHaveBeenCalledTimes(1);
  expect(props.api.diff).toHaveBeenCalledTimes(1);
  expect(within(content()).getByRole("status").textContent).toBe("Loading file…");
  await act(async () => {
    pendingFile.resolve(file("a.ts"));
    pendingDiff.resolve({ path: "a.ts", text: "+changed", reason: null });
  });
  expect(within(content()).getByText("+changed")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "File" }));
  expect(within(content()).getByText("first")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Diff" }));
  expect(within(content()).getByText("+changed")).toBeTruthy();
});

test("test_old_folder_reply_cannot_replace_current_file_or_diff", async () => {
  const oldFile = deferred<CodeFile>();
  const oldDiff = deferred<CodeDiff>();
  const read = vi.fn<CodeApi["file"]>((id, path) =>
    id === A ? oldFile.promise : Promise.resolve(file(path, "Beta file")),
  );
  const diff = vi.fn<CodeApi["diff"]>((id, path) =>
    id === A ? oldDiff.promise : Promise.resolve({ path, text: "+Beta diff", reason: null }),
  );
  setup({ file: read, diff });
  await openChange();
  selectFolder(B);
  await settle();
  fireEvent.click(screen.getByRole("button", { name: ".M a.ts" }));
  await settle();
  expect(read.mock.calls[0]?.[2]?.aborted).toBe(true);
  expect(diff.mock.calls[0]?.[2]?.aborted).toBe(true);
  expect(within(content()).getByText("+Beta diff")).toBeTruthy();
  await act(async () => {
    oldFile.resolve(file("a.ts", "Alpha stale file"));
    oldDiff.resolve({ path: "a.ts", text: "+Alpha stale diff", reason: null });
  });
  expect(within(content()).getByText("+Beta diff")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "File" }));
  expect(fileTexts(content())).toContain("Beta file");
  expect(fileTexts()).not.toContain("Alpha stale file");
  expect(screen.queryByText("+Alpha stale diff")).toBeNull();
});

test("API replacement cancels old file and diff reads even when their promises ignore abort", async () => {
  const oldFile = deferred<CodeFile>(),
    oldDiff = deferred<CodeDiff>();
  const { props, rerender } = setup({
    file: vi.fn().mockReturnValue(oldFile.promise),
    diff: vi.fn().mockReturnValue(oldDiff.promise),
  });
  await openChange();
  const replacement = fakeApi({
    file: vi.fn().mockResolvedValue(file("a.ts", "new API file")),
    diff: vi.fn().mockResolvedValue({ path: "a.ts", text: "+new API diff", reason: null }),
  });
  rerender(<PreviewHarness {...props} api={replacement} />);
  expect(vi.mocked(props.api.file).mock.calls[0]?.[2]?.aborted).toBe(true);
  expect(vi.mocked(props.api.diff).mock.calls[0]?.[2]?.aborted).toBe(true);
  expect(within(content()).getByRole("alert").textContent).toBe(
    "This preview is no longer available. Choose the file again.",
  );
  await openChange();
  expect(within(content()).getByText("+new API diff")).toBeTruthy();
  await act(async () => {
    oldFile.resolve(file("a.ts", "old API file"));
    oldDiff.resolve({ path: "a.ts", text: "+old API diff", reason: null });
  });
  expect(within(content()).getByText("+new API diff")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "File" }));
  expect(fileTexts(content())).toContain("new API file");
  expect(fileTexts()).not.toContain("old API file");
  expect(screen.queryByText("+old API diff")).toBeNull();
});

test("API replacement immediately removes already visible content and quote controls", async () => {
  const { props, rerender } = setup({
    file: vi.fn().mockResolvedValue(file("a.ts", "prior client content")),
  });
  await openFile();
  expect(fileTexts(content())).toContain("prior client content");
  rerender(
    <PreviewHarness
      {...props}
      api={fakeApi({ file: vi.fn().mockResolvedValue(file("a.ts", "replacement content")) })}
    />,
  );
  expect(fileTexts()).not.toContain("prior client content");
  expect(screen.queryByRole("button", { name: "Quote in chat" })).toBeNull();
  await openFile();
  expect(fileTexts(content())).toContain("replacement content");
});

test("a later ref wins even when a cancelled read returns, and unmount aborts reads", async () => {
  const old = deferred<CodeFile>();
  const read = vi.fn<CodeApi["file"]>((_id, path) =>
    path === "a.ts" ? old.promise : Promise.resolve(file(path, "second ref")),
  );
  const { unmount } = setup({
    file: read,
    tree: vi.fn().mockResolvedValue({
      path: "",
      entries: [
        { name: "a.ts", type: "file", size: 3 },
        { name: "b.ts", type: "file", size: 3 },
      ],
      truncated: false,
    }),
  });
  await openFile();
  fireEvent.click(screen.getByRole("button", { name: "b.ts" }));
  await settle();
  expect(read.mock.calls[0]?.[2]?.aborted).toBe(true);
  await act(async () => old.resolve(file("a.ts", "stale ref")));
  expect(fileTexts(content())).toContain("second ref");
  expect(fileTexts()).not.toContain("stale ref");
  unmount();
  expect(read.mock.calls[1]?.[2]?.aborted).toBe(true);
});

test("removal and changed registration identity drop content and cancel old results", async () => {
  const pending = deferred<CodeFile>();
  const { props, rerender } = setup({ file: vi.fn().mockReturnValue(pending.promise) });
  await openFile();
  rerender(
    <PreviewHarness
      {...props}
      folders={[folder(A, "Alpha", { root_physical_id: "replacement", policy_revision: 4 })]}
    />,
  );
  await settle();
  expect(vi.mocked(props.api.file).mock.calls[0]?.[2]?.aborted).toBe(true);
  await act(async () => pending.resolve(file("a.ts", "old registration")));
  expect(fileTexts()).not.toContain("old registration");
  rerender(<PreviewHarness {...props} folders={[]} />);
  expect(screen.queryByRole("button", { name: "Quote in chat" })).toBeNull();
  expect(within(content()).getByRole("alert").textContent).toBe(
    "This preview is no longer available. Choose the file again.",
  );
});

test("refresh explicitly reloads the visible list and selected file, without polling", async () => {
  const { props } = setup();
  await openFile();
  expect(props.api.file).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  await settle();
  expect(props.api.tree).toHaveBeenCalledTimes(2);
  expect(props.api.file).toHaveBeenCalledTimes(2);
  expect(props.api.diff).not.toHaveBeenCalled();
  expect(vi.mocked(props.api.file).mock.calls[0]?.[2]?.aborted).toBe(true);
  expect(props.api.save).not.toHaveBeenCalled();
});

test("deleted files retain a readable diff but private refusals do not show it", async () => {
  const read = vi.fn().mockRejectedValue(new CodeApiError("not_found"));
  const { props, rerender } = setup({ file: read });
  await openChange();
  expect(within(content()).getByText("+current diff")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Quote in chat" })).toBeNull();
  read.mockRejectedValue(new CodeApiError("private_path"));
  rerender(<PreviewHarness {...props} folders={[folder(B, "Beta")]} />);
  await settle();
  fireEvent.click(screen.getByRole("button", { name: ".M a.ts" }));
  await settle();
  expect(within(content()).getByRole("alert").textContent).toBe(
    "This file is private and is not shown.",
  );
  expect(screen.queryByText("+current diff")).toBeNull();
});

test.each(["binary", "too_large", "unsupported_encoding"] as const)(
  "file refusal %s remains read-only",
  async (reason) => {
    const { props } = setup({
      file: vi.fn().mockResolvedValue(file("a.ts", "", { text: null, reason })),
    });
    await openFile();
    expect(within(content()).queryByRole("listbox")).toBeNull();
    expect(within(content()).getByText(/not shown/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Quote in chat" }));
    expect(props.onQuote).not.toHaveBeenCalled();
    expect(props.api.save).not.toHaveBeenCalled();
  },
);

test("trust and no-diff refusals retain the existing viewer wording", async () => {
  const { props, rerender } = setup(
    { changes: vi.fn().mockRejectedValue(new CodeApiError("trust_required")) },
    { folders: [folder(A, "Alpha", { policy: "existing_readonly" })] },
  );
  fireEvent.click(screen.getByRole("tab", { name: "Changes" }));
  await settle();
  expect(
    screen.getByText("Trust this folder in Settings → Workspaces to see its Git changes."),
  ).toBeTruthy();
  const api = fakeApi({
    diff: vi.fn().mockResolvedValue({ path: "a.ts", text: null, reason: "no_diff" }),
  });
  rerender(<PreviewHarness {...props} api={api} folders={[folder(A, "Alpha")]} />);
  await openChange();
  fireEvent.click(screen.getByRole("button", { name: "Diff" }));
  expect(screen.getByText("No changes against HEAD.")).toBeTruthy();
});

test("private changes cannot be opened and mismatched response refs are refused", async () => {
  const api = fakeApi();
  const changes = await api.changes(A);
  const entry = changes.entries[0];
  if (!entry) throw new Error("missing synthetic change");
  entry.private = true;
  const { props, rerender } = setup({ changes: vi.fn().mockResolvedValue(changes) });
  fireEvent.click(screen.getByRole("tab", { name: "Changes" }));
  await settle();
  expect(screen.getByText("Private")).toBeTruthy();
  expect(screen.queryByRole("button", { name: ".M a.ts" })).toBeNull();
  expect(props.api.file).not.toHaveBeenCalled();
  const mismatched = fakeApi({
    file: vi.fn().mockResolvedValue(file("different.ts", "wrong ref")),
  });
  rerender(<PreviewHarness {...props} api={mismatched} />);
  fireEvent.click(screen.getByRole("tab", { name: "Files" }));
  await openFile();
  expect(within(content()).getByRole("alert").textContent).toBe("This file could not be opened.");
  expect(fileTexts()).not.toContain("wrong ref");
});

test("tabs use arrows, Home and End; file keyboard selection and display settings stay usable", async () => {
  const { props, container } = setup({}, { fontSize: 17, lineWrap: true });
  const files = screen.getByRole("tab", { name: "Files" });
  files.focus();
  fireEvent.keyDown(files, { key: "ArrowRight" });
  const changes = screen.getByRole("tab", { name: "Changes" });
  expect(document.activeElement).toBe(changes);
  expect(changes.getAttribute("aria-selected")).toBe("true");
  expect(files.tabIndex).toBe(-1);
  fireEvent.keyDown(changes, { key: "Home" });
  expect(document.activeElement).toBe(files);
  await openFile();
  const area = within(content()).getByRole("listbox", { name: "a.ts" });
  fireEvent.keyDown(area, { key: "ArrowDown" });
  fireEvent.keyDown(area, { key: "End", shiftKey: true });
  fireEvent.click(screen.getByRole("button", { name: "Quote in chat" }));
  expect(props.onQuote).toHaveBeenLastCalledWith({ workspaceId: A, text: "`a.ts:1-3`" });
  const sidebar = container.querySelector<HTMLElement>(".code-preview");
  expect(sidebar?.style.getPropertyValue("--code-font-size")).toBe("17px");
  expect(sidebar?.hasAttribute("data-wrap")).toBe(true);
  fireEvent.keyDown(files, { key: "End" });
  expect(document.activeElement).toBe(screen.getByRole("tab", { name: "Changes" }));
});

test("closing a held preview aborts reads and cannot reopen on a late response", async () => {
  const pending = deferred<CodeFile>();
  const { props } = setup({ file: vi.fn().mockReturnValue(pending.promise) });
  await openFile();
  const signal = vi.mocked(props.api.file).mock.calls[0]?.[2];
  expect(within(content()).getByRole("status").textContent).toBe("Loading file…");
  fireEvent.click(screen.getByRole("button", { name: "Close code preview" }));
  expect(signal?.aborted).toBe(true);
  expect(screen.queryByRole("region", { name: "Code content" })).toBeNull();
  await act(async () => pending.resolve(file("a.ts", "late closed content")));
  expect(fileTexts()).not.toContain("late closed content");
  expect(screen.queryByRole("region", { name: "Code content" })).toBeNull();
});

test("preview content is outside the explorer and offers explicit Edit before write controls", async () => {
  const { container, props } = setup();
  await openFile();
  expect(content().closest(".code-sidebar")).toBeNull();
  expect(container.querySelector(".code-sidebar .code-file")).toBeNull();
  expect(screen.getByRole("button", { name: "Back to chat" })).toBeTruthy();
  const pencil = screen.getByRole("button", { name: "Edit" }).querySelector("svg");
  expect(pencil?.getAttribute("width")).toBe("16");
  expect(pencil?.getAttribute("height")).toBe("16");
  expect(screen.queryByRole("button", { name: /^(Save|Terminal)$/ })).toBeNull();
  expect(props.api.save).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Back to chat" }));
  expect(screen.queryByRole("region", { name: "Code content" })).toBeNull();
});

test("explicit Edit opens the existing editor in center without saving", async () => {
  const { props, container } = setup();
  await openFile();
  expect(loader.create).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Edit" }));
  await settle();
  expect(within(content()).getByRole("textbox", { name: "Edit a.ts" })).toBeTruthy();
  expect(container.querySelector(".code-sidebar .code-editor")).toBeNull();
  expect(within(content()).getByRole("button", { name: "Save" })).toBeTruthy();
  expect(props.api.save).not.toHaveBeenCalled();
  expect(loader.create).toHaveBeenCalledWith(
    expect.objectContaining({
      ref: "a.ts",
      text: "first\nsecond\nthird",
      fontSize: 13,
      wrap: false,
    }),
  );
  fireEvent.click(within(content()).getByRole("button", { name: "Close" }));
  await settle();
  expect(props.api.file).toHaveBeenCalledTimes(2);
  expect(within(content()).queryByRole("textbox")).toBeNull();
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "Edit" }));
});

test.each([
  [{ policy: "existing_readonly" }, {}, "Trust this folder to edit its files."],
  [{ aliased: true }, {}, "This folder can't be saved to now."],
  [
    {},
    { newline: "mixed" },
    "This file mixes line endings, so it opens read-only. Edit it in VS Code.",
  ],
  [{}, { sha256: null }, "This file can't be edited here. Open it in VS Code."],
] as const)("Edit explains a refused gate (%j, %j)", async (registration, metadata, reason) => {
  const { props } = setup(
    { file: vi.fn().mockResolvedValue(file("a.ts", "first", metadata)) },
    { folders: [folder(A, "Alpha", registration)] },
  );
  await openFile();
  const edit = screen.getByRole("button", { name: "Edit" });
  expect(edit.getAttribute("aria-disabled")).toBe("true");
  expect(document.getElementById(edit.getAttribute("aria-describedby") ?? "")?.textContent).toBe(
    reason,
  );
  fireEvent.click(edit);
  expect(loader.create).not.toHaveBeenCalled();
  expect(props.api.save).not.toHaveBeenCalled();
});

test("the preview refuses a request from an old API before starting any read", async () => {
  const oldApi = fakeApi();
  const api = fakeApi();
  const currentFolder = folder(A, "Alpha");
  render(
    <CodePreview
      api={api}
      folders={[currentFolder]}
      activeId={A}
      request={{
        api: oldApi,
        folder: currentFolder,
        folderKey: codeFolderKey(currentFolder),
        path: "a.ts",
        withDiff: true,
      }}
      onQuote={vi.fn()}
      onOpenInEditor={vi.fn()}
      onClose={vi.fn()}
      fontSize={13}
      lineWrap={false}
    />,
  );
  await settle();
  expect(within(content()).getByRole("alert").textContent).toBe(
    "This preview is no longer available. Choose the file again.",
  );
  expect(api.file).not.toHaveBeenCalled();
  expect(api.diff).not.toHaveBeenCalled();
  expect(oldApi.file).not.toHaveBeenCalled();
});

test("a reused registration row cannot retarget an already selected preview", async () => {
  const api = fakeApi({ file: vi.fn().mockResolvedValue(file("a.ts", "original root content")) });
  const registration = folder(A, "Alpha");
  const request: CodeOpenRequest = {
    api,
    folder: registration,
    folderKey: codeFolderKey(registration),
    path: "a.ts",
    withDiff: false,
  };
  const props: CodePreviewProps = {
    api,
    folders: [registration],
    activeId: A,
    request,
    onQuote: vi.fn(),
    onOpenInEditor: vi.fn(),
    onClose: vi.fn(),
    fontSize: 13,
    lineWrap: false,
  };
  const { rerender } = render(<CodePreview {...props} />);
  await settle();
  expect(fileTexts(content())).toContain("original root content");
  const signal = vi.mocked(api.file).mock.calls[0]?.[2];
  const original = { ...registration };
  Object.assign(registration, {
    root: "replacement-root",
    root_physical_id: "replacement-identity",
    policy_revision: 4,
  });
  rerender(<CodePreview {...props} folders={[registration]} />);
  expect(fileTexts()).not.toContain("original root content");
  expect(screen.queryByRole("button", { name: "Quote in chat" })).toBeNull();
  expect(signal?.aborted).toBe(true);
  await settle();
  expect(api.file).toHaveBeenCalledTimes(1);
  expect(within(content()).getByRole("alert").textContent).toBe(
    "This preview is no longer available. Choose the file again.",
  );
  Object.assign(registration, original);
  rerender(<CodePreview {...props} folders={[registration]} />);
  await settle();
  expect(api.file).toHaveBeenCalledTimes(1);
  expect(fileTexts()).not.toContain("original root content");
});
