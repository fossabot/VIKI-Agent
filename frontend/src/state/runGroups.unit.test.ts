/**
 * runGroups.ts: grouping, attribution and wording for the right rail's run groups. One test per
 * rule in the module's own header comment.
 */
import { expect, test } from "vitest";
import type { AgentInfo, Envelope, HistoryItem, PermissionRequestedData } from "../protocol";
import { rosterRunning, runningAgents } from "./rows";
import {
  groupRuns,
  type RunRequests,
  type RunRow,
  runCounts,
  runElapsed,
  runKey,
  runLine,
} from "./runGroups";
import { applyEnvelope, applyHistoryPage, applySnapshot } from "./sessionReducer";
import type { Card, RoutingBlock, SessionView, Turn } from "./view";
import { emptyView, presence } from "./view";

const RUNTIME = "33333333-3333-4333-8333-333333333333";
const TURN_ID = "55555555-5555-4555-8555-555555555555";

function routing(id: string, overrides: Partial<RoutingBlock> = {}): RoutingBlock {
  return {
    kind: "routing",
    id,
    agent: "researcher",
    model: "sonnet",
    description: "Find the daily note template",
    reason: "",
    status: "running",
    tools: [],
    ...overrides,
  };
}

function turn(id: string, blocks: RoutingBlock[], overrides: Partial<Turn> = {}): Turn {
  return {
    id,
    user: null,
    blocks,
    status: "running",
    usage: null,
    durationMs: 0,
    history: false,
    ...overrides,
  };
}

function view(sessionId: string, overrides: Partial<SessionView> = {}): SessionView {
  return { ...emptyView(sessionId), ...overrides };
}

function permissionCard(
  requestId: string,
  agentName: string,
  overrides: Partial<PermissionRequestedData> = {},
): Extract<Card, { kind: "permission" }> {
  return {
    kind: "permission",
    data: {
      tool_use_id: `tu-${requestId}`,
      kind: "tool",
      tool: "Write",
      summary: "30-knowledge/x.md",
      question: "",
      reason: "",
      agent: agentName,
      plan: "",
      ...overrides,
    },
    nativeRequest: { runtime_id: RUNTIME, request_id: requestId, turn_id: TURN_ID },
  };
}

function questionCard(requestId: string): Card {
  return {
    kind: "question",
    data: {
      tool_use_id: `tu-${requestId}`,
      questions: [
        { header: "", question: "Which one should I use?", options: [], multi_select: false },
      ],
    },
    nativeRequest: { runtime_id: RUNTIME, request_id: requestId, turn_id: TURN_ID },
  };
}

function requestsMap(
  sessionId: string,
  entries: Record<string, { run: string | null; raisedAt: number }>,
): RunRequests {
  return new Map([[sessionId, new Map(Object.entries(entries))]]);
}

const NO_REVIEWED: ReadonlySet<string> = new Set();

test("runKey joins the session and block id with a colon", () => {
  expect(runKey("s1", "r1")).toBe("s1:r1");
});

// --- attribution -------------------------------------------------------------------------------

test("a permission card naming one agent joins the one running row of that agent", () => {
  const sessions = {
    s1: view("s1", {
      turns: [turn("t0", [routing("r1", { agent: "researcher" })])],
      cards: [permissionCard("req-1", "researcher")],
    }),
  };
  const groups = groupRuns(sessions, NO_REVIEWED);
  expect(groups.needs).toHaveLength(1);
  expect(groups.needs[0]?.requests.map((r) => r.tool_use_id)).toEqual(["tu-req-1"]);
  expect(groups.unattributed).toEqual([]);
});

test("a permission card naming an agent running twice in the same chat joins the chat level instead", () => {
  const sessions = {
    s1: view("s1", {
      turns: [
        turn("t0", [
          routing("r1", { agent: "researcher" }),
          routing("r2", { agent: "researcher" }),
        ]),
      ],
      cards: [permissionCard("req-1", "researcher")],
    }),
  };
  const groups = groupRuns(sessions, NO_REVIEWED);
  // Neither run claims the card: both stay in `working`, with no requests of their own.
  expect(groups.working.map((r) => r.key)).toEqual(["s1:r1", "s1:r2"]);
  expect(groups.needs).toEqual([]);
  expect(groups.unattributed).toEqual([
    { sessionId: "s1", requests: [permissionCard("req-1", "researcher").data], waitingSince: null },
  ]);
});

test("a permission card naming an agent running twice joins the run that requests names by id", () => {
  const sessions = {
    s1: view("s1", {
      turns: [
        turn("t0", [
          routing("r1", { agent: "researcher" }),
          routing("r2", { agent: "researcher" }),
        ]),
      ],
      cards: [permissionCard("req-1", "researcher")],
    }),
  };
  const requests = requestsMap("s1", { "tu-req-1": { run: "r2", raisedAt: 1_000 } });
  const groups = groupRuns(sessions, NO_REVIEWED, requests);
  expect(groups.working.map((r) => r.key)).toEqual(["s1:r1"]);
  expect(groups.needs.map((r) => r.key)).toEqual(["s1:r2"]);
  expect(groups.needs[0]?.requests.map((r) => r.tool_use_id)).toEqual(["tu-req-1"]);
  expect(groups.unattributed).toEqual([]);
});

test("a card whose requests-named run is not a running row of the session falls back to the type rule", () => {
  const sessions = {
    s1: view("s1", {
      turns: [
        turn("t0", [
          routing("r1", { agent: "researcher" }),
          routing("r2", { agent: "researcher" }),
        ]),
      ],
      cards: [permissionCard("req-1", "researcher")],
    }),
  };
  // "r9" names no row at all; a named run that is not running (e.g. already finished) falls back
  // the same way, since `namedRow` only ever matches a running row.
  const requests = requestsMap("s1", { "tu-req-1": { run: "r9", raisedAt: 1_000 } });
  const groups = groupRuns(sessions, NO_REVIEWED, requests);
  expect(groups.working.map((r) => r.key)).toEqual(["s1:r1", "s1:r2"]);
  expect(groups.needs).toEqual([]);
  expect(groups.unattributed).toEqual([
    {
      sessionId: "s1",
      requests: [permissionCard("req-1", "researcher").data],
      waitingSince: 1_000,
    },
  ]);
});

test("the generic subagent fallback with no same-named run joins the chat level", () => {
  const sessions = {
    s1: view("s1", {
      turns: [turn("t0", [routing("r1", { agent: "researcher" })])],
      cards: [permissionCard("req-1", "subagent")],
    }),
  };
  const groups = groupRuns(sessions, NO_REVIEWED);
  expect(groups.needs).toEqual([]);
  expect(groups.working[0]?.requests).toEqual([]);
  expect(groups.unattributed).toHaveLength(1);
  expect(groups.unattributed[0]?.sessionId).toBe("s1");
});

test("a permission card whose agent matches no running row joins the chat level", () => {
  const sessions = {
    s1: view("s1", {
      turns: [turn("t0", [routing("r1", { agent: "researcher" })])],
      cards: [permissionCard("req-1", "connector")],
    }),
  };
  const groups = groupRuns(sessions, NO_REVIEWED);
  expect(groups.unattributed).toHaveLength(1);
  expect(groups.unattributed[0]?.requests.map((r) => r.tool_use_id)).toEqual(["tu-req-1"]);
});

test("a permission card with no agent is never attributed, not even at the chat level", () => {
  const sessions = {
    s1: view("s1", {
      turns: [turn("t0", [routing("r1", { agent: "researcher" })])],
      cards: [permissionCard("req-1", "")],
    }),
  };
  const groups = groupRuns(sessions, NO_REVIEWED);
  expect(groups.working[0]?.requests).toEqual([]);
  expect(groups.needs).toEqual([]);
  expect(groups.unattributed).toEqual([]);
});

test("a question card is ignored for attribution", () => {
  const sessions = {
    s1: view("s1", {
      turns: [turn("t0", [routing("r1", { agent: "researcher" })])],
      cards: [questionCard("req-1")],
    }),
  };
  const groups = groupRuns(sessions, NO_REVIEWED);
  expect(groups.working[0]?.requests).toEqual([]);
  expect(groups.unattributed).toEqual([]);
});

// --- waitingSince ------------------------------------------------------------------------------

test("a row's waitingSince is the smallest known raisedAt among its requests, else null", () => {
  const sessions = {
    s1: view("s1", {
      turns: [
        turn("t0", [routing("r1", { agent: "researcher" }), routing("r2", { agent: "connector" })]),
      ],
      cards: [
        permissionCard("req-1", "researcher", { tool_use_id: "tu-a" }),
        permissionCard("req-2", "researcher", { tool_use_id: "tu-b" }),
        permissionCard("req-3", "connector", { tool_use_id: "tu-c" }),
      ],
    }),
  };
  // r1 gets two requests (by id); the older one (1_000) wins over the newer (2_000).
  const requests = requestsMap("s1", {
    "tu-a": { run: "r1", raisedAt: 2_000 },
    "tu-b": { run: "r1", raisedAt: 1_000 },
    // r2's own request is never entered into `requests`, so it stays unknown.
  });
  const groups = groupRuns(sessions, NO_REVIEWED, requests);
  expect(groups.needs.find((r) => r.key === "s1:r1")?.waitingSince).toBe(1_000);
  expect(groups.needs.find((r) => r.key === "s1:r2")?.waitingSince).toBeNull();
});

test("a session need's waitingSince is the smallest known raisedAt among its requests, else null", () => {
  const ambiguous = {
    s1: view("s1", {
      turns: [
        turn("t0", [
          routing("r1", { agent: "researcher" }),
          routing("r2", { agent: "researcher" }),
        ]),
      ],
      cards: [permissionCard("req-1", "researcher")],
    }),
  };
  expect(groupRuns(ambiguous, NO_REVIEWED).unattributed[0]?.waitingSince).toBeNull();
  const requests = requestsMap("s1", { "tu-req-1": { run: null, raisedAt: 3_000 } });
  expect(groupRuns(ambiguous, NO_REVIEWED, requests).unattributed[0]?.waitingSince).toBe(3_000);
});

// --- grouping and order -------------------------------------------------------------------------

test("needs and working keep walk order; review and done are newest first", () => {
  const sessions = {
    s1: view("s1", {
      turns: [
        turn("t0", [
          routing("r1", { agent: "a" }), // working
          routing("r2", { agent: "b", status: "completed" }), // review
        ]),
        turn("t1", [
          routing("r3", { agent: "c" }), // working
          routing("r4", { agent: "d", status: "completed" }), // review
        ]),
      ],
      cards: [permissionCard("req-1", "a")], // turns r1 into `needs`, out of `working`
    }),
  };
  const groups = groupRuns(sessions, NO_REVIEWED);
  expect(groups.needs.map((r) => r.key)).toEqual(["s1:r1"]);
  expect(groups.working.map((r) => r.key)).toEqual(["s1:r3"]);
  expect(groups.review.map((r) => r.key)).toEqual(["s1:r4", "s1:r2"]); // newest (r4) first
});

test("a reviewed run moves from review to done", () => {
  const sessions = {
    s1: view("s1", {
      turns: [turn("t0", [routing("r1", { status: "completed" })])],
    }),
  };
  expect(groupRuns(sessions, NO_REVIEWED).review.map((r) => r.key)).toEqual(["s1:r1"]);
  expect(groupRuns(sessions, new Set(["s1:r1"])).done.map((r) => r.key)).toEqual(["s1:r1"]);
  expect(groupRuns(sessions, new Set(["s1:r1"])).review).toEqual([]);
});

test("a completed block rebuilt from history is done even though nothing has reviewed it", () => {
  const sessions = {
    s1: view("s1", {
      turns: [turn("t0", [routing("r1", { status: "completed" })], { history: true })],
    }),
  };
  const groups = groupRuns(sessions, NO_REVIEWED);
  expect(groups.done.map((r) => r.key)).toEqual(["s1:r1"]);
  expect(groups.review).toEqual([]);
});

test("a stopped run is done, not review, live or not", () => {
  const sessions = {
    s1: view("s1", {
      turns: [turn("t0", [routing("r1", { status: "stopped" })])],
    }),
  };
  const groups = groupRuns(sessions, NO_REVIEWED);
  expect(groups.done.map((r) => r.key)).toEqual(["s1:r1"]);
  expect(groups.review).toEqual([]);
  expect(groups.working).toEqual([]);
});

test("resumed reducer events restore the working run and named roster across snapshot reconstruction", () => {
  const roster: AgentInfo[] = [
    { name: "researcher", description: "", tools: [], model: "sonnet", last_run: "" },
    { name: "curator", description: "", tools: [], model: "sonnet", last_run: "" },
  ];
  const started: HistoryItem = {
    type: "subagent.started",
    data: {
      block_id: "r1",
      agent: "researcher",
      model: "sonnet",
      description: "Find the daily note template",
      reason: "",
    },
  };
  let current = emptyView("s1");
  const events: HistoryItem[] = [];
  const receive = (item: HistoryItem) => {
    events.push(item);
    current = applyEnvelope(current, {
      v: 2,
      session_id: "s1",
      generation: RUNTIME,
      request_id: "",
      seq: events.length,
      ...item,
    } as Envelope);
  };
  const running = (session: SessionView) =>
    runningAgents({ sessions: { s1: session }, others: [], status: "open" });
  receive({ type: "turn.started", data: { turn_id: "t0" } });
  receive(started);
  receive({ ...started, data: { ...started.data, block_id: "r2", agent: "curator" } });
  receive({ type: "subagent.done", data: { block_id: "r2", status: "completed" } });
  receive({ type: "subagent.done", data: { block_id: "r1", status: "stopped" } });
  receive({
    type: "turn.done",
    data: {
      status: "success",
      usage: { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_creation_tokens: 0 },
      context_percent: null,
      duration_ms: 1,
    },
  });
  const sibling = current.turns[0]?.blocks[1];
  expect(groupRuns({ s1: current }, NO_REVIEWED).done.map((run) => run.key)).toEqual(["s1:r1"]);
  expect(running(current)).toEqual([]);
  expect(presence(current)).toBe("idle");

  receive(started);
  receive(started);
  const groups = groupRuns({ s1: current }, NO_REVIEWED);
  expect(groups.working.map((run) => run.key)).toEqual(["s1:r1"]);
  expect(groups.working[0]?.background).toBe(true);
  expect(groups.review.map((run) => run.key)).toEqual(["s1:r2"]);
  expect(groups.done).toEqual([]);
  expect(current.turns[0]?.blocks[1]).toBe(sibling);
  expect(running(current)).toEqual(["researcher"]);
  expect(rosterRunning(roster, running(current))).toEqual(["researcher"]);
  expect(presence(current)).toBe("idle");

  const rebuilt = applySnapshot(emptyView("s1"), {
    v: 2,
    type: "session.snapshot",
    session_id: "s1",
    generation: RUNTIME,
    request_id: "",
    seq: events.length,
    data: {
      events: events.map((item, index) =>
        item.type === "subagent.done" ? { ...item, source_seq: index + 1 } : item,
      ),
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
  });
  expect(rebuilt.turns).toEqual(current.turns);
  expect(groupRuns({ s1: rebuilt }, NO_REVIEWED)).toEqual(groups);
  expect(rosterRunning(roster, running(rebuilt))).toEqual(["researcher"]);

  const history = applyHistoryPage(emptyView("s1"), { events, next_before_uuid: null });
  const oldGroups = groupRuns({ s1: history }, NO_REVIEWED);
  expect(oldGroups.working).toEqual([]);
  expect(oldGroups.review).toEqual([]);
  expect(oldGroups.done.map((run) => run.key)).toEqual(["s1:r2", "s1:r1"]);
  expect(rosterRunning(roster, running(history))).toEqual([]);

  receive({ type: "subagent.done", data: { block_id: "r1", status: "completed" } });
  const completed = groupRuns({ s1: current }, NO_REVIEWED);
  expect(completed.working).toEqual([]);
  expect(completed.review.map((run) => run.key)).toEqual(["s1:r2", "s1:r1"]);
  expect(groupRuns({ s1: current }, new Set(["s1:r1"])).done.map((run) => run.key)).toEqual([
    "s1:r1",
  ]);
  expect(running(current)).toEqual([]);
  expect(rosterRunning(roster, running(current))).toEqual([]);
});

test("a failed live run not yet reviewed is reviewed, exactly like a completed one", () => {
  const sessions = {
    s1: view("s1", {
      turns: [turn("t0", [routing("r1", { status: "failed" })])],
    }),
  };
  const groups = groupRuns(sessions, NO_REVIEWED);
  expect(groups.review.map((r) => r.key)).toEqual(["s1:r1"]);
  expect(groups.done).toEqual([]);
});

test("background is true only once the run's own turn has ended while it keeps running", () => {
  const sameTurn = {
    s1: view("s1", { turns: [turn("t0", [routing("r1")], { status: "running" })] }),
  };
  expect(groupRuns(sameTurn, NO_REVIEWED).working[0]?.background).toBe(false);

  const endedTurn = {
    s1: view("s1", { turns: [turn("t0", [routing("r1")], { status: "success" })] }),
  };
  expect(groupRuns(endedTurn, NO_REVIEWED).working[0]?.background).toBe(true);
});

test("a row whose session has closed while its block still runs is ended: done, background forced false", () => {
  const sessions = {
    s1: view("s1", {
      closed: true,
      turns: [turn("t0", [routing("r1")], { status: "success" })], // turn over, block still "running"
    }),
  };
  const groups = groupRuns(sessions, NO_REVIEWED);
  expect(groups.done.map((r) => r.key)).toEqual(["s1:r1"]);
  expect(groups.needs).toEqual([]);
  expect(groups.working).toEqual([]);
  // The turn having ended would otherwise make this row `background: true` (previous test); an
  // ended row forces it back to false instead.
  expect(groups.done[0]?.ended).toBe(true);
  expect(groups.done[0]?.background).toBe(false);
});

// --- runCounts -----------------------------------------------------------------------------------

function row(block: RoutingBlock, overrides: Partial<RunRow> = {}): RunRow {
  return {
    key: runKey("s1", block.id),
    sessionId: "s1",
    turnId: "t0",
    block,
    background: false,
    live: true,
    ended: false,
    waitingSince: null,
    requests: [],
    ...overrides,
  };
}

test("runCounts for a running row shows elapsed time, tool uses and tokens once stats are known", () => {
  const block = routing("r1", {
    tools: [
      {
        id: "tr1",
        tool: "Read",
        row: { head: "Read", rest: "a.md", tail: "" },
        done: true,
        isError: false,
        summary: "",
        preview: "",
      },
    ],
  });
  const text = runCounts(row(block), { startedAt: 1_000, tokens: 1_234 }, 1_000 + 65_000);
  expect(text).toBe("1m 5s · 1 tool use · ~1.2k tokens");
});

test("runCounts for a running row omits elapsed time and tokens while stats are not known yet", () => {
  const block = routing("r1");
  expect(runCounts(row(block), undefined, 5_000)).toBe("");
  expect(runCounts(row(block), { startedAt: null, tokens: null }, 5_000)).toBe("");
});

test("runCounts marks a backgrounded running row", () => {
  const block = routing("r1");
  expect(runCounts(row(block, { background: true }), undefined, 0)).toBe("in background");
});

test("runCounts leads a running row's line with how long it has waited, once waitingSince is known", () => {
  const block = routing("r1");
  const text = runCounts(row(block, { waitingSince: 5_000 }), undefined, 5_000 + 65_000);
  expect(text).toBe("waiting 1m 5s");
});

test("runCounts for a finished row leads with its status word and the CLI's own counts", () => {
  const done = routing("r1", {
    status: "completed",
    toolUses: 3,
    totalTokens: 500,
    durationMs: 42_000,
  });
  expect(runCounts(row(done), undefined, 0)).toBe("Done · 3 tool uses · 500 tokens · 42s");

  const failed = routing("r1", { status: "failed", toolUses: 1 });
  expect(runCounts(row(failed), undefined, 0)).toBe("Failed · 1 tool use");

  const stopped = routing("r1", { status: "stopped", toolUses: 2 });
  expect(runCounts(row(stopped), undefined, 0)).toBe("Stopped · 2 tool uses");
});

test("runCounts for a finished row without CLI counts falls back to its nested tool count", () => {
  const block = routing("r1", {
    status: "completed",
    tools: [
      {
        id: "tr1",
        tool: "Read",
        row: { head: "Read", rest: "a.md", tail: "" },
        done: true,
        isError: false,
        summary: "",
        preview: "",
      },
      {
        id: "tr2",
        tool: "Read",
        row: { head: "Read", rest: "b.md", tail: "" },
        done: true,
        isError: false,
        summary: "",
        preview: "",
      },
    ],
  });
  expect(runCounts(row(block), undefined, 0)).toBe("Done · 2 tool uses");
});

test("runCounts for a finished row with neither CLI counts nor nested tools shows only the status word", () => {
  const block = routing("r1", { status: "completed" });
  expect(runCounts(row(block), undefined, 0)).toBe("Done");
});

test("runCounts for an ended row reads like a stopped one: status word plus the nested tool count", () => {
  // Still "running" by block status (its CLI process is gone, so it has no CLI toolUses either) —
  // `ended` must override the running branch, not the CLI-counts branch.
  const block = routing("r1", {
    status: "running",
    tools: [
      {
        id: "tr1",
        tool: "Read",
        row: { head: "Read", rest: "a.md", tail: "" },
        done: true,
        isError: false,
        summary: "",
        preview: "",
      },
      {
        id: "tr2",
        tool: "Read",
        row: { head: "Read", rest: "b.md", tail: "" },
        done: true,
        isError: false,
        summary: "",
        preview: "",
      },
    ],
  });
  const ended = row(block, { ended: true, waitingSince: 1_000, background: true });
  expect(runCounts(ended, { startedAt: 0, tokens: 10 }, 999_999)).toBe("Stopped · 2 tool uses");
});

// --- runLine -------------------------------------------------------------------------------------

test("runLine on a row with one pending request names its tool and summary, with no more-count", () => {
  const block = routing("r1");
  const withRequest = row(block, {
    requests: [permissionCard("req-1", "researcher", { tool: "Write", summary: "a.md" }).data],
  });
  expect(runLine(withRequest)).toBe("Waiting for approval · Write a.md");
});

test("runLine on a row with several pending requests adds how many more are waiting", () => {
  const block = routing("r1");
  const requests = [
    permissionCard("req-1", "researcher", { tool: "Write", summary: "a.md" }).data,
    permissionCard("req-2", "researcher", { tool: "Edit", summary: "b.md" }).data,
    permissionCard("req-3", "researcher", { tool: "Bash", summary: "ls" }).data,
  ];
  expect(runLine(row(block, { requests }))).toBe("Waiting for approval · Write a.md (+2 more)");
});

test("runLine on a running row with no requests shows its newest nested tool row, trailing an ellipsis while it is not done", () => {
  const running = routing("r1", {
    tools: [
      {
        id: "tr1",
        tool: "Read",
        row: { head: "Read", rest: "a.md", tail: "" },
        done: true,
        isError: false,
        summary: "",
        preview: "",
      },
      {
        id: "tr2",
        tool: "Grep",
        row: { head: "rg", rest: "-il linter", tail: "" },
        done: false,
        isError: false,
        summary: "",
        preview: "",
      },
    ],
  });
  expect(runLine(row(running))).toBe("rg -il linter …");
});

test("runLine on a running row stops the ellipsis once its newest nested tool row is done", () => {
  const running = routing("r1", {
    tools: [
      {
        id: "tr1",
        tool: "Read",
        row: { head: "Read", rest: "a.md", tail: "" },
        done: true,
        isError: false,
        summary: "",
        preview: "",
      },
    ],
  });
  expect(runLine(row(running))).toBe("Read a.md");
});

test("runLine on a running row whose newest nested tool row has no rest shows the head alone", () => {
  const running = routing("r1", {
    tools: [
      {
        id: "tr1",
        tool: "ls",
        row: { head: "ls", rest: "", tail: "" },
        done: true,
        isError: false,
        summary: "",
        preview: "",
      },
    ],
  });
  expect(runLine(row(running))).toBe("ls");
});

test("runLine on a running row with no nested tool rows yet is empty", () => {
  expect(runLine(row(routing("r1")))).toBe("");
});

test("runLine on a finished row shows its summary, or empty when it has none", () => {
  const withSummary = routing("r1", { status: "completed", summary: "Found the template." });
  expect(runLine(row(withSummary))).toBe("Found the template.");

  const withoutSummary = routing("r1", { status: "completed" });
  expect(runLine(row(withoutSummary))).toBe("");
});

test("runLine on an ended row shows its summary, ignoring any pending requests", () => {
  const block = routing("r1", { status: "running", summary: "Found the template." });
  const ended = row(block, {
    ended: true,
    requests: [permissionCard("req-1", "researcher", { tool: "Write", summary: "a.md" }).data],
  });
  expect(runLine(ended)).toBe("Found the template.");

  const withoutSummary = row(routing("r1", { status: "running" }), { ended: true });
  expect(runLine(withoutSummary)).toBe("");
});

// --- runElapsed ------------------------------------------------------------------------------------

test("runElapsed under a minute shows seconds alone", () => {
  expect(runElapsed(0)).toBe("0s");
  expect(runElapsed(45_000)).toBe("45s");
  expect(runElapsed(59_999)).toBe("59s");
});

test("runElapsed under an hour shows minutes and seconds", () => {
  expect(runElapsed(60_000)).toBe("1m 0s");
  expect(runElapsed(65_000)).toBe("1m 5s");
  expect(runElapsed(3_599_000)).toBe("59m 59s");
});

test("runElapsed at an hour or more shows hours and minutes", () => {
  expect(runElapsed(3_600_000)).toBe("1h 0m");
  expect(runElapsed(3_900_000)).toBe("1h 5m");
  expect(runElapsed(7_260_000)).toBe("2h 1m");
});
