import { act, renderHook } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import {
  beginBufferSave,
  type CodeBuffer,
  claimEditor,
  confirmBufferBase,
  dirtyRefs,
  dropBuffer,
  editorOwner,
  getBuffer,
  noteSaved,
  putBuffer,
  releaseEditor,
  savedVersion,
  useCodeBuffers,
} from "./codeBuffers";
import { hasUnsavedWork, useHasUnsavedWork } from "./unsavedWork";

const base = { text: "a\nb\n", sha256: "0".repeat(64), encoding: "utf-8", newline: "lf" } as const;

function buffer(
  workspaceId: string,
  ref: string,
  text: string,
  over?: Partial<CodeBuffer["base"]>,
) {
  return { workspaceId, ref, base: { ...base, ...over }, text } satisfies CodeBuffer;
}

const touched: Array<[string, string]> = [];
function put(b: CodeBuffer): void {
  touched.push([b.workspaceId, b.ref]);
  putBuffer(b);
}

const claims: Array<[string, string, string]> = [];
function claim(workspaceId: string, ref: string, owner: string): boolean {
  claims.push([workspaceId, ref, owner]);
  return claimEditor(workspaceId, ref, owner);
}

afterEach(() => {
  for (const [workspaceId, ref] of touched.splice(0)) dropBuffer(workspaceId, ref);
  for (const [workspaceId, ref, owner] of claims.splice(0)) releaseEditor(workspaceId, ref, owner);
});

test("a clean put stores nothing", () => {
  put(buffer("w", "src/a.ts", "a\nb\n"));
  expect(getBuffer("w", "src/a.ts")).toBeUndefined();
  expect(dirtyRefs("w")).toEqual([]);
  expect(hasUnsavedWork()).toBe(false);
});

test("a CRLF base against the same LF text is clean", () => {
  put(buffer("w", "src/a.ts", "a\nb\n", { text: "a\r\nb\r\n", newline: "crlf" }));
  put(buffer("w", "src/b.ts", "a\nb\n", { text: "a\rb\r", newline: "cr" }));
  expect(dirtyRefs("w")).toEqual([]);
});

test("a dirty buffer sets the code owner and its drop clears it", () => {
  const reader = renderHook(() => useHasUnsavedWork());
  act(() => put(buffer("w", "src/a.ts", "changed")));
  expect(getBuffer("w", "src/a.ts")?.text).toBe("changed");
  expect(reader.result.current).toBe(true);

  act(() => dropBuffer("w", "src/a.ts"));
  expect(getBuffer("w", "src/a.ts")).toBeUndefined();
  expect(reader.result.current).toBe(false);
  reader.unmount();
});

test("putting the base text back removes the buffer", () => {
  put(buffer("w", "src/a.ts", "changed"));
  put(buffer("w", "src/a.ts", "a\nb\n"));
  expect(getBuffer("w", "src/a.ts")).toBeUndefined();
  expect(renderHook(() => useHasUnsavedWork()).result.current).toBe(false);
});

test("refs that differ only in case are separate buffers", () => {
  put(buffer("w", "Src/A.ts", "upper"));
  expect(getBuffer("w", "src/a.ts")).toBeUndefined();
  put(buffer("w", "src/a.ts", "lower"));
  expect(getBuffer("w", "Src/A.ts")?.text).toBe("upper");
  expect(getBuffer("w", "src/a.ts")?.text).toBe("lower");
  expect(dirtyRefs("w")).toEqual(["Src/A.ts", "src/a.ts"]);
  dropBuffer("w", "Src/A.ts");
  expect(dirtyRefs("w")).toEqual(["src/a.ts"]);
});

test("dirtyRefs is per workspace and sorted", () => {
  put(buffer("w1", "b.ts", "x"));
  put(buffer("w1", "a.ts", "x"));
  put(buffer("w2", "c.ts", "x"));
  expect(dirtyRefs("w1")).toEqual(["a.ts", "b.ts"]);
  expect(dirtyRefs("w2")).toEqual(["c.ts"]);
  expect(dirtyRefs("w3")).toEqual([]);
});

test("useCodeBuffers changes version when a buffer changes", () => {
  const { result } = renderHook(() => useCodeBuffers());
  const before = result.current;
  act(() => put(buffer("w", "src/a.ts", "changed")));
  expect(result.current).not.toBe(before);
});

test("one owner holds a file's editor until it releases it; each exact ref is separate", () => {
  expect(editorOwner("w", "src/a.ts")).toBeUndefined();
  expect(claim("w", "src/a.ts", "left")).toBe(true);
  expect(claim("w", "src/a.ts", "left")).toBe(true);
  expect(claim("w", "src/a.ts", "right")).toBe(false);
  expect(claim("w", "Src/A.ts", "right")).toBe(true);
  expect(claim("w2", "src/a.ts", "right")).toBe(true);
  releaseEditor("w", "src/a.ts", "right");
  expect(editorOwner("w", "src/a.ts")).toBe("left");
  releaseEditor("w", "src/a.ts", "left");
  expect(editorOwner("w", "src/a.ts")).toBeUndefined();
  expect(claim("w", "src/a.ts", "right")).toBe(true);
  expect(editorOwner("w", "src/a.ts")).toBe("right");
});

test("noteSaved counts the saves of each exact ref", () => {
  const before = savedVersion("w", "src/s.ts");
  noteSaved("w", "src/s.ts");
  noteSaved("w", "src/s.ts");
  expect(savedVersion("w", "src/s.ts")).toBe(before + 2);
  expect(savedVersion("w", "Src/S.ts")).toBe(0);
  expect(savedVersion("w2", "src/s.ts")).toBe(0);
});

test("a claim, a release and a save change version; a refused claim or a foreign release do not", () => {
  const { result } = renderHook(() => useCodeBuffers());
  const steps = [
    () => claim("w", "src/v.ts", "left"),
    () => claim("w", "src/v.ts", "right"),
    () => releaseEditor("w", "src/v.ts", "right"),
    () => releaseEditor("w", "src/v.ts", "left"),
    () => noteSaved("w", "src/v.ts"),
  ];
  const changed = steps.map((step) => {
    const before = result.current;
    act(() => {
      step();
    });
    return result.current !== before;
  });
  expect(changed).toEqual([true, false, false, true, true]);
});

test("overlapping saves retain an undo until every response or a verified fresh base", () => {
  const edit = buffer("w", "src/a.ts", "submitted");
  put(edit);
  const first = beginBufferSave(edit);
  const second = beginBufferSave(edit);
  put(buffer("w", "src/a.ts", base.text));
  expect(getBuffer("w", "src/a.ts")?.text).toBe(base.text);

  first(base, base.text);
  expect(getBuffer("w", "src/a.ts")?.text).toBe(base.text);
  second(); // The other write may have finished without an answer.
  put(buffer("w", "src/a.ts", base.text));
  expect(getBuffer("w", "src/a.ts")?.base).toEqual(base);
  confirmBufferBase(buffer("w", "src/a.ts", base.text));
  expect(getBuffer("w", "src/a.ts")?.base).toEqual(base);
  confirmBufferBase(buffer("w", "src/a.ts", base.text, { sha256: "new" }));
  expect(getBuffer("w", "src/a.ts")).toBeUndefined();
});

test("save retention belongs to the exact workspace and ref, and discard invalidates its owner", () => {
  const edit = buffer("w", "Src/A.ts", "submitted");
  put(edit);
  const finish = beginBufferSave(edit);
  put(buffer("w", "src/a.ts", base.text));
  put(buffer("other", "Src/A.ts", base.text));
  expect(getBuffer("w", "src/a.ts")).toBeUndefined();
  expect(getBuffer("other", "Src/A.ts")).toBeUndefined();

  dropBuffer("w", "Src/A.ts");
  const replacement = buffer("w", "Src/A.ts", "replacement", { sha256: "new" });
  put(replacement);
  expect(finish({ ...base, text: "submitted", sha256: "old-save" })).toBe(false);
  expect(getBuffer("w", "Src/A.ts")).toEqual(replacement);
});
