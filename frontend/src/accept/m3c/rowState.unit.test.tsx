/**
 * Behaviours rowState.accept.test.ts never pinned, so a regression
 * in any of them kept the frozen suite green. Two gaps:
 *
 * (a) presence() can return "thinking" as well as "writing" (state/view.ts) but no frozen
 *     fixture ever builds a `thinking` block, so `rowState`'s `p === "thinking"` branch could
 *     be deleted without failing anything — and a row would read "live · idle" for VIKI's whole
 *     thinking phase.
 * (b) rowState.ts checks "writing" (1) before "elsewhere" (3) before "live" (4), but the
 *     frozen suite only ever exercises "elsewhere" against an idle view, never against a
 *     session that is also writing in this window. Hoisting the
 *     `ctx.elsewhere.has()` check above the presence check would still pass every frozen test.
 *
 * Also pins the two rowState.css rules the module doc describes, since that file has no
 * sibling `.css.unit.test.ts` of its own.
 */
import { expect, test } from "vitest";
import type { RowContext } from "../../components/LeftRail/rowState";
import { rowMeta, rowState } from "../../components/LeftRail/rowState";
import type { SessionRow } from "../../protocol";
import type { AppSnapshot } from "../../state/store";
import type { Block, SessionView, Turn } from "../../state/view";
import { readCss, ruleIn } from "../../testing/css";

const NOW = new Date(2026, 8, 12, 19, 30).getTime();

const row = (over: Partial<SessionRow> = {}): SessionRow => ({
  session_id: "s1",
  title: "Dashboard v1",
  first_prompt: "",
  entrypoint: "viki-agent",
  modified: new Date(2026, 8, 12, 17, 20).toISOString(),
  cli_version: "2.1.259",
  other: false,
  ...over,
});

function view(over: Partial<SessionView> = {}): SessionView {
  return {
    sessionId: "s1",
    generation: null,
    requestId: null,
    synced: false,
    truncated: false,
    historyRequestId: null,
    historyError: null,
    seq: 1,
    info: null,
    mode: "auto",
    turns: [],
    cards: [],
    blocked: null,
    closed: false,
    error: null,
    compacting: false,
    contextPercent: null,
    rateLimit: null,
    historyCursor: undefined,
    historyLoading: false,
    ...over,
  };
}

function turn(status: Turn["status"], blocks: Block[] = []): Turn {
  return { id: "t1", user: null, blocks, status, usage: null, durationMs: 0, history: false };
}

function ctx(over: Partial<RowContext> = {}): RowContext {
  const app: AppSnapshot = { sessions: {}, others: [], status: "open" };
  return { app, elsewhere: new Map(), ...over };
}

function held(v: SessionView, rest: Partial<RowContext> = {}): RowContext {
  return ctx({ app: { sessions: { s1: v }, others: [], status: "open" }, ...rest });
}

const writeTool: Block = {
  kind: "tools",
  id: "b1",
  rows: [
    {
      id: "tu1",
      tool: "Write",
      row: { head: "Write", rest: "30-knowledge/x.md", tail: "+12" },
      done: true,
      isError: false,
      summary: "",
      preview: "",
    },
  ],
};

const thinkingBlock = (done: boolean): Block => ({
  kind: "thinking",
  id: "b1",
  text: "",
  done,
  durationMs: null,
});

test("(a) a session mid-thought still says it is writing, not idle", () => {
  const c = held(view({ turns: [turn("running", [thinkingBlock(false)])] }));
  expect(rowState(row(), c)).toBe("writing");
  expect(rowMeta(row(), "writing", c, NOW)).toBe("writing…");
});

test("(b) writing outranks elsewhere: a session live here AND open in VS Code still reads writing", () => {
  const c = held(view({ turns: [turn("running", [writeTool])] }), {
    elsewhere: new Map([["s1", "claude-vscode"]]),
  });
  expect(rowState(row(), c)).toBe("writing");
});

test("(b) elsewhere outranks live: whose it is matters more than whether this window wrote", () => {
  const c = held(view({ turns: [turn("success", [writeTool])] }), {
    elsewhere: new Map([["s1", "claude-vscode"]]),
  });
  expect(rowState(row(), c)).toBe("elsewhere");
});

test("rowState.css: the writing meta color and the ringed live dot exist for the wiring WP", () => {
  const css = readCss(import.meta.url, "../../components/LeftRail/rowState.css");
  expect(ruleIn(css, ".sess-meta-writing")).toMatch(/color:\s*var\(--or-hi\)/);
  const liveDot = ruleIn(css, ".sess-dot-live");
  expect(liveDot).toMatch(/border:\s*1px solid var\(--or\)/);
});
