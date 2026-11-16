/**
 * Unit tests pinning sessionReducer.ts rules that sessionReducer.accept.test.ts leaves implicit:
 * done-without-delta, deltas after done, cross-turn lookups, unknown ids, empty-turn drops,
 * empty-string fields that must not overwrite, history-page turn boundaries, when a rate limit
 * stops being shown, and the routing status a history page ends on.
 */
import { expect, test } from "vitest";
import type { Envelope, HistoryPage, SessionSnapshot, UserMessageData } from "../protocol";
import { readCss } from "../testing/css";
import { groupRuns } from "./runGroups";
import { applyEnvelope, applyHistoryPage, applySnapshot } from "./sessionReducer";
import { emptyView, presence, type RoutingBlock, type SessionView, type ToolBlock } from "./view";

const STREAM_STATE_PY = "../../../src/viki_agent/conversations/stream_state.py";

const SID = "s1";
const AT = "2026-09-11T08:00:00.000Z";

interface Item {
  type: string;
  data: Record<string, unknown>;
}

const usage = { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_creation_tokens: 0 };

function envelopes(items: Item[], first = 1): Envelope[] {
  return items.map(
    (item, i) =>
      ({
        v: 2,
        generation: "11111111-1111-4111-8111-111111111111",
        request_id: "",
        session_id: SID,
        seq: first + i,
        ...item,
      }) as unknown as Envelope,
  );
}

function run(items: Item[], start: SessionView = emptyView(SID)): SessionView {
  return envelopes(items, start.seq + 1).reduce(applyEnvelope, start);
}

const userTurn: Item[] = [
  { type: "user.message", data: { text: "hi", command: "", at: AT } },
  { type: "turn.started", data: { turn_id: "t1" } },
];

test("history media survives reduction without sharing the incoming attachment objects", () => {
  const message: UserMessageData = {
    text: "history image",
    command: "",
    at: AT,
    attachments: [
      {
        asset_id: "123e4567-e89b-12d3-a456-426614174000",
        media_type: "image/png",
        byte_size: 20,
        width: 2,
        height: 2,
        name: "history-image.png",
        source: "history",
        modified_at: null,
      },
    ],
    unavailable_attachments: [
      {
        name: "history-image.webp",
        media_type: "image/webp",
        byte_size: 0,
        reason: "invalid_native_media",
      },
    ],
  };
  const view = applyHistoryPage(emptyView(SID), {
    events: [{ type: "user.message", data: message }],
    next_before_uuid: null,
  });
  const reduced = view.turns[0]?.user;
  expect(reduced).toEqual(message);
  expect(reduced?.attachments?.[0]).not.toBe(message.attachments?.[0]);
  expect(reduced?.unavailable_attachments?.[0]).not.toBe(message.unavailable_attachments?.[0]);
});

function toolStarted(id: string, parent = "", tool = "Read"): Item {
  return {
    type: "tool.started",
    data: { block_id: id, tool, row: { head: tool, rest: id, tail: "" }, parent_id: parent },
  };
}

function toolDone(id: string, tail = ""): Item {
  return {
    type: "tool.done",
    data: { block_id: id, is_error: false, summary: "ok", preview: "ok", tail },
  };
}

function turnDone(status: "success" | "cancelled" | "error", contextPercent: number | null): Item {
  return {
    type: "turn.done",
    data: { status, usage, context_percent: contextPercent, duration_ms: 1 },
  };
}

test("text.done with no prior delta creates the block already finished", () => {
  const view = run([
    ...userTurn,
    { type: "text.done", data: { block_id: "m:0", text: "whole thing" } },
  ]);
  expect(view.turns[0]?.blocks).toEqual([
    { kind: "text", id: "m:0", text: "whole thing", done: true },
  ]);
});

test("a delta after its block is done is ignored", () => {
  const view = run([
    ...userTurn,
    { type: "text.delta", data: { block_id: "m:0", text: "first" } },
    { type: "text.done", data: { block_id: "m:0", text: "first" } },
    { type: "text.delta", data: { block_id: "m:0", text: "more" } },
  ]);
  expect(view.turns[0]?.blocks).toEqual([{ kind: "text", id: "m:0", text: "first", done: true }]);
});

test("a second turn.started for an already-renamed running turn starts a new turn instead of renaming again", () => {
  const view = run([...userTurn, { type: "turn.started", data: { turn_id: "t1-again" } }]);
  expect(view.turns.map((t) => [t.id, t.user])).toEqual([
    ["t1", { text: "hi", command: "", at: AT }],
    ["t1-again", null],
  ]);
});

test("tool.started naming a routing block from an earlier turn nests there, never in the current turn", () => {
  const afterFirstTurn = run([
    ...userTurn,
    {
      type: "subagent.started",
      data: { block_id: "ag", agent: "researcher", model: "opus", description: "d", reason: "r" },
    },
    turnDone("success", null),
  ]);
  const view = run(
    [{ type: "turn.started", data: { turn_id: "t2" } }, toolStarted("x", "ag")],
    afterFirstTurn,
  );
  expect(view.turns).toHaveLength(2);
  expect(view.turns[1]?.blocks).toEqual([]); // the new turn stays empty
  const routing = view.turns[0]?.blocks[0] as RoutingBlock;
  expect(routing.tools.map((r) => r.id)).toEqual(["x"]);
});

test("only Skill rows record envelope order across nested and later turns", () => {
  const first = run([
    ...userTurn,
    {
      type: "subagent.started",
      data: { block_id: "ag", agent: "researcher", model: "opus", description: "d", reason: "r" },
    },
    toolStarted("nested-first", "ag", "Skill"),
    toolStarted("read", "", "Read"),
    turnDone("success", null),
    { type: "turn.started", data: { turn_id: "t2" } },
    toolStarted("top", "", "Skill"),
    toolStarted("nested-last", "ag", "Skill"),
  ]);
  const route = first.turns[0]?.blocks[0] as RoutingBlock;
  const top = first.turns[1]?.blocks[0] as ToolBlock;
  expect(route.tools.map((row) => [row.id, row.skillCallOrder])).toEqual([
    ["nested-first", 4],
    ["nested-last", 9],
  ]);
  expect(top.rows[0]?.skillCallOrder).toBe(8);
  expect(first.turns[0]?.blocks[1]).toEqual({
    kind: "tools",
    id: "read",
    rows: [
      {
        id: "read",
        tool: "Read",
        row: { head: "Read", rest: "read", tail: "" },
        done: false,
        isError: false,
        summary: "",
        preview: "",
        artifactStart: undefined,
      },
    ],
  });
  const done = run([toolDone("nested-last")], first);
  const updated = done.turns[0]?.blocks[0] as RoutingBlock;
  expect(updated.tools[1]?.skillCallOrder).toBe(9);
});

test("older history pages get lower Skill orders than every loaded page and live call", () => {
  const live = run([...userTurn, toolStarted("live", "", "Skill")]);
  const page = (id: string, cursor: string | null): HistoryPage["data"] =>
    ({
      events: [
        { type: "user.message", data: { text: id, command: "", at: AT } },
        toolStarted(`${id}-first`, "", "Skill"),
        toolStarted(`${id}-last`, "", "Skill"),
      ],
      next_before_uuid: cursor,
    }) as unknown as HistoryPage["data"];
  const newer = applyHistoryPage(live, page("newer", "older-cursor"));
  const older = applyHistoryPage(newer, page("older", null));
  const orders = older.turns.map((turn) => {
    const block = turn.blocks[0] as ToolBlock;
    return block.rows.map((row) => row.skillCallOrder);
  });
  expect(orders).toEqual([[-4, -3], [-2, -1], [3]]);
});

test("tool.started whose parent_id names an existing block that is not a routing block falls back to the ToolBlock rule", () => {
  const view = run([
    ...userTurn,
    toolStarted("toolu_AGENT", ""),
    toolStarted("toolu_CHILD", "toolu_AGENT"),
  ]);
  expect(view.turns[0]?.blocks).toHaveLength(1);
  const toolBlock = view.turns[0]?.blocks[0] as ToolBlock;
  expect(toolBlock.rows.map((r) => r.id)).toEqual(["toolu_AGENT", "toolu_CHILD"]);
});

test("a thinking.done for an id already held by a text block starts a separate thinking block instead of vanishing", () => {
  const view = run([
    ...userTurn,
    { type: "text.done", data: { block_id: "m:0", text: "hello" } },
    { type: "thinking.done", data: { block_id: "m:0", text: "reasoning", duration_ms: 5 } },
  ]);
  expect(view.turns[0]?.blocks).toEqual([
    { kind: "text", id: "m:0", text: "hello", done: true },
    { kind: "thinking", id: "m:0", text: "reasoning", done: true, durationMs: 5 },
  ]);
});

test("tool.done for a row nobody started leaves the turns untouched", () => {
  const view = run(userTurn);
  const after = run([toolDone("ghost")], view);
  expect(after.turns).toBe(view.turns);
});

test("a notice with no turn open is dropped rather than crashing", () => {
  const view = applyEnvelope(
    emptyView(SID),
    envelopes([
      { type: "hook", data: { name: "PreToolUse:Bash", outcome: "blocked", message: "guard" } },
    ])[0] as Envelope,
  );
  expect(view.turns).toEqual([]);
});

test("status with an empty mode keeps the current mode", () => {
  const withMode = run([{ type: "status", data: { state: "", mode: "plan" } }]);
  expect(withMode.mode).toBe("plan");
  const unchanged = run([{ type: "status", data: { state: "requesting", mode: "" } }], withMode);
  expect(unchanged.mode).toBe("plan");
});

test("turn.done with a null context_percent keeps the last known value", () => {
  const withPercent = run([...userTurn, turnDone("success", 40)]);
  expect(withPercent.contextPercent).toBe(40);
  const view = run([...userTurn, turnDone("success", null)], withPercent);
  expect(view.contextPercent).toBe(40);
});

test("an error becomes a notice while a turn is running, and view.error when the session is idle", () => {
  const crash = { type: "error", data: { code: "crashed", message: "oops", recoverable: true } };
  const runningView = run([...userTurn, crash]);
  expect(runningView.turns[0]?.blocks).toEqual([
    { kind: "notice", id: "error-3", tone: "error", subject: "crashed", detail: "oops" },
  ]);
  expect(runningView.error).toBeNull();

  const idleView = applyEnvelope(emptyView(SID), envelopes([crash])[0] as Envelope);
  expect(idleView.turns).toEqual([]);
  expect(idleView.error).toEqual(crash.data);
});

test("turn.done keeps a subagent's open card and drops the chat's own", () => {
  const permission = (id: string, agent: string): Item => ({
    type: "permission.requested",
    data: {
      tool_use_id: id,
      kind: "tool",
      tool: "Bash",
      summary: "ls",
      question: "",
      reason: "",
      agent,
      plan: "",
    },
  });
  const ended = run([
    ...userTurn,
    permission("from-run", "researcher"),
    permission("from-chat", ""),
    { type: "question.asked", data: { tool_use_id: "q1", questions: [] } },
    turnDone("success", null),
  ]);
  // A background run can outlive the turn that ends here, and the CLI still waits for its answer:
  // scenario 28 shows the CLI start and end a turn of its own while a run is still going.
  expect(ended.cards.map((card) => card.data.tool_use_id)).toEqual(["from-run"]);
});

test("session.closed clears open cards along with closing the session", () => {
  const withCard = run([
    ...userTurn,
    { type: "question.asked", data: { tool_use_id: "q1", questions: [] } },
  ]);
  expect(withCard.cards).toHaveLength(1);
  const closed = run([{ type: "session.closed", data: { reason: "requested" } }], withCard);
  expect(closed.cards).toEqual([]);
  expect(closed.closed).toBe(true);
});

test("history items before the first user.message form a turn with no user message", () => {
  const page = {
    events: [
      toolStarted("x"),
      toolDone("x"),
      { type: "user.message", data: { text: "hi", command: "", at: AT } },
      { type: "text.done", data: { block_id: "m:0", text: "hello" } },
    ],
    next_before_uuid: null,
  } as unknown as HistoryPage["data"];
  const view = applyHistoryPage(emptyView(SID), page);
  expect(view.turns).toHaveLength(2);
  expect(view.turns[0]).toMatchObject({
    id: "h-start-0",
    user: null,
    history: true,
    status: "success",
  });
  const toolBlock = view.turns[0]?.blocks[0] as ToolBlock;
  expect(toolBlock.rows[0]).toMatchObject({ id: "x", done: true });
  expect(view.turns[1]).toMatchObject({ id: "h-start-1", history: true });
  expect(view.turns[1]?.user?.text).toBe("hi");
});

function historyPrompt(text: string): HistoryPage["data"] {
  return {
    events: [{ type: "user.message", data: { text, command: "", at: AT } }],
    next_before_uuid: null,
  } as unknown as HistoryPage["data"];
}

test("a history prompt of exactly the reader's cut length, counted in code points, is marked cut", () => {
  // One astral character: 16,384 code points but 16,385 UTF-16 units.
  const view = applyHistoryPage(emptyView(SID), historyPrompt(`😀${"a".repeat(16_383)}`));
  expect(view.turns[0]?.userCut).toBe(true);
});

test("a history prompt one code point short of the cut length is not marked cut", () => {
  // One astral character: 16,383 code points but 16,384 UTF-16 units.
  const view = applyHistoryPage(emptyView(SID), historyPrompt(`😀${"a".repeat(16_382)}`));
  expect(view.turns[0]?.userCut).toBeUndefined();
});

test("the reader's cut length still matches stream_state.py's MAX_TEXT_CHARS", () => {
  // Python and TS share no module, so this read is the only thing keeping HISTORY_TEXT_LIMIT
  // in step with the server's own cut length: raise MAX_TEXT_CHARS alone and a cut history
  // prompt would keep a different length here, go unmarked, and pass as whole out of Edit and
  // both exports.
  const source = readCss(import.meta.url, STREAM_STATE_PY);
  const match = /^MAX_TEXT_CHARS = (.+)$/m.exec(source);
  expect(match, "MAX_TEXT_CHARS not found in stream_state.py").toBeTruthy();
  const literal = (match?.[1] ?? "").trim();
  expect(literal, `MAX_TEXT_CHARS is not a plain int literal: ${literal}`).toMatch(/^\d[\d_]*$/);
  const limit = Number(literal.replace(/_/g, ""));

  const cut = applyHistoryPage(emptyView(SID), historyPrompt("a".repeat(limit)));
  expect(cut.turns[0]?.userCut).toBe(true);
  const notCut = applyHistoryPage(emptyView(SID), historyPrompt("a".repeat(limit - 1)));
  expect(notCut.turns[0]?.userCut).toBeUndefined();
});

test("a thinking row from history keeps durationMs null even though duration_ms is given", () => {
  const page = {
    events: [
      { type: "user.message", data: { text: "hi", command: "", at: AT } },
      { type: "thinking.done", data: { block_id: "m:0", text: "reasoning", duration_ms: 999 } },
    ],
    next_before_uuid: null,
  } as unknown as HistoryPage["data"];
  const view = applyHistoryPage(emptyView(SID), page);
  expect(view.turns[0]?.blocks[0]).toMatchObject({
    kind: "thinking",
    done: true,
    durationMs: null,
  });
});

test("a rate limit belongs to its turn and is dropped when the next one begins", () => {
  const rateLimit = (status: string): Item => ({
    type: "rate_limit",
    data: { status, kind: "five_hour", resets_at: 1_757_000_000 },
  });
  const warned = run([...userTurn, rateLimit("allowed_warning"), turnDone("success", null)]);
  expect(warned.rateLimit).toMatchObject({ status: "allowed_warning" });

  // Nothing on the wire ever says the window reopened (mapper drops `allowed`), so the next turn
  // has to drop it or the card outlives its own reset time under replies that arrived fine.
  const sent = run(
    [{ type: "user.message", data: { text: "again", command: "", at: AT } }],
    warned,
  );
  expect(sent.rateLimit).toBeNull();

  // A limit that still applies comes back with this turn's own frame: it arrives after system/init,
  // so renaming the turn at turn.started must not clear what the turn just announced.
  const refused = run(
    [{ type: "turn.started", data: { turn_id: "t2" } }, rateLimit("rejected")],
    sent,
  );
  expect(refused.rateLimit).toMatchObject({ status: "rejected" });

  // The CLI opens turns by itself too (a background agent reporting), and the user may never
  // send again after being warned — that turn has to clear it as well.
  const background = run([{ type: "turn.started", data: { turn_id: "t3" } }], refused);
  expect(background.rateLimit).toBeNull();
});

test("a history page's routing block that nothing closed reads as stopped, never as running", () => {
  const subagent = (id: string, agent: string): Item => ({
    type: "subagent.started",
    data: { block_id: id, agent, model: "opus", description: "d", reason: "r" },
  });
  const page = {
    events: [
      { type: "user.message", data: { text: "Look it up", command: "", at: AT } },
      subagent("ag", "researcher"),
      toolStarted("x", "ag"),
      toolDone("x"),
      subagent("bg", "Explore"),
      { type: "subagent.done", data: { block_id: "bg", status: "completed" } },
    ],
    next_before_uuid: null,
  } as unknown as HistoryPage["data"];
  const view = applyHistoryPage(emptyView(SID), page);
  const blocks = view.turns[0]?.blocks ?? [];
  // "ag" is the ordinary background-agent shape: a start whose task_notification never reached
  // this page. Left "running", runningAgents() reports the researcher as here now for a run that
  // ended days ago, and the replayed row keeps its live presence dot.
  expect(blocks.map((b) => [b.id, (b as RoutingBlock).status])).toEqual([
    ["ag", "stopped"],
    ["bg", "completed"],
  ]);
  expect((blocks[0] as RoutingBlock).tools.map((r) => r.id)).toEqual(["x"]);
});

test("subagent.done copies tool_uses, total_tokens, duration_ms and summary onto the routing block", () => {
  const view = run([
    ...userTurn,
    {
      type: "subagent.started",
      data: { block_id: "ag", agent: "researcher", model: "opus", description: "d", reason: "r" },
    },
    {
      type: "subagent.done",
      data: {
        block_id: "ag",
        status: "completed",
        tool_uses: 2,
        total_tokens: 19744,
        duration_ms: 9380,
        summary: "daily.md",
      },
    },
  ]);
  const routing = view.turns[0]?.blocks[0] as RoutingBlock;
  expect(routing).toMatchObject({
    status: "completed",
    toolUses: 2,
    totalTokens: 19744,
    durationMs: 9380,
    summary: "daily.md",
  });
  expect(routing.completionSeq).toBe(view.seq);
});

test("subagent.done with no stats leaves toolUses, totalTokens, durationMs and summary absent", () => {
  const view = run([
    ...userTurn,
    {
      type: "subagent.started",
      data: { block_id: "ag", agent: "researcher", model: "opus", description: "d", reason: "r" },
    },
    { type: "subagent.done", data: { block_id: "ag", status: "completed" } },
  ]);
  const routing = view.turns[0]?.blocks[0] as RoutingBlock;
  expect(routing.toolUses).toBeUndefined();
  expect(routing.totalTokens).toBeUndefined();
  expect(routing.durationMs).toBeUndefined();
  expect(routing.summary).toBeUndefined();
  expect(routing).toEqual({
    kind: "routing",
    id: "ag",
    agent: "researcher",
    model: "opus",
    description: "d",
    reason: "r",
    status: "completed",
    tools: [],
    completionSeq: view.seq,
  });
});

test("a completion uses its live envelope sequence and history has no terminal source identity", () => {
  const events: Item[] = [
    ...userTurn,
    {
      type: "subagent.started",
      data: { block_id: "ag", agent: "researcher", model: "sonnet", description: "d", reason: "" },
    },
    { type: "subagent.done", data: { block_id: "ag", status: "completed" } },
  ];
  const live = envelopes(events, 88).reduce(applyEnvelope, emptyView(SID));
  const liveRun = live.turns[0]?.blocks[0] as RoutingBlock;
  expect(liveRun.completionSeq).toBe(91);
  const history = applyHistoryPage(emptyView(SID), {
    events: envelopes(events, 88),
    next_before_uuid: null,
  });
  const oldRun = history.turns[0]?.blocks[0] as RoutingBlock;
  expect(oldRun.completionSeq).toBeUndefined();
});

test.each([
  [91, 91],
  [undefined, undefined],
  [null, undefined],
  ["91", undefined],
  [true, undefined],
  [0, undefined],
  [-1, undefined],
  [1.5, undefined],
  [101, undefined],
  [Number.MAX_SAFE_INTEGER + 1, undefined],
])(
  "snapshot terminal source metadata %s preserves only a valid original sequence",
  (source, expected) => {
    const events = envelopes([
      ...userTurn,
      {
        type: "subagent.started",
        data: {
          block_id: "ag",
          agent: "researcher",
          model: "sonnet",
          description: "d",
          reason: "",
        },
      },
      { type: "subagent.done", data: { block_id: "ag", status: "completed" } },
    ]);
    const snapshot: SessionSnapshot = {
      v: 2,
      type: "session.snapshot",
      session_id: SID,
      generation: "11111111-1111-4111-8111-111111111111",
      request_id: "",
      seq: 100,
      data: {
        events: events.map(({ type, data }) => ({
          type,
          data,
          ...(type === "subagent.done" ? { source_seq: source } : {}),
        })) as SessionSnapshot["data"]["events"],
        state: {
          live: true,
          info: null,
          mode: "default",
          blocked: null,
          closed: false,
          error: null,
          compacting: false,
          context_percent: null,
          rate_limit: null,
        },
        truncated: false,
      },
    };
    const view = applySnapshot(emptyView(SID), snapshot);
    const routing = view.turns[0]?.blocks[0] as RoutingBlock;
    expect(routing.completionSeq).toBe(expected);
  },
);

test.each(["completed", "failed", "stopped"] as const)(
  "subagent.started resumes a %s routing block across turns without duplicating it",
  (status) => {
    const started: Item = {
      type: "subagent.started",
      data: {
        block_id: "ag",
        agent: "general-purpose",
        model: "inherit",
        description: "original task",
        reason: "",
      },
    };
    const before = run([
      ...userTurn,
      started,
      toolStarted("x", "ag"),
      toolDone("x"),
      { ...started, data: { ...started.data, block_id: "sibling", agent: "curator" } },
      {
        type: "subagent.done",
        data: {
          block_id: "ag",
          status,
          tool_uses: 2,
          total_tokens: 500,
          duration_ms: 1000,
          summary: "old result",
        },
      },
      turnDone("success", null),
    ]);
    const original = before.turns[0]?.blocks[0] as RoutingBlock;
    const sibling = before.turns[0]?.blocks[1];
    const resumed: Item = {
      ...started,
      data: {
        ...started.data,
        agent: "researcher",
        model: "sonnet",
        description: "continued task",
        reason: "continue research",
      },
    };
    const after = run(
      [{ type: "turn.started", data: { turn_id: "t2" } }, resumed, resumed],
      before,
    );
    expect(after.turns).toHaveLength(2);
    expect(after.turns[0]?.blocks.map((block) => block.id)).toEqual(["ag", "sibling"]);
    expect(after.turns[0]?.status).toBe("success");
    expect(after.turns[1]?.blocks).toEqual([]);
    expect(after.turns[0]?.blocks[1]).toBe(sibling);
    const routing = after.turns[0]?.blocks[0] as RoutingBlock;
    expect(routing).toMatchObject({
      agent: "researcher",
      model: "sonnet",
      description: "continued task",
      reason: "continue research",
      status: "running",
    });
    expect(routing.tools).toBe(original.tools);
    expect(routing.completionSeq).toBeUndefined();
    expect(routing.tools.map((row) => [row.id, row.done])).toEqual([["x", true]]);
    expect([routing.toolUses, routing.totalTokens, routing.durationMs, routing.summary]).toEqual([
      undefined,
      undefined,
      undefined,
      undefined,
    ]);
    expect(original).toMatchObject({ status, summary: "old result", toolUses: 2 });

    const completed = run(
      [{ type: "subagent.done", data: { block_id: "ag", status: "completed" } }],
      after,
    );
    expect(completed.turns[0]?.blocks).toHaveLength(2);
    expect(completed.turns[0]?.blocks[0]).toMatchObject({ status: "completed" });
    expect(completed.turns[0]?.blocks[1]).toBe(sibling);
  },
);

test.each(["before", "after", "snapshot", "background"])(
  "a resumed run stays unique and reviewable when history meets live events via %s",
  (order) => {
    const started: Item = {
      type: "subagent.started",
      data: { block_id: "ag", agent: "researcher", model: "sonnet", description: "d", reason: "" },
    };
    const page: HistoryPage["data"] = {
      events: envelopes([
        started,
        toolStarted("old-read", "ag"),
        toolDone("old-read"),
        { type: "subagent.done", data: { block_id: "ag", status: "stopped" } },
        { ...started, data: { ...started.data, block_id: "sibling", agent: "curator" } },
        { type: "subagent.done", data: { block_id: "sibling", status: "completed" } },
      ]),
      next_before_uuid: "older",
    };
    const events = [
      ...(order === "background" ? [] : userTurn),
      started,
      toolStarted("new-read", "ag"),
      toolDone("new-read"),
    ];
    let view =
      order === "snapshot"
        ? applyHistoryPage(run([{ type: "status", data: { state: "", mode: "default" } }]), page)
        : run(events, order !== "after" ? applyHistoryPage(emptyView(SID), page) : emptyView(SID));
    if (order === "after") view = applyHistoryPage(view, page);
    const parentPresence = presence(view);
    if (order === "background") expect(parentPresence).toBe("idle");
    const sibling = view.turns
      .flatMap((turn) => turn.blocks)
      .find((block) => block.id === "sibling");
    const completion: Item = {
      type: "subagent.done",
      data: { block_id: "ag", status: "completed" },
    };
    if (order !== "snapshot") {
      expect(groupRuns({ [SID]: view }, new Set()).working.map((row) => row.key)).toEqual([
        "s1:ag",
      ]);
      expect(
        groupRuns({ [SID]: run([completion], view) }, new Set()).review.map((row) => row.key),
      ).toEqual(["s1:ag"]);
    }
    const snapshot: SessionSnapshot = {
      v: 2,
      type: "session.snapshot",
      session_id: SID,
      generation: view.generation as string,
      request_id: "",
      seq: events.length,
      data: {
        events: envelopes(events),
        state: {
          live: true,
          info: null,
          mode: "default",
          blocked: null,
          closed: false,
          error: null,
          compacting: false,
          context_percent: null,
          rate_limit: null,
        },
        truncated: false,
      },
    };
    view = applySnapshot(view, snapshot);
    if (order !== "snapshot") expect(presence(view)).toBe(parentPresence);
    expect(groupRuns({ [SID]: view }, new Set()).working.map((row) => row.key)).toEqual(["s1:ag"]);
    expect(groupRuns({ [SID]: view }, new Set()).working[0]?.live).toBe(true);
    expect(groupRuns({ [SID]: view }, new Set()).done.map((row) => row.key)).toEqual([
      "s1:sibling",
    ]);
    const resumed = view.turns.flatMap((turn) => turn.blocks).find((block) => block.id === "ag") as
      | RoutingBlock
      | undefined;
    expect(resumed?.tools.map((row) => row.id)).toEqual(["old-read", "new-read"]);

    view = applyHistoryPage(view, { ...page, next_before_uuid: null });
    view = applySnapshot(view, snapshot);
    expect(groupRuns({ [SID]: view }, new Set()).working.map((row) => row.key)).toEqual(["s1:ag"]);
    expect(groupRuns({ [SID]: view }, new Set()).done.map((row) => row.key)).toEqual([
      "s1:sibling",
    ]);
    expect(view.turns.flatMap((turn) => turn.blocks).find((block) => block.id === "sibling")).toBe(
      sibling,
    );
    expect(
      view.turns.filter((turn) => turn.history).every((turn) => turn.status === "success"),
    ).toBe(true);
    view = run([completion], view);
    const completed = groupRuns({ [SID]: view }, new Set());
    expect(completed.working).toEqual([]);
    expect(completed.review.map((row) => row.key)).toEqual(["s1:ag"]);
    expect(completed.done.map((row) => row.key)).toEqual(["s1:sibling"]);
    expect(completed.review[0]?.block.tools.map((row) => row.id)).toEqual(["old-read", "new-read"]);
  },
);

test("a duplicate seq for a known session is a no-op", () => {
  const view = run(userTurn);
  const [dup] = envelopes(userTurn.slice(1), view.seq); // seq <= view.seq
  expect(applyEnvelope(view, dup as Envelope)).toBe(view);
});

// A tail-windowed transcript's pages carry partial: "tail_budget". A page with a
// cursor still has more to page through; only the one whose window bottoms out (cursor null)
// is the terminal "can't go further back" state the note has to explain.
test("a partial page with a cursor applies its events and keeps paging, no note", () => {
  const page = {
    events: [{ type: "user.message", data: { text: "hi", command: "", at: AT } }],
    next_before_uuid: "older-uuid",
    partial: "tail_budget",
  } as unknown as HistoryPage["data"];
  const view = applyHistoryPage(emptyView(SID), page);
  expect(view.turns).toHaveLength(1);
  expect(view.historyCursor).toBe("older-uuid");
  expect(view.historyError).toBeNull();
});

test("a partial page whose window bottoms out (cursor null) sets historyError tail_budget", () => {
  const page = {
    events: [{ type: "user.message", data: { text: "hi", command: "", at: AT } }],
    next_before_uuid: null,
    partial: "tail_budget",
  } as unknown as HistoryPage["data"];
  const view = applyHistoryPage(emptyView(SID), page);
  expect(view.historyCursor).toBeNull();
  expect(view.historyError).toBe("tail_budget");
});

// Busy keeps the cursor so a manual reload (viki.actions.reloadHistory) can resume
// paging; every other history.page error still nulls it (the terminal, non-recoverable states).
test("a busy page keeps the prior cursor; a non-busy error still nulls it", () => {
  const loaded = { ...emptyView(SID), historyCursor: "some-uuid" };
  const busyPage = {
    events: [],
    next_before_uuid: null,
    error: "busy",
  } as unknown as HistoryPage["data"];
  const busy = applyHistoryPage(loaded, busyPage);
  expect(busy.historyCursor).toBe("some-uuid");
  expect(busy.historyError).toBe("busy");

  const tooLargePage = {
    events: [],
    next_before_uuid: null,
    error: "too_large",
  } as unknown as HistoryPage["data"];
  const tooLarge = applyHistoryPage(loaded, tooLargePage);
  expect(tooLarge.historyCursor).toBeNull();
  expect(tooLarge.historyError).toBe("too_large");
});
