/**
 * Unit tests for components/Sources/publishSources.ts: every `inVault` rule (kind, chat-only,
 * workspace id, roots tracked/loaded, root absent or matching), both `saveAsNote` paths (editor
 * and hand-off, including "unavailable" with no words and the today fallback), `peek`'s display
 * title and its "note text missing" rejection, `resolve`'s 16-id cap and request id, and the
 * publish lifecycle (null before mount and after unmount, stable across action-only renders,
 * rebuilt when a data field changes).
 */
import { cleanup, render, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import type { SessionRootJson } from "../../clients/code/rootsApi";
import type { ConversationSearchApi } from "../../clients/conversations/conversationSearchApi";
import type { RootsSnapshot } from "../../state/sessionRoots";
import type { VaultReadApi } from "../../state/vaultApi";
import type { Block, TextBlock, ToolBlock, ToolRowView, Turn } from "../../state/view";
import { type PublishSourcesInput, usePublishSources } from "./publishSources";
import { resetSourcesEnv, type SourcesEnv, useSourcesEnv } from "./sourcesEnv";

afterEach(cleanup);
beforeEach(resetSourcesEnv);

// --- fixtures ----------------------------------------------------------------------------------

function turn(blocks: Block[], id = "t1"): Turn {
  return { id, user: null, blocks, status: "success", usage: null, durationMs: 0, history: false };
}
function textBlock(text: string, id = "b0"): TextBlock {
  return { kind: "text", id, text, done: true };
}
/** A turn whose only block is this one piece of reply text. */
function replyTurn(text: string): Turn {
  return turn([textBlock(text)]);
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

function rootsSnapshot(
  loaded: boolean,
  entries: SessionRootJson[] = [],
  rootsComplete = loaded,
): RootsSnapshot {
  return {
    roots: new Map(entries.map((root) => [root.session_id, root])),
    worktrees: [],
    loaded,
    rootsComplete,
  };
}

const notUsed = (): Promise<never> => Promise.reject(new Error("not used in this test"));

function fakeVault(note: VaultReadApi["note"]): VaultReadApi {
  return { entries: notUsed, note, backlinks: notUsed, history: notUsed };
}
function fakeSearch(toolSources?: ConversationSearchApi["toolSources"]): ConversationSearchApi {
  return { search: notUsed, anchor: notUsed, ...(toolSources ? { toolSources } : {}) };
}

const DISPLAY_METADATA = {
  state: "ready",
  frontmatter_state: "ready",
  heading_state: "absent",
  display_title: "Deny rules hold even in bypass mode",
  title_source: "title",
  tags: [],
  type: null,
  status: null,
};

function baseInput(overrides: Partial<PublishSourcesInput> = {}): PublishSourcesInput {
  return {
    workspaceId: "ws-1",
    kind: "vault",
    chatOnly: false,
    roots: rootsSnapshot(true),
    rootsTracked: true,
    save: null,
    openNote: vi.fn(),
    openFolder: vi.fn(),
    openUrl: vi.fn(),
    openDraft: vi.fn(),
    handoff: vi.fn((_text: string) => true),
    ...overrides,
  };
}

function Harness({ input }: { input: PublishSourcesInput }): null {
  usePublishSources(input);
  return null;
}

/** Mounts the hook with these overrides and returns the env it published. */
function publish(overrides: Partial<PublishSourcesInput> = {}): SourcesEnv {
  render(<Harness input={baseInput(overrides)} />);
  const { result } = renderHook(() => useSourcesEnv());
  const env = result.current;
  if (!env) throw new Error("env not published");
  return env;
}

// --- inVault -------------------------------------------------------------------------------------

describe("inVault", () => {
  test("false when the active workspace is not a vault", () => {
    expect(publish({ kind: "code" }).inVault("s1")).toBe(false);
  });

  test("false when the active workspace is chat-only", () => {
    expect(publish({ chatOnly: true }).inVault("s1")).toBe(false);
  });

  test("false when the active workspace id is unknown", () => {
    expect(publish({ workspaceId: null }).inVault("s1")).toBe(false);
  });

  test("false while roots are tracked but not yet loaded", () => {
    expect(publish({ rootsTracked: true, roots: rootsSnapshot(false) }).inVault("s1")).toBe(false);
  });

  test("true when roots are not tracked, even unloaded, with no recorded root", () => {
    expect(publish({ rootsTracked: false, roots: rootsSnapshot(false) }).inVault("s1")).toBe(true);
  });

  test("true when roots are loaded and this session has no recorded root", () => {
    expect(publish({ roots: rootsSnapshot(true) }).inVault("s1")).toBe(true);
  });

  test("false when the session is missing from incomplete root coverage", () => {
    expect(publish({ roots: rootsSnapshot(true, [], false) }).inVault("s1")).toBe(false);
  });

  test("an exact recorded workspace remains usable with incomplete root coverage", () => {
    const roots = rootsSnapshot(
      true,
      [{ session_id: "s1", workspace_id: "ws-1", worktree_id: null }],
      false,
    );
    expect(publish({ roots }).inVault("s1")).toBe(true);
  });

  test("true when the recorded root is this workspace outside any worktree", () => {
    const roots = rootsSnapshot(true, [
      { session_id: "s1", workspace_id: "ws-1", worktree_id: null },
    ]);
    expect(publish({ roots }).inVault("s1")).toBe(true);
  });

  test("false when the recorded root belongs to a different workspace", () => {
    const roots = rootsSnapshot(true, [
      { session_id: "s1", workspace_id: "ws-2", worktree_id: null },
    ]);
    expect(publish({ roots }).inVault("s1")).toBe(false);
  });

  test("false when the recorded root is a worktree", () => {
    const roots = rootsSnapshot(true, [
      { session_id: "s1", workspace_id: "ws-1", worktree_id: "wt-1" },
    ]);
    expect(publish({ roots }).inVault("s1")).toBe(false);
  });
});

// --- saveAsNote: both save paths ------------------------------------------------------------------

describe("saveAsNote", () => {
  test("null save publishes no saveAsNote", () => {
    expect(publish({ save: null }).saveAsNote).toBeUndefined();
  });

  test("editor: opens a prefilled draft and reports opened", async () => {
    const openDraft = vi.fn();
    const env = publish({ save: "editor", openDraft });
    const outcome = await env.saveAsNote?.("s1", replyTurn("Hello world"));
    expect(outcome).toBe("opened");
    expect(openDraft).toHaveBeenCalledTimes(1);
    const draft = openDraft.mock.calls[0]?.[0];
    expect(draft.ref).toBe("Hello world.md");
    expect(draft.content.startsWith("# Hello world\n")).toBe(true);
    expect(draft.content.endsWith("No vault note or web page was read for this reply.")).toBe(true);
  });

  test("editor: unavailable when the turn has no words", async () => {
    const openDraft = vi.fn();
    const env = publish({ save: "editor", openDraft });
    const outcome = await env.saveAsNote?.("s1", turn([]));
    expect(outcome).toBe("unavailable");
    expect(openDraft).not.toHaveBeenCalled();
  });

  test("editor: falls back to the user's local date, not the UTC one, when none is supplied", async () => {
    const originalTz = process.env.TZ;
    process.env.TZ = "Asia/Tokyo";
    vi.useFakeTimers();
    // 2026-10-09T15:30Z is already 2026-10-10 00:30 in Asia/Tokyo (UTC+9): the UTC date would
    // wrongly date this reply a day early.
    vi.setSystemTime(new Date("2026-10-09T15:30:00.000Z"));
    try {
      const openDraft = vi.fn();
      const env = publish({ save: "editor", openDraft });
      await env.saveAsNote?.("s1", replyTurn("????"));
      expect(openDraft.mock.calls[0]?.[0].ref).toBe("Saved reply 2026-10-10.md");
    } finally {
      vi.useRealTimers();
      process.env.TZ = originalTz;
    }
  });

  test("editor: uses the supplied today instead of the real date", async () => {
    const openDraft = vi.fn();
    const env = publish({ save: "editor", openDraft, today: "2026-01-01" });
    await env.saveAsNote?.("s1", replyTurn("????"));
    expect(openDraft.mock.calls[0]?.[0].ref).toBe("Saved reply 2026-01-01.md");
  });

  test("hand-off: opens a chat draft and reports opened", async () => {
    const handoff = vi.fn((_text: string) => true);
    const env = publish({ save: "handoff", handoff });
    const outcome = await env.saveAsNote?.("s1", replyTurn("Hello world"));
    expect(outcome).toBe("opened");
    expect(handoff).toHaveBeenCalledTimes(1);
    expect(handoff.mock.calls[0]?.[0]).toContain("Hello world");
  });

  test("hand-off: no_room when the editor is at its cap", async () => {
    const env = publish({ save: "handoff", handoff: vi.fn((_text: string) => false) });
    const outcome = await env.saveAsNote?.("s1", replyTurn("Hello world"));
    expect(outcome).toBe("no_room");
  });

  test("hand-off: unavailable when the turn has no words", async () => {
    const handoff = vi.fn((_text: string) => true);
    const env = publish({ save: "handoff", handoff });
    const outcome = await env.saveAsNote?.("s1", turn([]));
    expect(outcome).toBe("unavailable");
    expect(handoff).not.toHaveBeenCalled();
  });

  test("resolves up to 16 search ids and falls back to unresolved text on failure", async () => {
    const rows = Array.from({ length: 17 }, (_, i) => toolRow(`g${i}`, "Grep", `pattern-${i}`));
    const content = turn([textBlock("Body text"), toolsBlock(...rows)]);
    const toolSources = vi.fn().mockRejectedValue(new Error("boom"));
    const openDraft = vi.fn();
    const env = publish({ save: "editor", openDraft, search: fakeSearch(toolSources) });
    const outcome = await env.saveAsNote?.("s1", content);
    expect(outcome).toBe("opened");
    expect(toolSources).toHaveBeenCalledTimes(1);
    expect(toolSources.mock.calls[0]?.[1]).toHaveLength(16);
    const draftContent = openDraft.mock.calls[0]?.[0].content as string;
    expect(draftContent).toContain("- Searched: pattern-0");
    expect(draftContent).toContain("- Searched: pattern-16");
  });

  test("uses resolved search/web facts when the lookup succeeds", async () => {
    const rows = [toolRow("g1", "Grep", "TODO src/"), toolRow("w1", "WebFetch", "example.com")];
    const content = turn([textBlock("Body text"), toolsBlock(...rows)]);
    const toolSources = vi.fn().mockResolvedValue({
      request_id: "r1",
      state: "ready",
      reason: "",
      items: [
        {
          tool_use_id: "g1",
          tool: "Grep",
          pattern: "TODO",
          path: "src",
          path_state: "workspace_relative",
        },
        { tool_use_id: "w1", tool: "WebFetch", url: "https://example.com/page" },
      ],
    });
    const openDraft = vi.fn();
    const env = publish({ save: "editor", openDraft, search: fakeSearch(toolSources) });
    await env.saveAsNote?.("s1", content);
    const draftContent = openDraft.mock.calls[0]?.[0].content as string;
    expect(draftContent).toContain('- Searched src for "TODO"');
    expect(draftContent).toContain("- https://example.com/page");
  });
});

// --- peek ------------------------------------------------------------------------------------------

describe("peek", () => {
  test("absent without a vault", () => {
    expect(publish({}).peek).toBeUndefined();
  });

  test("reads the note and falls back to the chip's own label without display metadata", async () => {
    const note = vi.fn().mockResolvedValue({
      note: { text: "Body\n\n## Summary\nThe gist.", frontmatter: {} },
    });
    const env = publish({ vault: fakeVault(note) });
    const peek = await env.peek?.("30-knowledge/claim.md");
    expect(peek?.title).toBe("claim");
    expect(peek?.summary).toBe("The gist.");
    expect(note).toHaveBeenCalledWith("ws-1", "30-knowledge/claim.md", undefined);
  });

  test("prefers the authored display title when display_metadata parses", async () => {
    const note = vi.fn().mockResolvedValue({
      note: {
        text: "Body",
        frontmatter: { modified: "2026-10-01", confidence: "high" },
        display_metadata: DISPLAY_METADATA,
      },
    });
    const env = publish({ vault: fakeVault(note) });
    const peek = await env.peek?.("30-knowledge/claim.md");
    expect(peek?.title).toBe("Deny rules hold even in bypass mode");
    expect(peek?.modified).toBe("2026-10-01");
    expect(peek?.confidence).toBe("high");
  });

  test("rejects when the note's own text is missing", async () => {
    const note = vi.fn().mockResolvedValue({ note: { frontmatter: {} } });
    const env = publish({ vault: fakeVault(note) });
    await expect(env.peek?.("x.md")).rejects.toThrow();
  });
});

// --- resolve ---------------------------------------------------------------------------------------

describe("resolve", () => {
  test("absent without a search client", () => {
    expect(publish({}).resolve).toBeUndefined();
  });

  test("absent when the search client has no toolSources", () => {
    expect(publish({ search: fakeSearch() }).resolve).toBeUndefined();
  });

  test("maps results by tool_use_id and caps the request at 16 ids with a fresh request id", async () => {
    const toolSources = vi.fn().mockResolvedValue({
      request_id: "r1",
      state: "ready",
      reason: "",
      items: [{ tool_use_id: "b1", tool: "WebFetch", url: "https://example.com" }],
    });
    const env = publish({ search: fakeSearch(toolSources) });
    const ids = Array.from({ length: 20 }, (_, i) => `b${i}`);
    const resolved = await env.resolve?.("s1", ids);
    expect(resolved?.get("b1")).toEqual({
      tool_use_id: "b1",
      tool: "WebFetch",
      url: "https://example.com",
    });
    const call = toolSources.mock.calls[0];
    expect(call?.[0]).toBe("s1");
    expect(call?.[1]).toHaveLength(16);
    expect(call?.[2]).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
  });

  test("propagates a lookup failure rather than swallowing it", async () => {
    const toolSources = vi.fn().mockRejectedValue(new Error("boom"));
    const env = publish({ search: fakeSearch(toolSources) });
    await expect(env.resolve?.("s1", ["b1"])).rejects.toThrow("boom");
  });
});

// --- publish lifecycle -------------------------------------------------------------------------------

describe("publish lifecycle", () => {
  test("publishes null before mount and again after unmount", () => {
    const { result } = renderHook(() => useSourcesEnv());
    expect(result.current).toBeNull();
    const { unmount } = render(<Harness input={baseInput()} />);
    expect(result.current).not.toBeNull();
    unmount();
    expect(result.current).toBeNull();
  });

  test("keeps the same env across renders that only change actions", () => {
    const { result } = renderHook(() => useSourcesEnv());
    const data = baseInput();
    const { rerender } = render(<Harness input={{ ...data, openNote: vi.fn() }} />);
    const first = result.current;
    rerender(<Harness input={{ ...data, openNote: vi.fn() }} />);
    expect(result.current).toBe(first);
  });

  test("always calls the latest action even though the env is stable", () => {
    const data = baseInput();
    const { rerender } = render(<Harness input={{ ...data, openNote: vi.fn() }} />);
    const { result } = renderHook(() => useSourcesEnv());
    const second = vi.fn();
    rerender(<Harness input={{ ...data, openNote: second }} />);
    result.current?.openNote("30-knowledge/a.md");
    expect(second).toHaveBeenCalledWith("30-knowledge/a.md");
  });

  test("rebuilds the env when a data field changes", () => {
    const { result } = renderHook(() => useSourcesEnv());
    const data = baseInput({ workspaceId: "ws-1" });
    const { rerender } = render(<Harness input={data} />);
    const first = result.current;
    rerender(<Harness input={{ ...data, workspaceId: "ws-2" }} />);
    expect(result.current).not.toBe(first);
  });
});
