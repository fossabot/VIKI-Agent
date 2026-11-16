import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { WorkspaceJson } from "../../clients/app/shellApi";
import {
  type CodeApi,
  CodeApiError,
  type CodeDiff,
  type CodeFile,
  type CodeSaveResult,
} from "../../clients/code/codeApi";
import {
  dirtyRefs,
  dropBuffer,
  editorOwner,
  getBuffer,
  savedVersion,
} from "../../state/codeBuffers";
import { useHasUnsavedWork } from "../../state/unsavedWork";
import { CodePreview, type CodePreviewProps } from "./CodePreview";
import { type CodeOpenRequest, codeFolderKey } from "./CodeSidebar";
import type { EditorOptions } from "./codemirror";

const loader = vi.hoisted(() => ({ create: vi.fn() }));
vi.mock("./codemirror", () => ({ createEditor: loader.create }));
const lf = (text: string) => text.replace(/\r\n?/g, "\n");
const A = "00000000-0000-4000-8000-00000000000a";
const B = "00000000-0000-4000-8000-00000000000b";
const folder = (workspace_id = A, over: Partial<WorkspaceJson> = {}) =>
  ({
    workspace_id,
    kind: "code",
    name: "Code",
    root: "registered-code",
    policy: "ordinary",
    policy_revision: 3,
    root_physical_id: "physical-code",
    root_available: true,
    persistent: true,
    ...over,
  }) as WorkspaceJson;
const file = (path = "a.txt", over: Partial<CodeFile> = {}): CodeFile => ({
  path,
  text: "first\r\n",
  size: 7,
  sha256: "a".repeat(64),
  encoding: "utf-16-le",
  newline: "crlf",
  reason: null,
  ...over,
});
const saved = (): CodeSaveResult => ({
  operation_id: "save-a",
  status: "saved",
  sha256: "b".repeat(64),
  cleanup_pending: false,
});
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
const settle = () => act(async () => {});
const button = (name: string) => screen.getByRole("button", { name });
const type = (area: HTMLTextAreaElement, text: string) =>
  fireEvent.input(area, { target: { value: text } });
async function edit(path = "a.txt") {
  await settle();
  fireEvent.click(button("Edit"));
  await settle();
  return screen.getByRole("textbox", { name: `Edit ${path}` }) as HTMLTextAreaElement;
}

beforeEach(() => {
  loader.create.mockReset().mockImplementation(async (options: EditorOptions) => {
    const area = document.createElement("textarea");
    area.setAttribute("aria-label", options.label);
    area.value = options.text;
    let base = options.base,
      reported = false;
    const report = () => {
      const dirty = lf(area.value) !== lf(base);
      if (dirty !== reported) {
        reported = dirty;
        options.onDirty(dirty);
      }
    };
    const changed = () => {
      report();
      options.onChange?.(lf(area.value));
    };
    area.addEventListener("input", changed);
    options.parent.append(area);
    report();
    return {
      text: () => lf(area.value),
      setText: (text: string) => {
        area.value = text;
        changed();
      },
      setBase: (text: string) => {
        base = text;
        report();
      },
      setWrap: vi.fn(),
      setFontSize: vi.fn(),
      focus: () => area.focus(),
      destroy: () => area.remove(),
    };
  });
});
afterEach(() => {
  cleanup();
  for (const id of [A, B]) for (const ref of dirtyRefs(id)) dropBuffer(id, ref);
  vi.restoreAllMocks();
});

function setup(over: Partial<CodePreviewProps> = {}, readFile = file()) {
  const api: CodeApi = {
    tree: vi.fn(),
    changes: vi.fn(),
    diff: vi.fn(),
    search: vi.fn(),
    file: vi.fn().mockImplementation(async (_id, path) => ({ ...readFile, path })),
    save: vi.fn(),
  };
  const registration = folder();
  const recovery = { list: vi.fn().mockResolvedValue([]), forget: vi.fn() };
  const request = (path = "a.txt", row = registration, client = api): CodeOpenRequest => ({
    api: client,
    folder: { ...row },
    folderKey: codeFolderKey(row),
    path,
    withDiff: false,
  });
  let props: CodePreviewProps = {
    api,
    folders: [registration, folder(B)],
    activeId: A,
    request: request(),
    onQuote: vi.fn(),
    onOpenInEditor: vi.fn(),
    onClose: vi.fn(),
    fontSize: 13,
    lineWrap: false,
    recovery,
    ...over,
  };
  let view = render(<CodePreview {...props} />);
  const show = (next: Partial<CodePreviewProps>) => {
    props = { ...props, ...next };
    view.rerender(<CodePreview {...props} />);
  };
  const remount = () => {
    view.unmount();
    view = render(<CodePreview {...props} />);
  };
  return { api, registration, recovery, request, show, remount, props };
}

const ownerLabel = (owner: string) => `the ${owner} pane`;
const lines = (root: ParentNode) =>
  [...root.querySelectorAll(".code-text")].map((line) => line.textContent);

/** Two views of a.txt in one window, as two panes show it: setup's view is "left". With `diff`,
 * the right view opens with it, as a file chosen from the changes list does. */
function twoViews(diff?: CodeDiff) {
  const onShowEditor = vi.fn();
  const h = setup({ viewId: "left", ownerLabel, onShowEditor });
  if (diff) vi.mocked(h.api.diff).mockResolvedValue(diff);
  const second = render(
    <CodePreview
      {...h.props}
      viewId="right"
      request={{ ...h.request(), withDiff: diff !== undefined }}
    />,
  );
  const left = () =>
    within(
      screen
        .getAllByRole("region", { name: "Code content" })
        .find((region) => !second.container.contains(region)) as HTMLElement,
    );
  return { ...h, onShowEditor, second, left, right: () => within(second.container) };
}
const textbox = (pane: ReturnType<typeof within>) =>
  pane.getByRole("textbox", { name: "Edit a.txt" }) as HTMLTextAreaElement;

test("center Save uses the selected API and exact read metadata; conflict retains the draft", async () => {
  const { api, props } = setup();
  await settle();
  expect(loader.create).not.toHaveBeenCalled();
  expect(props.recovery?.list).not.toHaveBeenCalled();
  const area = await edit();
  type(area, "local\n");
  vi.mocked(api.save).mockResolvedValue({ ...saved(), status: "conflict", sha256: null });
  vi.mocked(api.file).mockResolvedValue(
    file("a.txt", { text: "disk\r\n", sha256: "c".repeat(64) }),
  );
  fireEvent.click(button("Save"));
  await settle();
  expect(api.save).toHaveBeenCalledWith(A, "a.txt", {
    policy_revision: 3,
    root_physical_id: "physical-code",
    content: "local\n",
    expected_sha256: "a".repeat(64),
    encoding: "utf-16-le",
    newline: "crlf",
  });
  expect(area.value).toBe("local\n");
  expect(screen.getByText("Disk version").closest("aside")?.querySelector("pre")?.textContent).toBe(
    "disk\r\n",
  );
  expect(getBuffer(A, "a.txt")).toMatchObject({
    text: "local\n",
    base: { sha256: "c".repeat(64) },
  });
  vi.mocked(api.save).mockResolvedValue(saved());
  fireEvent.click(button("Save"));
  await settle();
  expect(vi.mocked(api.save).mock.calls[1]?.[2].expected_sha256).toBe("c".repeat(64));
  expect(getBuffer(A, "a.txt")).toBeUndefined();
});

test("blocked center Save refreshes reachable recovery only after explicit Edit", async () => {
  const { api, props, recovery } = setup();
  await settle();
  expect(props.recovery?.list).not.toHaveBeenCalled();
  const area = await edit();
  recovery.list.mockResolvedValue([
    {
      operation_id: "save-a",
      workspace_id: A,
      ref: "a.txt",
      state: "conflict",
      reason: "conflict",
      resolvable: ["forget"],
    },
  ]);
  vi.mocked(api.save).mockRejectedValue(new CodeApiError("recovery_required", "save-a"));
  type(area, "kept\n");
  fireEvent.click(button("Save"));
  await settle();
  expect(props.recovery?.list).toHaveBeenCalledTimes(2);
  expect(screen.getByRole("heading", { name: "Stopped saves" })).toBeTruthy();
  expect(button("Forget the record")).toBeTruthy();
  expect(area.value).toBe("kept\n");
  expect(getBuffer(A, "a.txt")?.text).toBe("kept\n");
  expect(api.file).toHaveBeenCalledTimes(1);
});

test.each(["refresh", "file", "folder", "close", "unmount"])(
  "%s retains dirty text and requires fresh explicit Edit to resume",
  async (boundary) => {
    const h = setup();
    type(await edit(), "retained\n");
    if (boundary === "file") h.show({ request: h.request("b.txt") });
    else if (boundary === "folder") h.show({ request: h.request("a.txt", folder(B)) });
    else if (boundary === "close") fireEvent.click(button("Close code preview"));
    else if (boundary === "unmount") h.remount();
    else h.show({ request: h.request() });
    await settle();
    expect(getBuffer(A, "a.txt")?.text).toBe("retained\n");
    expect(screen.queryByRole("textbox")).toBeNull();
    h.show({ request: h.request() });
    await settle();
    expect(screen.getByText(/Choose Edit to resume them/)).toBeTruthy();
    expect(screen.queryByRole("textbox")).toBeNull();
    expect((await edit()).value).toBe("retained\n");
    expect(h.api.save).not.toHaveBeenCalled();
  },
);

test.each(["API", "registration", "root", "policy"])(
  "%s invalidation removes editing irreversibly; a fresh selection resumes its draft",
  async (change) => {
    const h = setup();
    type(await edit(), "retained\n");
    const replacement = { ...h.api, file: vi.fn(), save: vi.fn() };
    h.show(
      change === "API"
        ? { api: replacement }
        : {
            folders:
              change === "registration"
                ? []
                : [
                    folder(
                      A,
                      change === "root" ? { root_available: false } : { policy_revision: 4 },
                    ),
                  ],
          },
    );
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(getBuffer(A, "a.txt")?.text).toBe("retained\n");
    h.show({ api: h.api, folders: h.props.folders });
    await settle();
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
    expect(h.api.file).toHaveBeenCalledTimes(1);
    expect(replacement.file).not.toHaveBeenCalled();
    h.show({ request: h.request() });
    expect((await edit()).value).toBe("retained\n");
  },
);

test.each([
  [{ persistent: false }, {}, "This folder can't be saved to now."],
  [{ root_physical_id: "" }, {}, "This folder can't be saved to now."],
  [{}, { encoding: null }, "This file can't be edited here. Open it in VS Code."],
  [{}, { newline: null }, "This file can't be edited here. Open it in VS Code."],
  [{}, { reason: "too_large" }, "This file can't be edited here. Open it in VS Code."],
] as const)(
  "ineligible metadata %j / %j refuses Edit without an editor or save",
  async (registration, metadata, reason) => {
    const row = folder(A, registration);
    const h = setup({ folders: [row] }, file("a.txt", metadata));
    h.show({ request: h.request("a.txt", row) });
    await settle();
    const control = button("Edit");
    expect(control.getAttribute("aria-disabled")).toBe("true");
    expect(
      document.getElementById(control.getAttribute("aria-describedby") ?? "")?.textContent,
    ).toBe(reason);
    fireEvent.click(control);
    expect(loader.create).not.toHaveBeenCalled();
    expect(h.api.save).not.toHaveBeenCalled();
  },
);

test.each([512, 513])("Edit admits paths up to 512 Unicode code points (%i)", async (length) => {
  const h = setup();
  const path = `${"😀".repeat(length - 4)}.txt`;
  h.show({ request: h.request(path) });
  await settle();
  expect(button("Edit").getAttribute("aria-disabled")).toBe(String(length > 512));
  fireEvent.click(button("Edit"));
  await settle();
  expect(screen.queryByRole("textbox", { name: `Edit ${path}` }) !== null).toBe(length === 512);
  if (length > 512)
    expect(screen.getByText("This file's path is too long to save from VIKI.")).toBeTruthy();
  expect(h.api.save).not.toHaveBeenCalled();
});

test("late success preserves newer RAM text in a still-dirty explicit resume", async () => {
  const h = setup(),
    answer = deferred<CodeSaveResult>();
  vi.mocked(h.api.save).mockReturnValue(answer.promise);
  type(await edit(), "sent\n");
  fireEvent.click(button("Save"));
  h.show({ request: h.request("b.txt", folder(B)) });
  await edit("b.txt");
  h.show({ request: h.request() });
  const newest = await edit();
  expect(button("Close").getAttribute("aria-disabled")).toBe("true");
  type(newest, "newest\n");
  newest.focus();
  await act(async () => answer.resolve(saved()));
  expect(getBuffer(A, "a.txt")).toEqual({
    workspaceId: A,
    ref: "a.txt",
    text: "newest\n",
    base: { text: "sent\n", sha256: "b".repeat(64), encoding: "utf-16-le", newline: "crlf" },
  });
  expect(newest.value).toBe("newest\n");
  expect(document.activeElement).toBe(newest);
  expect(h.api.save).toHaveBeenCalledTimes(1);
  expect(h.props.recovery?.list).toHaveBeenCalledTimes(3);
});

test.each([
  ["a reversal after a clean completion", "sent\n", "first\n"],
  ["another edit after a dirty completion", "newest\n", "later\n"],
])("a resumed editor retains %s against the saved base", async (_case, resumed, next) => {
  const h = setup(),
    answer = deferred<CodeSaveResult>();
  const unload = renderHook(() => useHasUnsavedWork());
  vi.mocked(h.api.save).mockReturnValueOnce(answer.promise);
  type(await edit(), "sent\n");
  fireEvent.click(button("Save"));
  fireEvent.click(button("Back to chat"));
  expect(screen.queryByRole("textbox")).toBeNull();
  h.show({ request: h.request() });
  const replacement = await edit();
  expect(replacement.value).toBe("sent\n");
  type(replacement, resumed);
  replacement.focus();
  vi.mocked(h.api.file).mockImplementation(async (_id, path) =>
    file(path, { text: "sent\r\n", sha256: "b".repeat(64) }),
  );
  await act(async () => answer.resolve(saved()));
  expect(replacement.value).toBe(resumed);
  expect(document.activeElement).toBe(replacement);
  type(replacement, next);
  expect(button("Save").getAttribute("aria-disabled")).toBe("false");
  expect(button("Close").getAttribute("aria-disabled")).toBe("true");
  expect(unload.result.current).toBe(true);
  const retained = {
    text: next,
    base: { text: "sent\n", sha256: "b".repeat(64), encoding: "utf-16-le", newline: "crlf" },
  };
  expect(getBuffer(A, "a.txt")).toMatchObject(retained);
  fireEvent.click(button("Close"));
  expect(screen.getByRole("textbox", { name: "Edit a.txt" })).toBe(replacement);
  h.show({ request: h.request("b.txt") });
  await settle();
  expect(getBuffer(A, "a.txt")).toMatchObject(retained);
  h.show({ request: h.request() });
  expect((await edit()).value).toBe(next);
  vi.mocked(h.api.save).mockResolvedValueOnce({ ...saved(), sha256: "c".repeat(64) });
  fireEvent.click(button("Save"));
  await settle();
  expect(vi.mocked(h.api.save).mock.calls[1]?.[2]).toMatchObject({
    content: next,
    expected_sha256: "b".repeat(64),
  });
  expect(getBuffer(A, "a.txt")).toBeUndefined();
  expect(unload.result.current).toBe(false);
  unload.unmount();
});

test.each(["conflict", "refusal", "blocked-conflict"])(
  "late %s cannot change replacement API, file, focus, or recovery",
  async (result) => {
    const h = setup(),
      answer = deferred<CodeSaveResult>();
    vi.mocked(h.api.save).mockReturnValue(answer.promise);
    type(await edit(), "sent\n");
    fireEvent.click(button("Save"));
    const replacement = { ...h.api, file: vi.fn().mockResolvedValue(file("b.txt")), save: vi.fn() };
    const next = h.request("b.txt", folder(B), replacement);
    h.show({ api: replacement, request: next });
    const area = await edit("b.txt");
    type(area, "B stays\n");
    area.focus();
    await act(async () => {
      if (result === "refusal") answer.reject(new CodeApiError("recovery_required"));
      else
        answer.resolve({
          ...saved(),
          status: "conflict",
          sha256: null,
          cleanup_pending: result === "blocked-conflict",
        });
    });
    expect(area.value).toBe("B stays\n");
    expect(document.activeElement).toBe(area);
    expect(screen.queryByText("Disk version")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(getBuffer(A, "a.txt")?.text).toBe("sent\n");
    expect(getBuffer(B, "b.txt")?.text).toBe("B stays\n");
    expect(replacement.file).toHaveBeenCalledTimes(1);
    expect(replacement.save).not.toHaveBeenCalled();
    expect(h.api.file).toHaveBeenCalledTimes(result === "conflict" ? 2 : 1);
    expect(h.props.recovery?.list).toHaveBeenCalledTimes(2);
  },
);

test("a second Edit of the file is refused while another view edits it; its Close releases it", async () => {
  const h = twoViews();
  await settle();
  fireEvent.click(h.left().getByRole("button", { name: "Edit" }));
  await settle();
  expect(textbox(h.left())).toBeTruthy();
  const refused = h.right().getByRole("button", { name: "Edit" });
  expect(refused.getAttribute("aria-disabled")).toBe("true");
  expect(document.getElementById(refused.getAttribute("aria-describedby") ?? "")?.textContent).toBe(
    "Editing in the left pane.",
  );
  fireEvent.click(refused);
  await settle();
  expect(h.right().queryByRole("textbox")).toBeNull();
  expect(loader.create).toHaveBeenCalledTimes(1);
  expect(editorOwner(A, "a.txt")).toBe("left");
  fireEvent.click(h.right().getByRole("button", { name: "Show editor" }));
  expect(h.onShowEditor).toHaveBeenCalledExactlyOnceWith("left");

  fireEvent.click(h.left().getByRole("button", { name: "Close" }));
  await settle();
  expect(editorOwner(A, "a.txt")).toBeUndefined();
  expect(h.right().queryByText("Editing in the left pane.")).toBeNull();
  expect(h.right().queryByRole("button", { name: "Show editor" })).toBeNull();
  fireEvent.click(h.right().getByRole("button", { name: "Edit" }));
  await settle();
  expect(textbox(h.right())).toBeTruthy();
  expect(editorOwner(A, "a.txt")).toBe("right");
  expect(h.left().getByRole("button", { name: "Edit" }).getAttribute("aria-disabled")).toBe("true");
});

test.each(["close", "file", "unmount"])(
  "%s releases the editor, and the other view resumes the kept draft",
  async (boundary) => {
    const h = twoViews();
    await settle();
    fireEvent.click(h.right().getByRole("button", { name: "Edit" }));
    await settle();
    type(textbox(h.right()), "kept\n");
    expect(h.left().getByRole("button", { name: "Edit" }).getAttribute("aria-disabled")).toBe(
      "true",
    );
    if (boundary === "close")
      fireEvent.click(h.right().getByRole("button", { name: "Close code preview" }));
    else if (boundary === "file")
      h.second.rerender(<CodePreview {...h.props} viewId="right" request={h.request("b.txt")} />);
    else h.second.unmount();
    await settle();
    expect(editorOwner(A, "a.txt")).toBeUndefined();
    fireEvent.click(h.left().getByRole("button", { name: "Edit" }));
    await settle();
    expect(textbox(h.left()).value).toBe("kept\n");
    expect(editorOwner(A, "a.txt")).toBe("left");
  },
);

test("a save re-reads the file in a view not editing it; a conflict changes nothing there", async () => {
  const h = twoViews();
  await settle();
  expect(h.api.file).toHaveBeenCalledTimes(2);
  expect(lines(h.second.container)).toEqual(["first"]);
  fireEvent.click(h.left().getByRole("button", { name: "Edit" }));
  await settle();
  type(textbox(h.left()), "local\n");
  const before = savedVersion(A, "a.txt");
  vi.mocked(h.api.save).mockResolvedValueOnce({ ...saved(), status: "conflict", sha256: null });
  fireEvent.click(h.left().getByRole("button", { name: "Save" }));
  await settle();
  expect(h.left().getByText("Disk version")).toBeTruthy();
  expect(savedVersion(A, "a.txt")).toBe(before);
  // The conflict view's read of the disk version is the only new read.
  expect(h.api.file).toHaveBeenCalledTimes(3);

  vi.mocked(h.api.save).mockResolvedValueOnce(saved());
  vi.mocked(h.api.file).mockImplementation(async (_id, path) =>
    file(path, { text: "local\r\n", sha256: "b".repeat(64) }),
  );
  fireEvent.click(h.left().getByRole("button", { name: "Save" }));
  await settle();
  expect(savedVersion(A, "a.txt")).toBe(before + 1);
  expect(h.api.file).toHaveBeenCalledTimes(4);
  expect(lines(h.second.container)).toEqual(["local"]);
  expect(textbox(h.left()).value).toBe("local\n");
  expect(h.left().getByText("Saved.")).toBeTruthy();
});

test("a refused view shows only the refusal, not the unsaved-changes notice", async () => {
  const h = twoViews();
  await settle();
  fireEvent.click(h.left().getByRole("button", { name: "Edit" }));
  await settle();
  type(textbox(h.left()), "draft\n");
  expect(h.right().getByText("Editing in the left pane.")).toBeTruthy();
  expect(h.right().getByRole("button", { name: "Show editor" })).toBeTruthy();
  expect(h.right().queryByText(/Unsaved changes are kept/)).toBeNull();
  // Once the editor is free, the notice is back.
  fireEvent.click(h.left().getByRole("button", { name: "Close code preview" }));
  await settle();
  expect(
    h.right().getByText("Unsaved changes are kept in this window. Choose Edit to resume them."),
  ).toBeTruthy();
});

test("a re-read after a save keeps the view's mode and its content on screen", async () => {
  const h = twoViews({ path: "a.txt", text: "-old\n+first\n", reason: null });
  await settle();
  expect(h.right().getByRole("button", { name: "Diff" }).getAttribute("aria-pressed")).toBe("true");
  fireEvent.click(h.right().getByRole("button", { name: "File" }));
  const list = h.right().getByRole("listbox", { name: "a.txt" });
  fireEvent.click(h.left().getByRole("button", { name: "Edit" }));
  await settle();
  type(textbox(h.left()), "local\n");
  const read = deferred<CodeFile>();
  vi.mocked(h.api.file).mockReturnValueOnce(read.promise);
  vi.mocked(h.api.save).mockResolvedValueOnce(saved());
  fireEvent.click(h.left().getByRole("button", { name: "Save" }));
  await settle();
  // The old content stays mounted, and with it the scroll position, until the new read lands.
  expect(h.api.file).toHaveBeenCalledTimes(3);
  expect(h.right().queryByText("Loading file…")).toBeNull();
  expect(lines(h.second.container)).toEqual(["first"]);
  await act(async () => read.resolve(file("a.txt", { text: "local\r\n", sha256: "b".repeat(64) })));
  expect(lines(h.second.container)).toEqual(["local"]);
  expect(h.right().getByRole("listbox", { name: "a.txt" })).toBe(list);
  expect(h.right().getByRole("button", { name: "File" }).getAttribute("aria-pressed")).toBe("true");
  expect(h.api.diff).toHaveBeenCalledTimes(2);
});

test("in a pane the preview closes with Close file and has no Back to chat; the middle preview keeps both", async () => {
  const inPane = setup({ inPane: true });
  await settle();
  expect(screen.queryByRole("button", { name: "Back to chat" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Close code preview" })).toBeNull();
  fireEvent.click(button("Close file"));
  expect(inPane.props.onClose).toHaveBeenCalledTimes(1);
  cleanup();

  const middle = setup();
  await settle();
  expect(screen.queryByRole("button", { name: "Close file" })).toBeNull();
  fireEvent.click(button("Back to chat"));
  expect(middle.props.onClose).toHaveBeenCalledTimes(1);
});

test("the heading takes the focus on mount unless autoFocus is false, which is read at mount only", async () => {
  const quiet = setup({ autoFocus: false });
  await settle();
  const heading = screen.getByRole("heading", { name: "a.txt" });
  expect(document.activeElement).not.toBe(heading);
  quiet.show({ autoFocus: true });
  await settle();
  expect(document.activeElement).not.toBe(heading);
  cleanup();

  setup();
  await settle();
  expect(document.activeElement).toBe(screen.getByRole("heading", { name: "a.txt" }));
});
