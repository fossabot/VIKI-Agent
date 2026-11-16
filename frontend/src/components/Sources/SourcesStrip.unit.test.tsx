/**
 * Unit tests for components/Sources/SourcesStrip.tsx: the three null-without-rendering guards (no
 * env, a running turn, no blocks), the "nothing to show" null once those pass, the "Read" label
 * and chip row with its eight-source disclosure, the vault "No vault sources" badge (and that a
 * note read suppresses it, and leaving the vault removes it), and the "Save as note" button's
 * busy/opened/no_room/unavailable/thrown-error states, including the ignored second click and the
 * message a new attempt clears.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { Block, TextBlock, ToolBlock, ToolRowView, Turn } from "../../state/view";
import { SourcesStrip } from "./SourcesStrip";
import { publishSourcesEnv, resetSourcesEnv, type SourcesEnv } from "./sourcesEnv";

const SID = "11111111-1111-1111-1111-111111111111";

type SaveOutcome = "opened" | "no_room" | "unavailable";

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (error: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function turn(blocks: Block[], status: Turn["status"] = "success", id = "t1"): Turn {
  return { id, user: null, blocks, status, usage: null, durationMs: 0, history: false };
}

function textBlock(text: string, id = "b1"): TextBlock {
  return { kind: "text", id, text, done: true };
}

function readRow(id: string, path: string): ToolRowView {
  return {
    id,
    tool: "Read",
    row: { head: "Read", rest: path, tail: "" },
    done: true,
    isError: false,
    summary: "read",
    preview: "",
    artifactStart: {
      artifact_id: id,
      kind: "file",
      operation: "read",
      path,
      path_state: "workspace_relative",
    },
    artifactResult: {
      artifact_id: id,
      observation: "result",
      byte_size: 1,
      sha256: "",
      preview: "",
      truncated: false,
      source_revision: "",
      detail_ref: null,
    },
  };
}

function toolRow(id: string, tool: string, rest: string): ToolRowView {
  return {
    id,
    tool,
    row: { head: tool, rest, tail: "" },
    done: true,
    isError: false,
    summary: "",
    preview: "",
  };
}

function toolsBlock(...rows: ToolRowView[]): ToolBlock {
  return { kind: "tools", id: rows[0]?.id ?? "tools", rows };
}

function fakeEnv(overrides: Partial<SourcesEnv> = {}): SourcesEnv {
  return {
    workspaceId: undefined,
    inVault: () => false,
    openNote: () => {},
    openFolder: () => {},
    openUrl: () => {},
    ...overrides,
  };
}

function saveSpy(): {
  saveAsNote: (sid: string, t: Turn) => Promise<SaveOutcome>;
  calls: { sid: string; turn: Turn }[];
  pending: Deferred<SaveOutcome>[];
} {
  const calls: { sid: string; turn: Turn }[] = [];
  const pending: Deferred<SaveOutcome>[] = [];
  return {
    calls,
    pending,
    saveAsNote(sid, t) {
      calls.push({ sid, turn: t });
      const d = deferred<SaveOutcome>();
      pending.push(d);
      return d.promise;
    },
  };
}

async function settle(): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  cleanup();
  resetSourcesEnv();
  vi.useRealTimers();
});

// --- null guards ----------------------------------------------------------------------------

test("renders nothing before a sources env is published", () => {
  const { container } = render(
    <SourcesStrip turn={turn([toolsBlock(readRow("r1", "a.md"))])} sessionId={SID} />,
  );
  expect(container.firstChild).toBeNull();
});

test("renders nothing while the turn is running", () => {
  publishSourcesEnv(fakeEnv({ inVault: () => true }));
  const { container } = render(
    <SourcesStrip turn={turn([toolsBlock(readRow("r1", "a.md"))], "running")} sessionId={SID} />,
  );
  expect(container.firstChild).toBeNull();
});

test("renders nothing with no blocks", () => {
  publishSourcesEnv(fakeEnv({ inVault: () => true }));
  const { container } = render(<SourcesStrip turn={turn([])} sessionId={SID} />);
  expect(container.firstChild).toBeNull();
});

test("renders nothing when there is nothing to show", () => {
  publishSourcesEnv(fakeEnv());
  const { container } = render(<SourcesStrip turn={turn([textBlock("hello")])} sessionId={SID} />);
  expect(container.firstChild).toBeNull();
});

// --- chips ------------------------------------------------------------------------------------

test("shows the Read label and a chip for a vault note read", () => {
  publishSourcesEnv(fakeEnv({ inVault: () => true }));
  render(
    <SourcesStrip
      turn={turn([toolsBlock(readRow("r1", "30-knowledge/foo.md"))])}
      sessionId={SID}
    />,
  );
  const group = screen.getByRole("group", { name: "Read for this reply" });
  expect(group.className).toBe("reply-sources");
  expect(screen.getByText("Read")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Read note 30-knowledge/foo.md" })).toBeTruthy();
});

test("shows at most 8 chips, then a +N more label", () => {
  publishSourcesEnv(fakeEnv({ inVault: () => true }));
  const rows = Array.from({ length: 10 }, (_, i) => readRow(`r${i}`, `n/f${i}.md`));
  render(<SourcesStrip turn={turn([toolsBlock(...rows)])} sessionId={SID} />);
  expect(screen.getAllByRole("button", { name: /^Read note / })).toHaveLength(8);
  expect(screen.getByText("+2 more")).toBeTruthy();
  expect(screen.queryByText("+1 more")).toBeNull();
});

test("reveals remaining sources without reading them and preserves the disclosure focus", () => {
  const peek = vi.fn(() => new Promise<never>(() => {}));
  const resolve = vi.fn(async () => new Map());
  const openNote = vi.fn();
  publishSourcesEnv(fakeEnv({ inVault: () => true, peek, resolve, openNote }));
  const refs = Array.from({ length: 10 }, (_, i) => `n/f${i}.md`);
  render(
    <SourcesStrip
      turn={turn([toolsBlock(...refs.map((ref, i) => readRow(`r${i}`, ref)))])}
      sessionId={SID}
    />,
  );
  const toggle = screen.getByRole("button", { name: "Show all 10 sources" });
  const controlled = document.getElementById(toggle.getAttribute("aria-controls") ?? "");
  if (!controlled) throw new Error("missing the controlled source list");
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  expect(
    [...controlled.querySelectorAll(".source-strip-ref")].map((ref) => ref.textContent),
  ).toEqual(refs.slice(0, 8));

  toggle.focus();
  fireEvent.click(toggle);
  expect(screen.getByRole("button", { name: "Show fewer sources" })).toBe(toggle);
  expect(toggle.getAttribute("aria-expanded")).toBe("true");
  expect(document.activeElement).toBe(toggle);
  expect(
    [...controlled.querySelectorAll(".source-strip-ref")].map((ref) => ref.textContent),
  ).toEqual(refs);
  expect(peek).not.toHaveBeenCalled();
  expect(resolve).not.toHaveBeenCalled();
  expect(openNote).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole("button", { name: "Read note n/f9.md" }));
  expect(openNote).toHaveBeenCalledExactlyOnceWith("n/f9.md");
  fireEvent.click(toggle);
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  expect(document.activeElement).toBe(toggle);
  expect(screen.getAllByRole("button", { name: /^Read note / })).toHaveLength(8);
  expect(peek).not.toHaveBeenCalled();
  expect(resolve).not.toHaveBeenCalled();
});

test("shows exact refs for notes with the same filename", () => {
  publishSourcesEnv(fakeEnv({ inVault: () => true }));
  const refs = ["projects/claim.md", "archive/claim.md"];
  const { container } = render(
    <SourcesStrip
      turn={turn([toolsBlock(...refs.map((ref, i) => readRow(`r${i}`, ref)))])}
      sessionId={SID}
    />,
  );
  expect(
    [...container.querySelectorAll(".source-strip-ref")].map((ref) => ref.textContent),
  ).toEqual(refs);
  expect(screen.getAllByText("claim", { selector: "button" })).toHaveLength(2);
});

test("resets the source disclosure when the turn or session changes", () => {
  publishSourcesEnv(fakeEnv({ inVault: () => true }));
  const rows = Array.from({ length: 10 }, (_, i) => readRow(`r${i}`, `n/f${i}.md`));
  const { rerender } = render(<SourcesStrip turn={turn([toolsBlock(...rows)])} sessionId={SID} />);
  fireEvent.click(screen.getByRole("button", { name: "Show all 10 sources" }));
  rerender(<SourcesStrip turn={turn([toolsBlock(...rows)], "success", "t2")} sessionId={SID} />);
  expect(
    screen.getByRole("button", { name: "Show all 10 sources" }).getAttribute("aria-expanded"),
  ).toBe("false");
  fireEvent.click(screen.getByRole("button", { name: "Show all 10 sources" }));
  rerender(
    <SourcesStrip turn={turn([toolsBlock(...rows)], "success", "t2")} sessionId="another" />,
  );
  expect(screen.getAllByRole("button", { name: /^Read note / })).toHaveLength(8);
  rerender(<SourcesStrip turn={turn([toolsBlock(...rows)], "success", "t2")} sessionId={SID} />);
  expect(screen.getAllByRole("button", { name: /^Read note / })).toHaveLength(8);
});

// --- the vault badge ----------------------------------------------------------------------------

test("shows the No vault sources badge alongside a non-note chip", () => {
  publishSourcesEnv(fakeEnv({ inVault: () => true }));
  render(
    <SourcesStrip
      turn={turn([toolsBlock(toolRow("g1", "Grep", "confidence in 30-knowledge"))])}
      sessionId={SID}
    />,
  );
  const badge = screen.getByText("No vault sources");
  expect(badge.className).toBe("sources-badge");
  expect(badge.getAttribute("title")).toBe("VIKI read no vault note for this reply.");
  // No `resolve` in this fake env, so SourceChip falls back to its non-interactive span (its own
  // documented behaviour) — still proves the search source rendered as a chip alongside the badge.
  const chip = screen.getByText("confidence in 30-knowledge");
  expect(chip.getAttribute("title")).toBe("Searched confidence in 30-knowledge");
});

test("shows no badge once a note was read", () => {
  publishSourcesEnv(fakeEnv({ inVault: () => true }));
  render(<SourcesStrip turn={turn([toolsBlock(readRow("r1", "a.md"))])} sessionId={SID} />);
  expect(screen.queryByText("No vault sources")).toBeNull();
});

test("shows no badge outside a vault", () => {
  const { saveAsNote } = saveSpy();
  publishSourcesEnv(fakeEnv({ inVault: () => false, saveAsNote }));
  render(<SourcesStrip turn={turn([textBlock("hello")])} sessionId={SID} />);
  expect(screen.queryByText("No vault sources")).toBeNull();
  expect(screen.getByRole("button", { name: "Save as note" })).toBeTruthy();
});

// --- the Save as note button: when it shows -------------------------------------------------

test("shows Save as note when available and the reply has words", () => {
  const { saveAsNote } = saveSpy();
  publishSourcesEnv(fakeEnv({ saveAsNote }));
  render(<SourcesStrip turn={turn([textBlock("Hello there")])} sessionId={SID} />);
  expect(screen.getByRole("button", { name: "Save as note" })).toBeTruthy();
});

test("hides Save as note without env.saveAsNote", () => {
  publishSourcesEnv(fakeEnv({ inVault: () => true }));
  render(<SourcesStrip turn={turn([toolsBlock(readRow("r1", "a.md"))])} sessionId={SID} />);
  expect(screen.queryByRole("button", { name: "Save as note" })).toBeNull();
});

test("hides Save as note when the reply has no words", () => {
  const { saveAsNote } = saveSpy();
  publishSourcesEnv(fakeEnv({ saveAsNote, inVault: () => true }));
  render(<SourcesStrip turn={turn([toolsBlock(readRow("r1", "a.md"))])} sessionId={SID} />);
  expect(screen.queryByRole("button", { name: "Save as note" })).toBeNull();
});

// --- the Save as note button: click flow ----------------------------------------------------

test("click shows Preparing… and aria-busy, then clears on opened", async () => {
  const { saveAsNote, calls, pending } = saveSpy();
  publishSourcesEnv(fakeEnv({ saveAsNote }));
  const t = turn([textBlock("Hello")]);
  render(<SourcesStrip turn={t} sessionId={SID} />);
  const button = screen.getByRole("button", { name: "Save as note" });

  fireEvent.click(button);
  expect(button.getAttribute("aria-busy")).toBe("true");
  expect(screen.getByRole("button", { name: "Preparing…" })).toBeTruthy();
  expect(calls).toEqual([{ sid: SID, turn: t }]);

  pending[0]?.resolve("opened");
  await settle();
  expect(screen.getByRole("button", { name: "Save as note" })).toBeTruthy();
  expect(screen.queryByRole("status")).toBeNull();
});

test("no_room shows its exact message", async () => {
  const { saveAsNote, pending } = saveSpy();
  publishSourcesEnv(fakeEnv({ saveAsNote }));
  render(<SourcesStrip turn={turn([textBlock("Hello")])} sessionId={SID} />);
  fireEvent.click(screen.getByRole("button", { name: "Save as note" }));
  pending[0]?.resolve("no_room");
  await settle();
  expect(screen.getByRole("status").textContent).toBe(
    "No room for another draft in this window. Close a draft, then try again.",
  );
});

test("unavailable shows its exact message", async () => {
  const { saveAsNote, pending } = saveSpy();
  publishSourcesEnv(fakeEnv({ saveAsNote }));
  render(<SourcesStrip turn={turn([textBlock("Hello")])} sessionId={SID} />);
  fireEvent.click(screen.getByRole("button", { name: "Save as note" }));
  pending[0]?.resolve("unavailable");
  await settle();
  expect(screen.getByRole("status").textContent).toBe("Couldn't prepare a note from this reply.");
});

test("a thrown error shows the same unavailable message", async () => {
  const { saveAsNote, pending } = saveSpy();
  publishSourcesEnv(fakeEnv({ saveAsNote }));
  render(<SourcesStrip turn={turn([textBlock("Hello")])} sessionId={SID} />);
  fireEvent.click(screen.getByRole("button", { name: "Save as note" }));
  pending[0]?.reject(new Error("boom"));
  await settle();
  expect(screen.getByRole("status").textContent).toBe("Couldn't prepare a note from this reply.");
});

test("ignores a second click while the first is in flight", async () => {
  const { saveAsNote, calls, pending } = saveSpy();
  publishSourcesEnv(fakeEnv({ saveAsNote }));
  render(<SourcesStrip turn={turn([textBlock("Hello")])} sessionId={SID} />);
  const button = screen.getByRole("button", { name: "Save as note" });

  fireEvent.click(button);
  fireEvent.click(button);
  expect(calls).toHaveLength(1);

  pending[0]?.resolve("opened");
  await settle();
});

test("a new attempt clears the previous message immediately", async () => {
  const { saveAsNote, pending } = saveSpy();
  publishSourcesEnv(fakeEnv({ saveAsNote }));
  render(<SourcesStrip turn={turn([textBlock("Hello")])} sessionId={SID} />);

  fireEvent.click(screen.getByRole("button", { name: "Save as note" }));
  pending[0]?.resolve("no_room");
  await settle();
  expect(screen.queryByRole("status")).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "Save as note" }));
  expect(screen.queryByRole("status")).toBeNull();

  pending[1]?.resolve("opened");
  await settle();
});
