import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import {
  type CodeApi,
  CodeApiError,
  type CodeFile,
  type CodeSaveResult,
} from "../../clients/code/codeApi";
import { dirtyRefs, dropBuffer, getBuffer, putBuffer } from "../../state/codeBuffers";
import { useHasUnsavedWork } from "../../state/unsavedWork";
import { CodeEditor } from "./CodeEditor";
import type { EditorHandle, EditorOptions } from "./codemirror";

// The real editor is a lazy chunk and needs layout; a textarea stands in for it.
const loader = vi.hoisted(() => ({ create: vi.fn() }));
vi.mock("./codemirror", () => ({ createEditor: loader.create }));

interface FakeEditor {
  options: EditorOptions;
  area: HTMLTextAreaElement;
  handle: EditorHandle & { [K in keyof EditorHandle]: ReturnType<typeof vi.fn> };
}
let editors: FakeEditor[] = [];

const lf = (text: string) => text.replace(/\r\n?/g, "\n");

function ed(index = 0): FakeEditor {
  const found = editors[index];
  if (found === undefined) throw new Error(`no editor ${index} was created`);
  return found;
}

/** The body of the nth save request. */
const sentBody = (save: ReturnType<typeof vi.fn>, n: number): unknown => save.mock.calls[n]?.[2];

function fakeEditor(options: EditorOptions): FakeEditor {
  const area = document.createElement("textarea");
  area.setAttribute("aria-label", options.label);
  area.value = options.text;
  let base = options.base;
  let reported = false;
  const report = () => {
    const dirty = lf(area.value) !== lf(base);
    if (dirty === reported) return;
    reported = dirty;
    options.onDirty(dirty);
  };
  const change = () => {
    report();
    options.onChange?.(lf(area.value));
  };
  area.addEventListener("input", change);
  area.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "s") options.onSave();
  });
  options.parent.append(area);
  report();
  const handle = {
    text: vi.fn(() => lf(area.value)),
    setText: vi.fn((text: string) => {
      area.value = text;
      change();
    }),
    setBase: vi.fn((text: string) => {
      base = text;
      report();
    }),
    setWrap: vi.fn(),
    setFontSize: vi.fn(),
    focus: vi.fn(),
    destroy: vi.fn(() => area.remove()),
  };
  const editor = { options, area, handle };
  editors.push(editor);
  return editor;
}

beforeEach(() => {
  editors = [];
  loader.create.mockReset();
  loader.create.mockImplementation((options: EditorOptions) =>
    Promise.resolve(fakeEditor(options).handle),
  );
});
afterEach(() => {
  cleanup();
  for (const ref of dirtyRefs("w1")) {
    // A buffer left by one test would arm the next one's editor.
    dropBuffer("w1", ref);
  }
  vi.restoreAllMocks();
});

/** Makes the next createEditor wait; the returned function lets it finish. */
function holdEditor() {
  const held: { release?: () => void } = {};
  loader.create.mockImplementation(
    (options: EditorOptions) =>
      new Promise<EditorHandle>((resolve) => {
        held.release = () => resolve(fakeEditor(options).handle);
      }),
  );
  return async () => {
    await waitFor(() => expect(held.release).toBeDefined());
    held.release?.();
  };
}

function file(over: Partial<CodeFile> = {}): CodeFile {
  return {
    path: "src/a.py",
    size: 6,
    sha256: "h1",
    encoding: "utf-8",
    newline: "lf",
    text: "a = 1\n",
    reason: null,
    ...over,
  };
}

const saved = (sha256 = "h2"): CodeSaveResult => ({
  operation_id: "op1",
  status: "saved",
  sha256,
  cleanup_pending: false,
});
const conflict = (cleanup_pending = false): CodeSaveResult => ({
  operation_id: "op1",
  status: "conflict",
  sha256: null,
  cleanup_pending,
});

function setup(over: Partial<Parameters<typeof CodeEditor>[0]> = {}) {
  const save = vi.fn();
  const read = vi.fn();
  const api: CodeApi = {
    tree: vi.fn(),
    file: read,
    search: vi.fn(),
    changes: vi.fn(),
    diff: vi.fn(),
    save,
  };
  const props = {
    api,
    workspaceId: "w1",
    policyRevision: 3,
    rootPhysicalId: "root1",
    file: file(),
    fileVerified: true,
    fontSize: 13,
    wrap: false,
    onBlocked: vi.fn(),
    onClose: vi.fn(),
    ...over,
  };
  const view = render(<CodeEditor {...props} />);
  return { save, read, props, view };
}

async function editor(name = "Edit src/a.py") {
  return (await screen.findByLabelText(name)) as HTMLTextAreaElement;
}
const type = (area: HTMLTextAreaElement, text: string) =>
  fireEvent.input(area, { target: { value: text } });
const button = (name: string) => screen.getByRole("button", { name });
const disabled = (name: string) => button(name).getAttribute("aria-disabled") === "true";

function heldSave(save: ReturnType<typeof vi.fn>) {
  let resolve!: (value: CodeSaveResult) => void;
  let reject!: (error: unknown) => void;
  save.mockReturnValue(
    new Promise<CodeSaveResult>((res, rej) => {
      resolve = res;
      reject = rej;
    }),
  );
  return { resolve, reject };
}

const settle = () => act(async () => {});

async function conflictOn(diskFile: CodeFile) {
  const t = setup();
  t.save.mockResolvedValue(conflict());
  t.read.mockResolvedValue(diskFile);
  const area = await editor();
  type(area, "a = 2\n");
  fireEvent.click(button("Save"));
  await screen.findByText("Disk version");
  return { ...t, area };
}

describe("loading", () => {
  test("shows the loading words until the editor exists, then names it after the file", async () => {
    const release = holdEditor();
    setup();
    expect(screen.getByText("Loading the editor…")).toBeTruthy();
    await release();
    await editor();
    await waitFor(() => expect(screen.queryByText("Loading the editor…")).toBeNull());
    const options = ed().options;
    expect(options).toMatchObject({
      text: "a = 1\n",
      base: "a = 1\n",
      ref: "src/a.py",
      wrap: false,
      fontSize: 13,
    });
  });

  test("a failed load shows its words and Retry loads again", async () => {
    loader.create.mockRejectedValueOnce(new Error("chunk"));
    setup();
    expect(await screen.findByText("The editor could not load.")).toBeTruthy();
    fireEvent.click(button("Retry"));
    await editor();
    expect(screen.queryByText("The editor could not load.")).toBeNull();
    expect(loader.create).toHaveBeenCalledTimes(2);
  });

  test("an editor created after unmount is destroyed at once", async () => {
    const release = holdEditor();
    const { view } = setup();
    view.unmount();
    await release();
    await waitFor(() => expect(ed().handle.destroy).toHaveBeenCalled());
    expect(getBuffer("w1", "src/a.py")).toBeUndefined();
  });

  test("a rerender keeps the editor and the edit", async () => {
    const { view, props } = setup();
    const area = await editor();
    type(area, "a = 2\n");
    view.rerender(<CodeEditor {...props} />);
    await settle();
    expect(editors).toHaveLength(1);
    expect(area.value).toBe("a = 2\n");
    expect(getBuffer("w1", "src/a.py")?.text).toBe("a = 2\n");
  });

  test("the editor takes focus once it is ready", async () => {
    setup();
    await editor();
    await waitFor(() => expect(ed().handle.focus).toHaveBeenCalledTimes(1));
  });

  test("an editor that loads after the focus moved elsewhere leaves the focus there", async () => {
    const release = holdEditor();
    setup();
    const input = document.createElement("input");
    document.body.append(input);
    try {
      input.focus();
      await release();
      await editor();
      expect(ed().handle.focus).not.toHaveBeenCalled();
      expect(document.activeElement).toBe(input);
    } finally {
      input.remove();
    }
  });

  test("an editor created after unmount is not focused", async () => {
    const release = holdEditor();
    const { view } = setup();
    view.unmount();
    await release();
    await waitFor(() => expect(ed().handle.destroy).toHaveBeenCalled());
    expect(ed().handle.focus).not.toHaveBeenCalled();
  });

  test("font size and wrap changes reach the handle", async () => {
    const { view, props } = setup();
    await editor();
    view.rerender(<CodeEditor {...props} fontSize={16} wrap />);
    expect(ed().handle.setFontSize).toHaveBeenCalledWith(16);
    expect(ed().handle.setWrap).toHaveBeenCalledWith(true);
  });
});

describe("saving", () => {
  test("a save sends the editor text with the base hash, encoding and newline", async () => {
    const { save } = setup({
      file: file({ encoding: "utf-16-le", newline: "crlf", text: "a = 1\r\n" }),
    });
    save.mockResolvedValue(saved());
    const area = await editor();
    expect(disabled("Save")).toBe(true);
    type(area, "a = 2\n");
    expect(disabled("Save")).toBe(false);
    fireEvent.click(button("Save"));
    await screen.findByText("Saved.");
    expect(save).toHaveBeenCalledWith("w1", "src/a.py", {
      policy_revision: 3,
      root_physical_id: "root1",
      content: "a = 2\n",
      expected_sha256: "h1",
      encoding: "utf-16-le",
      newline: "crlf",
    });
  });

  test("saved makes the sent text and the answer's hash the base and drops the buffer", async () => {
    const { save } = setup();
    save.mockResolvedValue(saved("h2"));
    const area = await editor();
    type(area, "a = 2\n");
    expect(getBuffer("w1", "src/a.py")?.text).toBe("a = 2\n");
    fireEvent.click(button("Save"));
    await screen.findByText("Saved.");
    expect(getBuffer("w1", "src/a.py")).toBeUndefined();
    expect(ed().handle.setBase).toHaveBeenCalledWith("a = 2\n");
    expect(disabled("Save")).toBe(true);
    expect(screen.getByText("Saved.").closest("[aria-live]")?.getAttribute("aria-live")).toBe(
      "polite",
    );
    type(area, "a = 3\n");
    save.mockResolvedValue(saved("h3"));
    fireEvent.click(button("Save"));
    await waitFor(() => expect(save).toHaveBeenCalledTimes(2));
    expect(sentBody(save, 1)).toMatchObject({ expected_sha256: "h2", content: "a = 3\n" });
  });

  test("a save answered after the editor closed keeps the text typed since Save", async () => {
    const { save, view } = setup();
    let answer: (result: CodeSaveResult) => void = () => {};
    save.mockReturnValue(new Promise<CodeSaveResult>((resolve) => (answer = resolve)));
    const area = await editor();
    type(area, "a = 2\n");
    fireEvent.click(button("Save"));
    type(area, "a = 3\n");
    view.unmount();
    answer(saved("h2"));
    await waitFor(() =>
      expect(getBuffer("w1", "src/a.py")).toMatchObject({
        text: "a = 3\n",
        base: { text: "a = 2\n", sha256: "h2" },
      }),
    );
  });

  test.each(["saved", "no_change"] as const)(
    "%s after undo and unmount preserves the reversal against the returned base",
    async (status) => {
      const { save, view } = setup();
      const pending = heldSave(save);
      const unload = renderHook(() => useHasUnsavedWork());
      const area = await editor();
      type(area, "a = 2\n");
      fireEvent.click(button("Save"));
      type(area, "a = 1\n");
      expect(disabled("Close")).toBe(true);
      expect(unload.result.current).toBe(true);
      view.unmount();
      pending.resolve({ ...saved("h2"), status });
      await settle();
      expect(getBuffer("w1", "src/a.py")).toMatchObject({
        text: "a = 1\n",
        base: { text: "a = 2\n", sha256: "h2" },
      });
      expect(unload.result.current).toBe(true);
      unload.unmount();
    },
  );

  test("a mounted undo is still dirty after Save publishes the submitted text", async () => {
    const { save } = setup();
    const pending = heldSave(save);
    const area = await editor();
    type(area, "a = 2\n");
    fireEvent.click(button("Save"));
    type(area, "a = 1\n");
    pending.resolve(saved("h2"));
    await settle();
    expect(area.value).toBe("a = 1\n");
    expect(disabled("Save")).toBe(false);
    expect(getBuffer("w1", "src/a.py")).toMatchObject({
      text: "a = 1\n",
      base: { text: "a = 2\n", sha256: "h2" },
    });
  });

  test.each([new TypeError("timeout"), new CodeApiError("unknown_publication")])(
    "an uncertain %s after undo and unmount keeps the text and its last verified hash",
    async (error) => {
      const { save, view } = setup();
      const pending = heldSave(save);
      const area = await editor();
      type(area, "a = 2\n");
      fireEvent.click(button("Save"));
      type(area, "a = 1\n");
      view.unmount();
      pending.reject(error);
      await settle();
      expect(getBuffer("w1", "src/a.py")).toMatchObject({
        text: "a = 1\n",
        base: { text: "a = 1\n", sha256: "h1" },
      });
    },
  );

  test("an uncertain clean edit can be explicitly discarded", async () => {
    const { save, props } = setup();
    const pending = heldSave(save);
    const area = await editor();
    type(area, "a = 2\n");
    fireEvent.click(button("Save"));
    type(area, "a = 1\n");
    pending.reject(new TypeError("unheard result"));
    await settle();
    expect(disabled("Close")).toBe(true);
    expect(disabled("Revert")).toBe(false);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    fireEvent.click(button("Revert"));
    expect(getBuffer("w1", "src/a.py")).toBeUndefined();
    expect(disabled("Close")).toBe(false);
    fireEvent.click(button("Close"));
    expect(props.onClose).toHaveBeenCalledTimes(1);
  });

  test.each([false, true])(
    "a verified read keeps the reversal and same-base uncertainty: disk changed %s",
    async (changed) => {
      const first = setup();
      const pending = heldSave(first.save);
      const area = await editor();
      type(area, "a = 2\n");
      fireEvent.click(button("Save"));
      type(area, "a = 1\n");
      first.view.unmount();
      pending.reject(new TypeError("timeout"));
      await settle();
      expect(getBuffer("w1", "src/a.py")).toBeDefined();
      setup({ file: changed ? file({ text: "a = 2\n", sha256: "h2" }) : file() });
      expect((await editor()).value).toBe("a = 1\n");
      await settle();
      if (changed) {
        expect(getBuffer("w1", "src/a.py")).toMatchObject({
          text: "a = 1\n",
          base: { text: "a = 2\n", sha256: "h2" },
        });
      } else {
        expect(getBuffer("w1", "src/a.py")).toMatchObject({
          text: "a = 1\n",
          base: { text: "a = 1\n", sha256: "h1" },
        });
        expect(disabled("Close")).toBe(true);
      }
    },
  );

  test("same-base uncertainty survives another unmount until a different base is verified", async () => {
    const first = setup();
    const pending = heldSave(first.save);
    const area = await editor();
    type(area, "a = 2\n");
    fireEvent.click(button("Save"));
    type(area, "a = 1\n");
    first.view.unmount();
    pending.reject(new TypeError("timeout"));
    await settle();
    const second = setup();
    await editor();
    second.view.unmount();
    expect(getBuffer("w1", "src/a.py")).toMatchObject({
      text: "a = 1\n",
      base: { sha256: "h1" },
    });
    setup({ file: file({ sha256: "h3", encoding: "utf-8-bom" }) });
    expect((await editor()).value).toBe("a = 1\n");
    await settle();
    expect(getBuffer("w1", "src/a.py")).toBeUndefined();
    expect(disabled("Close")).toBe(false);
  });

  test("an old Save keeps the newest dirty-to-dirty text after the same file remounts", async () => {
    const first = setup();
    let answer!: (result: CodeSaveResult) => void;
    first.save.mockReturnValue(new Promise<CodeSaveResult>((resolve) => (answer = resolve)));
    type(await editor(), "a = 2\n");
    fireEvent.click(button("Save"));
    first.view.unmount();
    const second = setup();
    const newest = await editor();
    expect(newest.value).toBe("a = 2\n");
    type(newest, "a = 3\n");
    await act(async () => answer(saved("h2")));
    expect(newest.value).toBe("a = 3\n");
    expect(getBuffer("w1", "src/a.py")).toMatchObject({
      text: "a = 3\n",
      base: { text: "a = 2\n", sha256: "h2" },
    });
    expect(sentBody(first.save, 0)).toMatchObject({
      content: "a = 2\n",
      expected_sha256: "h1",
      policy_revision: 3,
      root_physical_id: "root1",
    });
    expect(second.save).not.toHaveBeenCalled();
  });

  test("a resumed editor that finishes loading after an old Save uses its verified base", async () => {
    const first = setup();
    const pending = heldSave(first.save);
    type(await editor(), "a = 2\n");
    fireEvent.click(button("Save"));
    first.view.unmount();
    const release = holdEditor();
    const second = setup();
    await settle();
    expect(loader.create).toHaveBeenCalledTimes(2);
    pending.resolve(saved("h2"));
    await settle();
    await release();
    const replacement = await editor();
    await settle();
    expect(replacement.value).toBe("a = 2\n");
    type(replacement, "a = 1\n");
    expect(disabled("Save")).toBe(false);
    expect(disabled("Close")).toBe(true);
    expect(getBuffer("w1", "src/a.py")).toMatchObject({
      text: "a = 1\n",
      base: { text: "a = 2\n", sha256: "h2" },
    });
    second.save.mockResolvedValue(saved("h3"));
    fireEvent.click(button("Save"));
    await settle();
    expect(sentBody(second.save, 0)).toMatchObject({ expected_sha256: "h2", content: "a = 1\n" });
  });

  test("no_change reads like saved", async () => {
    const { save } = setup();
    save.mockResolvedValue({ ...saved("h1"), status: "no_change" });
    type(await editor(), "a = 2\n");
    fireEvent.click(button("Save"));
    expect(await screen.findByText("Saved.")).toBeTruthy();
    expect(getBuffer("w1", "src/a.py")).toBeUndefined();
  });

  test("Mod-s saves a dirty editor and does nothing for a clean one", async () => {
    const { save } = setup();
    save.mockResolvedValue(saved());
    const area = await editor();
    fireEvent.keyDown(area, { key: "s", ctrlKey: true });
    expect(save).not.toHaveBeenCalled();
    type(area, "a = 2\n");
    fireEvent.keyDown(area, { key: "s", ctrlKey: true });
    await screen.findByText("Saved.");
    expect(save).toHaveBeenCalledTimes(1);
  });

  test("a second save while one is running is not sent", async () => {
    const { save } = setup();
    save.mockReturnValue(new Promise(() => {}));
    const area = await editor();
    type(area, "a = 2\n");
    fireEvent.click(button("Save"));
    fireEvent.keyDown(area, { key: "s", ctrlKey: true });
    expect(save).toHaveBeenCalledTimes(1);
    expect(disabled("Save")).toBe(true);
  });
});

describe("conflict", () => {
  const disk = () =>
    file({
      sha256: "h9",
      text: "<script>alert(1)</script>\r\n",
      newline: "crlf",
      encoding: "utf-8-bom",
    });

  test("a conflict keeps the edit and shows the disk version", async () => {
    const { area, props, save } = await conflictOn(disk());
    expect(area.value).toBe("a = 2\n");
    expect(
      screen.getByText(
        "This file changed elsewhere. Your edit is kept. Compare, then save again or use the disk version.",
      ),
    ).toBeTruthy();
    const pane = screen.getByText("Disk version").closest("aside") as HTMLElement;
    const pre = pane.querySelector("pre") as HTMLElement;
    expect(pre.textContent).toBe("<script>alert(1)</script>\r\n");
    expect(document.querySelector("script")).toBeNull();
    expect(pre.querySelector("*")).toBeNull();
    expect(disabled("Save")).toBe(false);
    expect(getBuffer("w1", "src/a.py")?.base).toMatchObject({ sha256: "h9", newline: "crlf" });
    expect(props.onBlocked).not.toHaveBeenCalled();

    save.mockResolvedValue(saved("h10"));
    fireEvent.click(button("Save"));
    await screen.findByText("Saved.");
    expect(sentBody(save, 1)).toEqual({
      policy_revision: 3,
      root_physical_id: "root1",
      content: "a = 2\n",
      expected_sha256: "h9",
      encoding: "utf-8-bom",
      newline: "crlf",
    });
  });

  test("Use this version puts the disk text in the editor and closes the pane", async () => {
    const { area } = await conflictOn(disk());
    await waitFor(() => expect(ed().handle.focus).toHaveBeenCalledTimes(1));
    fireEvent.click(button("Use this version"));
    expect(ed().handle.focus).toHaveBeenCalledTimes(2);
    expect(ed().handle.setText).toHaveBeenCalledWith("<script>alert(1)</script>\r\n");
    expect(area.value).toBe("<script>alert(1)</script>\n"); // a textarea folds CRLF to LF
    expect(screen.queryByText("Disk version")).toBeNull();
    expect(getBuffer("w1", "src/a.py")).toBeUndefined();
    expect(disabled("Close")).toBe(false);
  });

  test("Revert after a conflict gives the disk version", async () => {
    const { area } = await conflictOn(disk());
    vi.spyOn(window, "confirm").mockReturnValue(true);
    fireEvent.click(button("Revert"));
    expect(area.value).toBe("<script>alert(1)</script>\n");
    expect(getBuffer("w1", "src/a.py")).toBeUndefined();
  });

  test.each([
    [
      "binary",
      { text: null, encoding: null, newline: null, reason: "binary" as const },
      "This file is binary, so it is not shown.",
    ],
    [
      "another encoding",
      { text: null, encoding: null, newline: null, reason: "unsupported_encoding" as const },
      "This file's encoding is not supported, so it is not shown.",
    ],
    [
      "mixed newlines",
      { newline: "mixed" as const, text: "a\r\nb\n" },
      "The file on disk has mixed line endings.",
    ],
  ])("a disk version that is %s cannot be used or saved over", async (_name, over, reason) => {
    const { area } = await conflictOn(file({ sha256: "h9", ...over }));
    expect(screen.getByText(reason)).toBeTruthy();
    expect(screen.getByText("The file on disk can no longer be edited here.")).toBeTruthy();
    expect(screen.queryByText("Use this version")).toBeNull();
    expect(disabled("Save")).toBe(true);
    expect(area.value).toBe("a = 2\n");
    expect(getBuffer("w1", "src/a.py")?.base.sha256).toBe("h1");
  });

  test("a failed read of the disk version gives the generic words and keeps the edit", async () => {
    const { save, read } = setup();
    save.mockResolvedValue(conflict());
    read.mockRejectedValue(new Error("down"));
    const area = await editor();
    type(area, "a = 2\n");
    fireEvent.click(button("Save"));
    expect(await screen.findByText("The file could not be saved. Your edit is kept.")).toBeTruthy();
    expect(screen.queryByText("Disk version")).toBeNull();
    expect(area.value).toBe("a = 2\n");
  });

  test("a buffer based on an older file opens in the conflict view with the file as the disk", async () => {
    putBuffer({
      workspaceId: "w1",
      ref: "src/a.py",
      base: { text: "a = 0\n", sha256: "old", encoding: "utf-8", newline: "lf" },
      text: "a = 2\n",
    });
    const { save } = setup({ file: file({ text: "a = 5\n", newline: "crlf" }) });
    const area = await editor();
    expect(ed().options).toMatchObject({ text: "a = 2\n", base: "a = 5\n" });
    expect(area.value).toBe("a = 2\n");
    const pane = screen.getByText("Disk version").closest("aside") as HTMLElement;
    expect(pane.querySelector("pre")?.textContent).toBe("a = 5\n");
    expect(screen.getByText(/This file changed elsewhere/)).toBeTruthy();
    expect(getBuffer("w1", "src/a.py")?.base).toMatchObject({ sha256: "h1", newline: "crlf" });

    save.mockResolvedValue(saved());
    fireEvent.click(button("Save"));
    await screen.findByText("Saved.");
    expect(sentBody(save, 0)).toMatchObject({ expected_sha256: "h1", newline: "crlf" });
  });
});

describe("refusals", () => {
  const CODES: [string[], string][] = [
    [["trust_required"], "This folder isn't trusted, so it can't be saved to. Your edit is kept."],
    [
      ["workspace_changed", "root_unavailable"],
      "This folder changed. Reopen the Code view, then save again. Your edit is kept.",
    ],
    [["unsupported_filesystem"], "This folder's drive does not support saving from VIKI."],
    [
      ["read_only_target"],
      "This file is read-only. Clear Read-only in its Properties, then save again.",
    ],
    [
      ["unsafe_target", "unsafe_parent"],
      "VIKI can't save this file safely (it is a link or has other names). Edit it in VS Code.",
    ],
    [["missing_target"], "This file was moved or deleted. Your edit is kept."],
    [
      ["target_busy", "parent_busy", "stage_busy"],
      "Another app is using this file. Close it, then save again.",
    ],
    [["too_large"], "The saved file would be over 2 MiB, so it can't be saved."],
    [
      ["invalid_path", "private_path", "invalid_content", "invalid_body"],
      "VIKI can't save this file.",
    ],
    [
      ["publication_busy", "recovery_required"],
      "A previous save of this file needs recovery. Restart VIKI Agent, or forget its record under Stopped saves.",
    ],
    [["overloaded", "publication_limit", "vault_locked"], "Saving is busy. Try again in a moment."],
    [["something_new", "constructor"], "The file could not be saved. Your edit is kept."],
  ];
  const rows = CODES.flatMap(([codes, words]) => codes.map((code) => [code, words] as const));

  test.each(rows)("%s shows its words and keeps the edit", async (code, words) => {
    const blocked = code === "publication_busy" || code === "recovery_required";
    const { save, props } = setup();
    save.mockRejectedValue(new CodeApiError(code, "op9"));
    const area = await editor();
    type(area, "a = 2\n");
    fireEvent.click(button("Save"));
    expect(await screen.findByText(words)).toBeTruthy();
    expect(props.onBlocked).toHaveBeenCalledTimes(blocked ? 1 : 0);
    expect(area.value).toBe("a = 2\n");
    expect(getBuffer("w1", "src/a.py")?.text).toBe("a = 2\n");
    expect(button("Save").getAttribute("aria-disabled")).toBe("false");
  });

  /** jsdom exposes `navigator.platform` through a prototype getter, so this file's own property is
   * removed afterwards rather than restored — there is no own descriptor to put back (the pattern in
   * src/components/VaultBrowser/VaultBrowser.unit.test.tsx). */
  function setPlatform(value: string): () => void {
    Object.defineProperty(window.navigator, "platform", { value, configurable: true });
    return () => {
      delete (window.navigator as { platform?: string }).platform;
    };
  }

  // Off Windows, read_only_target names a POSIX remedy instead of the Properties dialog above.
  const READ_ONLY_TARGET_CASES: [platform: string, copy: string][] = [
    ["Win32", "This file is read-only. Clear Read-only in its Properties, then save again."],
    ["MacIntel", "This file is read-only or locked. Make it writable, then save again."],
    ["Linux x86_64", "This file is read-only or locked. Make it writable, then save again."],
  ];

  test.each(READ_ONLY_TARGET_CASES)(
    "names the platform's remedy for a read-only file on %s",
    async (platform, copy) => {
      const restore = setPlatform(platform);
      try {
        const { save } = setup();
        save.mockRejectedValue(new CodeApiError("read_only_target", "op9"));
        const area = await editor();
        type(area, "a = 2\n");
        fireEvent.click(button("Save"));
        expect(await screen.findByText(copy)).toBeTruthy();
      } finally {
        restore();
      }
    },
  );

  test("a network failure is the generic refusal", async () => {
    const { save } = setup();
    save.mockRejectedValue(new TypeError("fetch failed"));
    type(await editor(), "a = 2\n");
    fireEvent.click(button("Save"));
    expect(await screen.findByText("The file could not be saved. Your edit is kept.")).toBeTruthy();
  });

  test("a conflict with a cleanup pending blocks and points at the stopped saves", async () => {
    const { save, read, props } = setup();
    save.mockResolvedValue(conflict(true));
    type(await editor(), "a = 2\n");
    fireEvent.click(button("Save"));
    expect(
      await screen.findByText(
        "A previous save of this file needs recovery. Restart VIKI Agent, or forget its record under Stopped saves.",
      ),
    ).toBeTruthy();
    expect(props.onBlocked).toHaveBeenCalledTimes(1);
    expect(read).not.toHaveBeenCalled();
    expect(screen.queryByText("Disk version")).toBeNull();
  });
});

describe("buffers and toolbar", () => {
  test("each document edit keeps current RAM text without a clean transition", async () => {
    setup();
    const area = await editor();
    type(area, "a = 2\n");
    type(area, "a = 3\n");
    expect(getBuffer("w1", "src/a.py")?.text).toBe("a = 3\n");
    expect(disabled("Close")).toBe(true);
  });

  test("unmounting while dirty stores the editor's text and a remount restores it", async () => {
    const first = setup();
    const area = await editor();
    type(area, "a = 2\n");
    area.value = "a = 3\n"; // typed after the last dirty transition
    first.view.unmount();
    expect(getBuffer("w1", "src/a.py")).toMatchObject({
      text: "a = 3\n",
      base: { text: "a = 1\n", sha256: "h1", encoding: "utf-8", newline: "lf" },
    });
    expect(ed().handle.destroy).toHaveBeenCalled();

    setup();
    const again = await editor();
    expect(again.value).toBe("a = 3\n");
    expect(ed(1).options).toMatchObject({ text: "a = 3\n", base: "a = 1\n" });
    expect(disabled("Close")).toBe(true);
  });

  test("a clean unmount stores nothing", async () => {
    const { view } = setup();
    await editor();
    view.unmount();
    expect(getBuffer("w1", "src/a.py")).toBeUndefined();
  });

  test("Close is unavailable while dirty, with the reason, and calls onClose when clean", async () => {
    const { props } = setup();
    const area = await editor();
    fireEvent.click(button("Close"));
    expect(props.onClose).toHaveBeenCalledTimes(1);
    type(area, "a = 2\n");
    expect(disabled("Close")).toBe(true);
    expect(screen.getByText("Save or revert first")).toBeTruthy();
    fireEvent.click(button("Close"));
    expect(props.onClose).toHaveBeenCalledTimes(1);
  });

  test("Revert asks, and a refusal keeps the edit", async () => {
    setup();
    const area = await editor();
    type(area, "a = 2\n");
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    fireEvent.click(button("Revert"));
    expect(confirm).toHaveBeenCalledWith("Discard your changes?");
    expect(area.value).toBe("a = 2\n");
    expect(getBuffer("w1", "src/a.py")).toBeDefined();

    confirm.mockReturnValue(true);
    fireEvent.click(button("Revert"));
    expect(area.value).toBe("a = 1\n");
    expect(getBuffer("w1", "src/a.py")).toBeUndefined();
    expect(disabled("Close")).toBe(false);
  });

  test("an old success keeps text from a reopened editor", async () => {
    const first = setup();
    const pending = heldSave(first.save);
    type(await editor(), "a = 2\n");
    fireEvent.click(button("Save"));
    first.view.unmount();
    const second = setup();
    type(await editor(), "newer draft\n");
    second.view.unmount();
    pending.resolve(saved("h2"));
    await settle();
    expect(getBuffer("w1", "src/a.py")).toMatchObject({
      text: "newer draft\n",
      base: { text: "a = 2\n", sha256: "h2" },
    });
  });

  test("an old success leaves a newer verified base alone", async () => {
    const first = setup();
    const pending = heldSave(first.save);
    type(await editor(), "a = 2\n");
    fireEvent.click(button("Save"));
    first.view.unmount();
    const second = setup({ file: file({ text: "new disk\n", sha256: "h3" }) });
    type(await editor(), "newer draft\n");
    second.view.unmount();
    pending.resolve(saved("h2"));
    await settle();
    expect(getBuffer("w1", "src/a.py")).toMatchObject({
      text: "newer draft\n",
      base: { text: "new disk\n", sha256: "h3" },
    });
  });

  test("an old success cannot restore a discarded buffer or rebase its replacement", async () => {
    const first = setup();
    const pending = heldSave(first.save);
    type(await editor(), "a = 2\n");
    fireEvent.click(button("Save"));
    first.view.unmount();
    const second = setup();
    await editor();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    fireEvent.click(button("Revert"));
    expect(getBuffer("w1", "src/a.py")).toBeUndefined();
    second.view.unmount();
    const third = setup();
    type(await editor(), "replacement draft\n");
    third.view.unmount();
    pending.resolve(saved("h2"));
    await settle();
    expect(getBuffer("w1", "src/a.py")).toMatchObject({
      text: "replacement draft\n",
      base: { text: "a = 1\n", sha256: "h1" },
    });
  });

  test.each(["newer verified read", "explicit discard"])(
    "an old success leaves a mounted replacement's %s intact",
    async (control) => {
      const first = setup();
      const pending = heldSave(first.save);
      type(await editor(), "a = 2\n");
      fireEvent.click(button("Save"));
      first.view.unmount();
      const newer = control === "newer verified read";
      const second = setup({ file: newer ? file({ text: "new disk\n", sha256: "h3" }) : file() });
      const replacement = await editor();
      if (!newer) {
        vi.spyOn(window, "confirm").mockReturnValue(true);
        fireEvent.click(button("Revert"));
      }
      type(replacement, "replacement draft\n");
      pending.resolve(saved("h2"));
      await settle();
      expect(replacement.value).toBe("replacement draft\n");
      expect(ed(1).handle.setBase).not.toHaveBeenCalledWith("a = 2\n");
      expect(getBuffer("w1", "src/a.py")).toMatchObject({
        text: "replacement draft\n",
        base: { text: newer ? "new disk\n" : "a = 1\n", sha256: newer ? "h3" : "h1" },
      });
      second.save.mockResolvedValue(saved("h4"));
      fireEvent.click(button("Save"));
      await settle();
      expect(sentBody(second.save, 0)).toMatchObject({ expected_sha256: newer ? "h3" : "h1" });
    },
  );
});
