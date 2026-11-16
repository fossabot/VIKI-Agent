/**
 * Acceptance (frozen): what a session row says about itself.
 * The row is where the user decides which conversation to walk into, so a row that says the
 * wrong thing costs them a wrong turn — and "uncommitted" and "live elsewhere" are the two that
 * stop them from losing work.
 */
import { expect, test } from "vitest";
import type { RowContext } from "../../components/LeftRail/rowState";
import { elsewhereMap, rowMeta, rowState } from "../../components/LeftRail/rowState";
import type { OtherSession, SessionRow } from "../../protocol";
import type { AppSnapshot } from "../../state/store";
import type { Block, SessionView, Turn } from "../../state/view";

/* timeLabel reads local hours and shows HH:MM only on the local day, so both constants are
   built as local times and then serialised, the way the m3b fakes do it. A UTC literal would
   pass in one timezone and fail in the user's. */
const NOW = new Date(2026, 8, 12, 19, 30).getTime();
const AT = new Date(2026, 8, 12, 17, 20).toISOString();

const row = (over: Partial<SessionRow> = {}): SessionRow => ({
  session_id: "s1",
  title: "Dashboard v1",
  first_prompt: "",
  entrypoint: "viki-agent",
  modified: AT,
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

test("a streaming session says it is writing, and that outranks everything else", () => {
  const c = held(view({ turns: [turn("running", [writeTool])] }));
  expect(rowState(row(), c)).toBe("writing");
  expect(rowMeta(row(), "writing", c, NOW)).toBe("writing…");
});

test("an open card outranks a dirty vault: the owner is being asked something now", () => {
  const c = held(
    view({
      turns: [turn("running")],
      cards: [
        {
          // `question` (added to permission.requested on 2026-09-13)
          // is required by the type, so this fixture has to carry it. Nothing here reads it —
          // rowState only counts open cards — so "" keeps the fixture honest about that.
          kind: "permission",
          nativeRequest: null,
          data: {
            tool_use_id: "tu1",
            kind: "tool",
            tool: "Write",
            summary: "",
            question: "",
            reason: "",
            agent: "",
            plan: "",
          },
        },
      ],
    }),
  );
  expect(rowState(row(), c)).toBe("waiting");
  expect(rowMeta(row(), "waiting", c, NOW)).toBe("Needs review · 17:20");
});

test("a session another process holds says whose it is, even while we show it", () => {
  const c = held(view(), { elsewhere: new Map([["s1", "claude-vscode"]]) });
  expect(rowState(row(), c)).toBe("elsewhere");
  expect(rowMeta(row(), "elsewhere", c, NOW)).toBe("VS Code · live elsewhere");
});

/* AMENDMENT (2026-09-13). The fixtures below
   said a terminal session's entrypoint is "claude-cli". No such value exists: catalog.py's
   MAIN_ENTRYPOINTS is {"claude-vscode", "cli", "viki-agent"} and the terminal writes `cli`, so
   every one of these tests was proving the rule against a string the server never sends — and a
   real terminal session took the else-branch. What each test asserts is unchanged, and the intent
   was right all along ("a terminal session is not called VS Code"); only the value it was tested
   with moved to the one the server actually writes. An unfamiliar entrypoint is now also checked
   here, because that is the branch the wrong value had been silently exercising. */
test("the CLI and an unfamiliar entrypoint are worded from the same rule", () => {
  const cli = held(view(), { elsewhere: new Map([["s1", "cli"]]) });
  expect(rowMeta(row(), "elsewhere", cli, NOW)).toBe("CLI · live elsewhere");
  const odd = held(view(), { elsewhere: new Map([["s1", "sdk-py"]]) });
  expect(rowMeta(row(), "elsewhere", odd, NOW)).toBe("sdk-py · live elsewhere");
});

test("a write in this window no longer gates on the vault's git state: the row reads live once idle", () => {
  const wrote = held(view({ turns: [turn("success", [writeTool])] }));
  expect(rowState(row(), wrote)).toBe("live");
  expect(rowMeta(row(), "live", wrote, NOW)).toBe("live · idle");
});

test("an idle session we hold reads live, and one we do not hold says nothing extra", () => {
  const live = held(view());
  expect(rowState(row(), live)).toBe("live");
  expect(rowMeta(row(), "live", live, NOW)).toBe("live · idle");

  const cold = ctx();
  expect(rowState(row(), cold)).toBe("plain");
  expect(rowMeta(row(), "plain", cold, NOW)).toBe("");
});

test("a closed view is not held: the row falls back to plain", () => {
  const c = held(view({ closed: true, turns: [turn("running")] }));
  expect(rowState(row(), c)).toBe("plain");
});

test("elsewhereMap keys the vault's other sessions by id", () => {
  const others: OtherSession[] = [
    { session_id: "s9", entrypoint: "claude-vscode", pid: 20412, title: "retro" },
  ];
  const map = elsewhereMap({ sessions: {}, others, status: "open" });
  expect(map.get("s9")).toBe("claude-vscode");
  expect(map.has("s1")).toBe(false);
});
