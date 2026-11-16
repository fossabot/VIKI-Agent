import { afterEach, expect, test, vi } from "vitest";
import { createEditor, type EditorHandle, indentUnitFor, languageKind } from "./codemirror";

test.each([
  ["a.py", "python"],
  ["a.pyi", "python"],
  ["a.pyw", "python"],
  ["a.js", "javascript"],
  ["a.mjs", "javascript"],
  ["a.cjs", "javascript"],
  ["a.jsx", "jsx"],
  ["a.ts", "typescript"],
  ["a.mts", "typescript"],
  ["a.cts", "typescript"],
  ["a.tsx", "tsx"],
  ["a.json", "json"],
])("languageKind(%s) is %s", (ref, kind) => {
  expect(languageKind(`src/${ref}`)).toBe(kind);
});

test("languageKind folds the suffix's case", () => {
  expect(languageKind("src/A.PY")).toBe("python");
  expect(languageKind("src/A.Mjs")).toBe("javascript");
  expect(languageKind("src/A.TSX")).toBe("tsx");
  expect(languageKind("package.JSON")).toBe("json");
});

test("languageKind is null for markdown, an unknown suffix and no suffix", () => {
  expect(languageKind("README.md")).toBeNull();
  expect(languageKind("src/Makefile")).toBeNull();
  expect(languageKind("src.py/Makefile")).toBeNull();
  expect(languageKind("notes.txt")).toBeNull();
});

test.each([
  ["tabs", "\tfoo\n\t\tbar\n", null, "\t"],
  ["a Makefile", "all:\n\techo hi\n", null, "\t"],
  [
    "four-space javascript",
    "function f() {\n    if (x) {\n        y();\n    }\n}\n",
    "javascript",
    "    ",
  ],
  ["two-space steps", "a {\n  b\n    c\n", null, "  "],
  ["python default", "def f():", "python", "    "],
  ["plain default", "x", null, "  "],
  ["json default", "{}", "json", "  "],
  ["JSDoc then code", "/**\n * doc\n */\nfunction f() {\n    return 1;\n}\n", "typescript", "    "],
  ["a tie takes the smaller step", "a\n  b\nc\n    d\n", null, "  "],
  ["spaces outnumbering tabs", "  a\n  b\n\tc\n", null, "  "],
  ["CRLF", "if x:\r\n    y\r\n", "python", "    "],
  ["blank lines do not reset", "if x:\n    a\n\n    b\n", "python", "    "],
] as const)("indentUnitFor %s", (_name, text, kind, unit) => {
  expect(indentUnitFor(text, kind)).toBe(unit);
});

// The editor itself, in jsdom: no layout, so only what the handle and the document show.
let handle: EditorHandle | undefined;
afterEach(() => {
  handle?.destroy();
  handle = undefined;
});

async function open(
  over: { text?: string; base?: string; ref?: string; onChange?: (text: string) => void } = {},
) {
  const parent = document.createElement("div");
  document.body.append(parent);
  const onDirty = vi.fn();
  const onSave = vi.fn();
  handle = await createEditor({
    parent,
    text: over.text ?? "x = 1\n",
    base: over.base ?? over.text ?? "x = 1\n",
    ref: over.ref ?? "a.py",
    wrap: false,
    fontSize: 13,
    label: "a.py",
    onDirty,
    ...(over.onChange ? { onChange: over.onChange } : {}),
    onSave,
  });
  return { parent, onDirty, onSave, editor: handle };
}

test("dirty is reported on transitions only, and setBase re-reports", async () => {
  const { editor, onDirty } = await open();
  expect(onDirty).not.toHaveBeenCalled();
  editor.setText("x = 2\n");
  editor.setText("x = 3\n");
  expect(onDirty.mock.calls).toEqual([[true]]);
  editor.setText("x = 1\n");
  expect(onDirty.mock.calls).toEqual([[true], [false]]);
  editor.setText("x = 4\n");
  editor.setBase("x = 4\n");
  expect(onDirty.mock.calls).toEqual([[true], [false], [true], [false]]);
  expect(editor.text()).toBe("x = 4\n");
});

test("document changes report current text even while dirty stays true", async () => {
  const onChange = vi.fn();
  const { editor, onDirty } = await open({ onChange });
  expect(onChange).not.toHaveBeenCalled();
  editor.setText("x = 2\n");
  editor.setText("x = 3\r\n");
  expect(onChange.mock.calls).toEqual([["x = 2\n"], ["x = 3\n"]]);
  expect(onDirty.mock.calls).toEqual([[true]]);
  editor.setBase("x = 3\n");
  editor.setWrap(true);
  editor.setFontSize(17);
  expect(onChange).toHaveBeenCalledTimes(2);
  expect(onDirty.mock.calls).toEqual([[true], [false]]);
});

test("a recovered buffer that differs from its base opens dirty", async () => {
  const { onDirty } = await open({ text: "x = 2\n", base: "x = 1\n" });
  expect(onDirty.mock.calls).toEqual([[true]]);
});

test("Mod-s calls onSave and the page's save shortcut is suppressed", async () => {
  const { parent, onSave } = await open();
  const press = new KeyboardEvent("keydown", {
    key: "s",
    code: "KeyS",
    keyCode: 83,
    ctrlKey: true,
    bubbles: true,
    cancelable: true,
  });
  parent.querySelector(".cm-content")?.dispatchEvent(press);
  expect(onSave).toHaveBeenCalledTimes(1);
  expect(press.defaultPrevented).toBe(true);
});

test.each([
  ["Makefile", "\titem", "\titem\n\t"],
  ["a.py", "def f():", "def f():\n    "],
])("Enter at the end of %s keeps the file's indent unit", async (ref, text, expected) => {
  const { parent, editor } = await open({ ref, text });
  const content = parent.querySelector(".cm-content");
  const press = (init: KeyboardEventInit) =>
    content?.dispatchEvent(
      new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init }),
    );
  press({ key: "End", code: "End", keyCode: 35, ctrlKey: true });
  press({ key: "Enter", code: "Enter", keyCode: 13 });
  expect(editor.text()).toBe(expected);
});

test("a Python file loads its grammar and the editing area carries its label", async () => {
  const { parent } = await open({ text: "def f():\n    return None\n" });
  expect(parent.querySelector(".cm-content")?.getAttribute("aria-label")).toBe("a.py");
  expect(parent.querySelector(".tok-keyword")).not.toBeNull();
});

test("a file with no grammar still opens", async () => {
  const { parent, editor } = await open({ text: "plain\n", ref: "notes.txt" });
  expect(parent.querySelector(".cm-content")).not.toBeNull();
  expect(editor.text()).toBe("plain\n");
});
