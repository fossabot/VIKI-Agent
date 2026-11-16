/**
 * SourceChip covers three kinds against a fake SourcesEnv and fake timers: a note chip (click
 * opens its exact ref; hover/focus past the 250 ms delay loads `peek` once and reuses it on
 * reopen; Esc/leave hide it) and web/search chips (click calls `resolve`, ignoring a second click
 * while the first is in flight; without `resolve` both fall back to a plain, non-interactive
 * `<span>`). Security-relevant cases prove the chip never opens the raw label it displays — only
 * a note's own `ref` or whatever `resolve` hands back — and that hostile text in any field renders
 * as a literal text node, never markup (the same technique RightRail/RunPeek's own tests use).
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { ToolSource } from "../../clients/conversations/conversationSearchApi";
import type { NotePeek } from "../../state/notePeek";
import type { Source } from "../../state/sources";
import { OPEN_DELAY_MS, SourceChip } from "./SourceChip";
import { publishSourcesEnv, resetSourcesEnv, type SourcesEnv } from "./sourcesEnv";

const SID = "11111111-1111-1111-1111-111111111111";

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

interface EnvCalls {
  openNote: string[];
  openFolder: string[];
  openUrl: string[];
}

function fakeEnv(overrides: Partial<SourcesEnv> = {}): { env: SourcesEnv; calls: EnvCalls } {
  const calls: EnvCalls = { openNote: [], openFolder: [], openUrl: [] };
  const env: SourcesEnv = {
    workspaceId: undefined,
    inVault: () => true,
    openNote: (ref) => calls.openNote.push(ref),
    openFolder: (folder) => calls.openFolder.push(folder),
    openUrl: (url) => calls.openUrl.push(url),
    ...overrides,
  };
  return { env, calls };
}

function peekSpy(): {
  peek: (ref: string, signal?: AbortSignal) => Promise<NotePeek>;
  calls: { ref: string; signal: AbortSignal | undefined }[];
  pending: Deferred<NotePeek>[];
} {
  const calls: { ref: string; signal: AbortSignal | undefined }[] = [];
  const pending: Deferred<NotePeek>[] = [];
  return {
    calls,
    pending,
    peek(ref, signal) {
      calls.push({ ref, signal });
      const d = deferred<NotePeek>();
      pending.push(d);
      return d.promise;
    },
  };
}

function resolveSpy(): {
  resolve: (
    sid: string,
    ids: readonly string[],
    signal?: AbortSignal,
  ) => Promise<ReadonlyMap<string, ToolSource>>;
  calls: { sid: string; ids: readonly string[]; signal: AbortSignal | undefined }[];
  pending: Deferred<ReadonlyMap<string, ToolSource>>[];
} {
  const calls: { sid: string; ids: readonly string[]; signal: AbortSignal | undefined }[] = [];
  const pending: Deferred<ReadonlyMap<string, ToolSource>>[] = [];
  return {
    calls,
    pending,
    resolve(sid, ids, signal) {
      calls.push({ sid, ids, signal });
      const d = deferred<ReadonlyMap<string, ToolSource>>();
      pending.push(d);
      return d.promise;
    },
  };
}

function noteSource(ref: string, label = ref, toolUseId = "n1", agent = ""): Source {
  return { kind: "note", key: `note:${ref}`, label, ref, toolUseId, tool: "Read", agent };
}
function webSource(label: string, toolUseId = "w1", agent = ""): Source {
  return {
    kind: "web",
    key: `web:${toolUseId}`,
    label,
    ref: "",
    toolUseId,
    tool: "WebFetch",
    agent,
  };
}
function searchSource(label: string, toolUseId = "s1", agent = ""): Source {
  return {
    kind: "search",
    key: `search:${toolUseId}`,
    label,
    ref: "",
    toolUseId,
    tool: "Grep",
    agent,
  };
}

async function settle(): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
}
async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
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

test("renders nothing before a sources env is published", () => {
  const { container } = render(<SourceChip source={noteSource("g.md")} sessionId={SID} />);
  expect(container.firstChild).toBeNull();
});

test("note: is a button with data-kind, and click opens the exact ref", () => {
  const { env, calls } = fakeEnv();
  publishSourcesEnv(env);
  render(<SourceChip source={noteSource("30-knowledge/foo.md")} sessionId={SID} />);
  const button = screen.getByRole("button", { name: "Read note 30-knowledge/foo.md" });
  expect(button.getAttribute("data-kind")).toBe("note");
  fireEvent.click(button);
  expect(calls.openNote).toEqual(["30-knowledge/foo.md"]);
});

test("note: a subagent's read shows a dim agent suffix and names the agent in the accessible name", () => {
  const { env } = fakeEnv();
  publishSourcesEnv(env);
  const source = noteSource("30-knowledge/foo.md", "30-knowledge/foo.md", "n1", "researcher");
  render(<SourceChip source={source} sessionId={SID} />);
  const button = screen.getByRole("button", {
    name: "Read note 30-knowledge/foo.md, by researcher",
  });
  expect(button.textContent).toBe("30-knowledge/foo.md · researcher");
  expect(document.querySelector(".source-chip-agent")?.textContent).toBe(" · researcher");
});

test("note: VIKI's own read (no agent) shows no agent suffix", () => {
  const { env } = fakeEnv();
  publishSourcesEnv(env);
  render(<SourceChip source={noteSource("30-knowledge/bar.md")} sessionId={SID} />);
  const button = screen.getByRole("button", { name: "Read note 30-knowledge/bar.md" });
  expect(button.textContent).toBe("30-knowledge/bar.md");
  expect(document.querySelector(".source-chip-agent")).toBeNull();
});

test("note: the agent suffix sits outside the truncating label span", () => {
  const { env } = fakeEnv();
  publishSourcesEnv(env);
  const longRef = "30-knowledge/a-note-title-long-enough-that-a-narrow-column-would-truncate-it.md";
  const source = noteSource(longRef, longRef, "n1", "researcher");
  render(<SourceChip source={source} sessionId={SID} />);
  const label = document.querySelector(".source-chip-label");
  const agent = document.querySelector(".source-chip-agent");
  expect(label?.textContent).toBe(longRef);
  expect(agent?.textContent).toBe(" · researcher");
  expect(label?.contains(agent)).toBe(false);
});

test("note: a hover shorter than the delay opens no card and calls peek nothing", async () => {
  const { peek, calls } = peekSpy();
  const { env } = fakeEnv({ peek });
  publishSourcesEnv(env);
  render(<SourceChip source={noteSource("a.md")} sessionId={SID} />);
  const button = screen.getByRole("button");
  fireEvent.mouseEnter(button);
  await advance(OPEN_DELAY_MS - 1);
  expect(screen.queryByRole("tooltip")).toBeNull();
  fireEvent.mouseLeave(button);
  await advance(OPEN_DELAY_MS);
  expect(screen.queryByRole("tooltip")).toBeNull();
  expect(calls).toHaveLength(0);
});

test("note: hover past the delay opens the card and loads peek once, reused on reopen", async () => {
  const { peek, calls, pending } = peekSpy();
  const { env } = fakeEnv({ peek });
  publishSourcesEnv(env);
  render(<SourceChip source={noteSource("a.md")} sessionId={SID} />);
  const button = screen.getByRole("button");

  fireEvent.mouseEnter(button);
  await advance(OPEN_DELAY_MS);
  expect(screen.getByRole("tooltip")).toBeTruthy();
  expect(screen.getByText("Reading…")).toBeTruthy();
  expect(calls).toHaveLength(1);
  expect(calls[0]?.ref).toBe("a.md");

  pending[0]?.resolve({
    title: "Foo",
    summary: "A short summary.",
    modified: "2026-10-01",
    confidence: "high",
  });
  await settle();

  expect(screen.getByText("Foo")).toBeTruthy();
  expect(screen.getByText("A short summary.")).toBeTruthy();
  expect(screen.getByText("Modified 2026-10-01")).toBeTruthy();
  expect(screen.getByText("Confidence high")).toBeTruthy();
  expect(document.querySelector(".source-chip-ref")?.textContent).toBe("a.md");

  fireEvent.mouseLeave(button);
  expect(screen.queryByRole("tooltip")).toBeNull();

  fireEvent.mouseEnter(button);
  await advance(OPEN_DELAY_MS);
  expect(screen.getByText("Foo")).toBeTruthy();
  expect(calls).toHaveLength(1);
});

test("note: no summary, modified or confidence shows only the fallback line", async () => {
  const { peek, pending } = peekSpy();
  const { env } = fakeEnv({ peek });
  publishSourcesEnv(env);
  render(<SourceChip source={noteSource("b.md")} sessionId={SID} />);
  fireEvent.mouseEnter(screen.getByRole("button"));
  await advance(OPEN_DELAY_MS);
  pending[0]?.resolve({ title: "Bare note", summary: null, modified: null, confidence: null });
  await settle();
  expect(screen.getByText("No summary section.")).toBeTruthy();
  expect(screen.queryByText(/^Modified /)).toBeNull();
  expect(screen.queryByText(/^Confidence /)).toBeNull();
});

test("note: a failed peek shows the error note", async () => {
  const { peek, pending } = peekSpy();
  const { env } = fakeEnv({ peek });
  publishSourcesEnv(env);
  render(<SourceChip source={noteSource("c.md")} sessionId={SID} />);
  fireEvent.mouseEnter(screen.getByRole("button"));
  await advance(OPEN_DELAY_MS);
  pending[0]?.reject(new Error("unavailable"));
  await settle();
  expect(screen.getByText("Couldn't read this note.")).toBeTruthy();
});

test("note: Escape hides the open card", async () => {
  const { peek, pending } = peekSpy();
  const { env } = fakeEnv({ peek });
  publishSourcesEnv(env);
  render(<SourceChip source={noteSource("d.md")} sessionId={SID} />);
  const button = screen.getByRole("button");
  fireEvent.mouseEnter(button);
  await advance(OPEN_DELAY_MS);
  pending[0]?.resolve({ title: "D", summary: null, modified: null, confidence: null });
  await settle();
  expect(screen.getByRole("tooltip")).toBeTruthy();
  fireEvent.keyDown(button, { key: "Escape" });
  expect(screen.queryByRole("tooltip")).toBeNull();
});

test("note: Escape on the open card never reaches a document keydown listener (voice/useHandsFree.ts's own Esc exit)", async () => {
  const { peek, pending } = peekSpy();
  const { env } = fakeEnv({ peek });
  publishSourcesEnv(env);
  render(<SourceChip source={noteSource("d2.md")} sessionId={SID} />);
  const button = screen.getByRole("button");
  fireEvent.mouseEnter(button);
  await advance(OPEN_DELAY_MS);
  pending[0]?.resolve({ title: "D2", summary: null, modified: null, confidence: null });
  await settle();
  expect(screen.getByRole("tooltip")).toBeTruthy();

  const documentKeyDown = vi.fn();
  document.addEventListener("keydown", documentKeyDown);
  try {
    fireEvent.keyDown(button, { key: "Escape" });
  } finally {
    document.removeEventListener("keydown", documentKeyDown);
  }
  expect(documentKeyDown).not.toHaveBeenCalled();
});

test("note: Escape with no card open or pending reaches a document keydown listener (hands-free, SessionLimitToast)", () => {
  const { env } = fakeEnv();
  publishSourcesEnv(env);
  render(<SourceChip source={noteSource("d3.md")} sessionId={SID} />);
  const button = screen.getByRole("button");

  const documentKeyDown = vi.fn();
  document.addEventListener("keydown", documentKeyDown);
  try {
    fireEvent.keyDown(button, { key: "Escape" });
  } finally {
    document.removeEventListener("keydown", documentKeyDown);
  }
  expect(documentKeyDown).toHaveBeenCalledTimes(1);
});

test("note: without peek, hovering opens no card", async () => {
  const { env } = fakeEnv();
  publishSourcesEnv(env);
  render(<SourceChip source={noteSource("e.md")} sessionId={SID} />);
  fireEvent.mouseEnter(screen.getByRole("button"));
  await advance(OPEN_DELAY_MS);
  expect(screen.queryByRole("tooltip")).toBeNull();
});

test("note: unmounting aborts an in-flight peek", async () => {
  const { peek, calls } = peekSpy();
  const { env } = fakeEnv({ peek });
  publishSourcesEnv(env);
  const { unmount } = render(<SourceChip source={noteSource("f.md")} sessionId={SID} />);
  fireEvent.mouseEnter(screen.getByRole("button"));
  await advance(OPEN_DELAY_MS);
  expect(calls[0]?.signal?.aborted).toBe(false);
  unmount();
  expect(calls[0]?.signal?.aborted).toBe(true);
});

test("note: hostile text in every peeked field renders as literal text, not an element", async () => {
  const hostile = "<img src=x onerror=alert(1)>";
  const { peek, pending } = peekSpy();
  const { env } = fakeEnv({ peek });
  publishSourcesEnv(env);
  render(<SourceChip source={noteSource(hostile, hostile)} sessionId={SID} />);
  fireEvent.mouseEnter(screen.getByRole("button"));
  await advance(OPEN_DELAY_MS);
  pending[0]?.resolve({ title: hostile, summary: hostile, modified: hostile, confidence: hostile });
  await settle();
  expect(document.querySelector("img")).toBeNull();
  expect(screen.getAllByText(hostile).length).toBeGreaterThan(0);
});

test("web: without resolve renders a plain, non-interactive span with a title", () => {
  const hostile = "<img src=x onerror=alert(1)>https://example.com";
  const { env } = fakeEnv();
  publishSourcesEnv(env);
  render(<SourceChip source={webSource(hostile)} sessionId={SID} />);
  expect(screen.queryByRole("button")).toBeNull();
  expect(document.querySelector("img")).toBeNull();
  const span = screen.getByText(hostile);
  expect(span.tagName).toBe("SPAN");
  expect(span.getAttribute("title")).toBe(`Fetched a page on ${hostile}`);
});

test("web: a subagent's read shows the same dim agent suffix and accessible-name treatment as a note", async () => {
  const { resolve, pending } = resolveSpy();
  const { env } = fakeEnv({ resolve });
  publishSourcesEnv(env);
  render(
    <SourceChip
      source={webSource("https://example.com/path", "w1", "researcher")}
      sessionId={SID}
    />,
  );
  const button = screen.getByRole("button", {
    name: "Fetched a page on example.com, by researcher",
  });
  expect(button.textContent).toBe("https://example.com/path · researcher");
  pending[0]?.resolve(new Map());
  await settle();
});

test("web: click resolves and opens the exact resolved URL, never the raw label", async () => {
  const { resolve, calls: resolveCalls, pending } = resolveSpy();
  const { env, calls } = fakeEnv({ resolve });
  publishSourcesEnv(env);
  render(<SourceChip source={webSource("https://example.com/path", "w1")} sessionId={SID} />);
  const button = screen.getByRole("button", { name: "Fetched a page on example.com" });
  fireEvent.click(button);
  expect(resolveCalls[0]).toMatchObject({ sid: SID, ids: ["w1"] });
  pending[0]?.resolve(
    new Map<string, ToolSource>([
      ["w1", { tool_use_id: "w1", tool: "WebFetch", url: "https://example.com/path?resolved=1" }],
    ]),
  );
  await settle();
  expect(calls.openUrl).toEqual(["https://example.com/path?resolved=1"]);
});

test("web: an unresolved id shows the unavailable status and opens no URL", async () => {
  const { resolve, pending } = resolveSpy();
  const { env, calls } = fakeEnv({ resolve });
  publishSourcesEnv(env);
  render(<SourceChip source={webSource("https://example.com", "w2")} sessionId={SID} />);
  fireEvent.click(screen.getByRole("button"));
  pending[0]?.resolve(new Map<string, ToolSource>());
  await settle();
  expect(screen.getByText("This page's address isn't available.")).toBeTruthy();
  expect(calls.openUrl).toHaveLength(0);
});

test("web: a second click while the first resolve is in flight is ignored; aria-busy reflects it", async () => {
  const { resolve, calls: resolveCalls, pending } = resolveSpy();
  const { env } = fakeEnv({ resolve });
  publishSourcesEnv(env);
  render(<SourceChip source={webSource("https://example.com", "w3")} sessionId={SID} />);
  const button = screen.getByRole("button");

  fireEvent.click(button);
  expect(button.getAttribute("aria-busy")).toBe("true");
  fireEvent.click(button);
  expect(resolveCalls).toHaveLength(1);

  pending[0]?.resolve(
    new Map<string, ToolSource>([
      ["w3", { tool_use_id: "w3", tool: "WebFetch", url: "https://example.com/" }],
    ]),
  );
  await settle();
  expect(button.getAttribute("aria-busy")).toBe("false");
});

test("search: a workspace_relative .md match opens the note", async () => {
  const { resolve, pending } = resolveSpy();
  const { env, calls } = fakeEnv({ resolve });
  publishSourcesEnv(env);
  render(<SourceChip source={searchSource("confidence in 30-knowledge", "s1")} sessionId={SID} />);
  fireEvent.click(screen.getByRole("button", { name: "Searched confidence in 30-knowledge" }));
  pending[0]?.resolve(
    new Map<string, ToolSource>([
      [
        "s1",
        {
          tool_use_id: "s1",
          tool: "Grep",
          pattern: "confidence",
          path: "30-knowledge/foo.md",
          path_state: "workspace_relative",
        },
      ],
    ]),
  );
  await settle();
  expect(calls.openNote).toEqual(["30-knowledge/foo.md"]);
  expect(calls.openFolder).toHaveLength(0);
});

test("search: a workspace_relative non-note match opens the folder", async () => {
  const { resolve, pending } = resolveSpy();
  const { env, calls } = fakeEnv({ resolve });
  publishSourcesEnv(env);
  render(<SourceChip source={searchSource("*.md in 30-knowledge", "s2")} sessionId={SID} />);
  fireEvent.click(screen.getByRole("button"));
  pending[0]?.resolve(
    new Map<string, ToolSource>([
      [
        "s2",
        {
          tool_use_id: "s2",
          tool: "Glob",
          pattern: "*.md",
          path: "30-knowledge",
          path_state: "workspace_relative",
        },
      ],
    ]),
  );
  await settle();
  expect(calls.openFolder).toEqual(["30-knowledge"]);
  expect(calls.openNote).toHaveLength(0);
});

test("search: a root match opens the vault root", async () => {
  const { resolve, pending } = resolveSpy();
  const { env, calls } = fakeEnv({ resolve });
  publishSourcesEnv(env);
  render(<SourceChip source={searchSource("foo everywhere", "s3")} sessionId={SID} />);
  fireEvent.click(screen.getByRole("button"));
  pending[0]?.resolve(
    new Map<string, ToolSource>([
      ["s3", { tool_use_id: "s3", tool: "Grep", pattern: "foo", path: "", path_state: "root" }],
    ]),
  );
  await settle();
  expect(calls.openFolder).toEqual([""]);
});

test("search: an unknown path_state says the search ran outside the vault", async () => {
  const { resolve, pending } = resolveSpy();
  const { env, calls } = fakeEnv({ resolve });
  publishSourcesEnv(env);
  render(<SourceChip source={searchSource("foo outside", "s4")} sessionId={SID} />);
  fireEvent.click(screen.getByRole("button"));
  pending[0]?.resolve(
    new Map<string, ToolSource>([
      ["s4", { tool_use_id: "s4", tool: "Grep", pattern: "foo", path: "", path_state: "unknown" }],
    ]),
  );
  await settle();
  expect(screen.getByText("This search ran outside the vault.")).toBeTruthy();
  expect(calls.openFolder).toHaveLength(0);
  expect(calls.openNote).toHaveLength(0);
});

test("search: a missing id says the folder isn't available", async () => {
  const { resolve, pending } = resolveSpy();
  const { env, calls } = fakeEnv({ resolve });
  publishSourcesEnv(env);
  render(<SourceChip source={searchSource("foo missing", "s5")} sessionId={SID} />);
  fireEvent.click(screen.getByRole("button"));
  pending[0]?.resolve(new Map<string, ToolSource>());
  await settle();
  expect(screen.getByText("The searched folder isn't available.")).toBeTruthy();
  expect(calls.openFolder).toHaveLength(0);
});

test("search: without resolve renders a plain span with a title", () => {
  const { env } = fakeEnv();
  publishSourcesEnv(env);
  render(<SourceChip source={searchSource("foo in bar")} sessionId={SID} />);
  expect(screen.queryByRole("button")).toBeNull();
  const span = screen.getByText("foo in bar");
  expect(span.tagName).toBe("SPAN");
  expect(span.getAttribute("title")).toBe("Searched foo in bar");
});
