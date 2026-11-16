/**
 * Acceptance (frozen): the reducer against the Python goldens, the reconnect property,
 * and the rules in sessionReducer.ts.
 *
 * AMENDED: the card test below asserted that an answered card closes when its TOOL
 * finishes, on the stated premise that "a reload replays from seq 0 and never sees the answer".
 * That premise was true and the behaviour it produced was wrong — tool.done arrives when the tool
 * ENDS, so a reload, a replay or a second window kept showing an answered card, with live buttons
 * the server silently ignores, for as long as the tool ran. The server now emits
 * `request.answered` the moment the user chooses. The old assertion is kept (tool.done must
 * still close a card, for the case where this app never saw the answer at all — another window
 * gave it) and the new event is pinned beside it.
 */
import { describe, expect, test } from "vitest";
import type { Envelope, HistoryPage } from "../protocol";
import { answerCard, applyEnvelope, applyHistoryPage } from "./sessionReducer";
import { emptyView, presence, type RoutingBlock, type SessionView, type ToolBlock } from "./view";

const SID = "s1";
const AT = "2026-09-11T08:00:00.000Z";

interface Item {
  type: string;
  data: Record<string, unknown>;
}
interface Golden {
  fixture?: string;
  transcript?: string;
  events: Item[];
}

const files = import.meta.glob<Golden>("../../../tests/contracts/events/*.json", {
  eager: true,
  import: "default",
});
const goldens = Object.entries(files).map(([path, g]): [string, Golden] => [
  path.split("/").at(-1) ?? path,
  g,
]);
const live = goldens.filter(([, g]) => g.fixture);
const history = goldens.filter(([, g]) => g.transcript);

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

function fold(view: SessionView, envs: Envelope[]): SessionView {
  return envs.reduce(applyEnvelope, view);
}

function thirds(text: string): string[] {
  const size = Math.ceil(text.length / 3);
  return [text.slice(0, size), text.slice(size, 2 * size), text.slice(2 * size)].filter(Boolean);
}

/** A live golden as the browser receives it: user.message and turn.started open each turn,
 * and every merged delta arrives in three pieces. */
function asReceived(golden: Golden): Item[] {
  const items: Item[] = [];
  let turn = 0;
  let open = false;
  for (const event of golden.events) {
    if (!open) {
      turn += 1;
      items.push({ type: "user.message", data: { text: `prompt ${turn}`, command: "", at: AT } });
      items.push({ type: "turn.started", data: { turn_id: `t${turn}` } });
      open = true;
    }
    if (event.type === "text.delta" || event.type === "thinking.delta") {
      for (const part of thirds(String(event.data.text))) {
        items.push({ type: event.type, data: { ...event.data, text: part } });
      }
    } else {
      items.push(event);
    }
    if (event.type === "turn.done") open = false;
  }
  return items;
}

/** The server's log at some moment: deltas of blocks already finished are compacted away. */
function compact(envs: Envelope[]): Envelope[] {
  const done = new Set(
    envs
      .filter((e) => e.type === "text.done" || e.type === "thinking.done")
      .map((e) => (e.data as { block_id: string }).block_id),
  );
  return envs.filter(
    (e) =>
      !(
        (e.type === "text.delta" || e.type === "thinking.delta") &&
        done.has((e.data as { block_id: string }).block_id)
      ),
  );
}

function allBlocks(view: SessionView) {
  return view.turns.flatMap((t) => t.blocks);
}

function allRows(view: SessionView) {
  return allBlocks(view).flatMap((b) =>
    b.kind === "tools" ? b.rows : b.kind === "routing" ? b.tools : [],
  );
}

// --- goldens ------------------------------------------------------------------------------------

describe.each(live)("live golden %s", (_name, golden) => {
  const envs = envelopes(asReceived(golden));
  const final = fold(emptyView(SID), envs);

  test("every turn ends with its turn.done status", () => {
    const dones = golden.events.filter((e) => e.type === "turn.done");
    expect(final.turns.map((t) => t.status)).toEqual(dones.map((e) => e.data.status));
    expect(final.turns.map((t) => t.id)).toEqual(dones.map((_, i) => `t${i + 1}`));
    expect(final.seq).toBe(envs.length);
  });

  test("finished blocks carry their final text", () => {
    for (const event of golden.events.filter((e) => e.type.endsWith(".done"))) {
      const id = String(event.data.block_id);
      if (event.type === "text.done" || event.type === "thinking.done") {
        const block = allBlocks(final).find((b) => b.id === id);
        expect(block).toMatchObject({ text: event.data.text, done: true });
      }
      if (event.type === "tool.done") {
        const row = allRows(final).find((r) => r.id === id);
        expect(row).toMatchObject({
          done: true,
          summary: event.data.summary,
          isError: event.data.is_error,
        });
      }
      if (event.type === "subagent.done") {
        const routing = allBlocks(final).find((b) => b.id === id) as RoutingBlock;
        expect(routing.status).toBe(event.data.status);
      }
    }
  });

  test("a client that drops after any event and reconnects ends up identical", () => {
    for (let t = 0; t <= envs.length; t++) {
      const log = compact(envs.slice(0, t));
      for (let k = 0; k <= t; k++) {
        let client = fold(emptyView(SID), envs.slice(0, k));
        client = fold(
          client,
          log.filter((e) => e.seq > k),
        ); // the replay after session.open
        client = fold(client, envs.slice(t)); // then live again
        expect(client, `dropped after ${k}, reconnected at ${t}`).toEqual(final);
      }
    }
  });
});

test("the subagent golden nests the subagent's tools under its routing row", () => {
  const [, golden] = live.find(([name]) => name.startsWith("subagent-explore")) ?? [];
  const final = fold(emptyView(SID), envelopes(asReceived(golden as Golden)));
  const [turn] = final.turns;
  expect(turn?.blocks.map((b) => b.kind)).toEqual(["routing", "text"]);
  const routing = turn?.blocks[0] as RoutingBlock;
  expect(routing).toMatchObject({ agent: "Explore", model: "haiku", status: "completed" });
  expect(routing.tools.map((r) => [r.tool, r.row.tail, r.isError])).toEqual([
    ["Bash", "→ exit 2", true],
    ["PowerShell", "", false],
  ]);
});

describe.each(history)("history golden %s", (_name, golden) => {
  test("becomes finished history turns", () => {
    const page = {
      events: golden.events,
      next_before_uuid: null,
    } as unknown as HistoryPage["data"];
    const view = applyHistoryPage(emptyView(SID), page);
    expect(view.turns).toHaveLength(1);
    expect(view.turns[0]).toMatchObject({ id: "h-start-0", history: true, status: "success" });
    expect(view.turns[0]?.user?.text).toBe(golden.events[0]?.data.text);
    expect(view.historyCursor).toBeNull();
    expect(view.seq).toBe(0);
  });
});

// --- rules --------------------------------------------------------------------------------------

function run(items: Item[], start: SessionView = emptyView(SID)): SessionView {
  return fold(start, envelopes(items, start.seq + 1));
}

const userTurn: Item[] = [
  { type: "user.message", data: { text: "hi", command: "", at: AT } },
  { type: "turn.started", data: { turn_id: "t1" } },
];

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

test("duplicates and other sessions' envelopes return the same view", () => {
  const view = run(userTurn);
  const [again] = envelopes(userTurn.slice(1), 2);
  expect(applyEnvelope(view, again as Envelope)).toBe(view);
  const foreign = { ...(envelopes(userTurn, 3)[0] as Envelope), session_id: "other" } as Envelope;
  expect(applyEnvelope(view, foreign)).toBe(view);
});

test("consecutive tools share a block; text splits them", () => {
  const view = run([
    ...userTurn,
    toolStarted("a"),
    toolStarted("b"),
    { type: "text.done", data: { block_id: "m:1", text: "between" } },
    toolStarted("c"),
    toolDone("b", "→ 7 files"),
  ]);
  const blocks = view.turns[0]?.blocks ?? [];
  expect(blocks.map((b) => b.kind)).toEqual(["tools", "text", "tools"]);
  expect((blocks[0] as ToolBlock).rows.map((r) => r.id)).toEqual(["a", "b"]);
  expect((blocks[0] as ToolBlock).rows[1]).toMatchObject({
    done: true,
    row: { tail: "→ 7 files" },
  });
  expect(blocks[0]?.id).toBe("a");
});

test("a subagent's tools nest under its routing row and finish there", () => {
  const view = run([
    ...userTurn,
    {
      type: "subagent.started",
      data: {
        block_id: "ag",
        agent: "researcher",
        model: "opus",
        description: "d",
        reason: "r",
      },
    },
    toolStarted("x", "ag"),
    toolDone("x"),
    { type: "subagent.done", data: { block_id: "ag", status: "completed" } },
  ]);
  const routing = view.turns[0]?.blocks[0] as RoutingBlock;
  expect(routing).toMatchObject({ kind: "routing", reason: "r", status: "completed" });
  expect(routing.tools).toEqual([
    {
      id: "x",
      tool: "Read",
      row: { head: "Read", rest: "x", tail: "" },
      done: true,
      isError: false,
      summary: "ok",
      preview: "ok",
    },
  ]);
});

test("cards open, deduplicate, cancel and close on answer; presence follows", () => {
  const question = { type: "question.asked", data: { tool_use_id: "q1", questions: [] } };
  const permission = {
    type: "permission.requested",
    data: {
      tool_use_id: "p1",
      kind: "tool",
      tool: "Bash",
      summary: "rm x",
      reason: "",
      agent: "",
      plan: "",
    },
  };
  let view = run([...userTurn, question, question, permission]);
  expect(view.cards.map((c) => [c.kind, c.data.tool_use_id])).toEqual([
    ["question", "q1"],
    ["permission", "p1"],
  ]);
  expect(view.cards[1]).toEqual({ kind: "permission", nativeRequest: null, data: permission.data });
  expect(presence(view)).toBe("waiting");
  view = run([{ type: "request.cancelled", data: { tool_use_id: "q1" } }], view);
  view = answerCard(view, "p1");
  expect(view.cards).toEqual([]);
  expect(presence(view)).toBe("writing");
});

test("an answered card closes on the answer, and on its tool finishing, so a replay never reopens it", () => {
  const permission = {
    type: "permission.requested",
    data: { tool_use_id: "p1", kind: "tool", tool: "Bash", summary: "ls", reason: "" },
  };
  const asked = run([...userTurn, toolStarted("p1", "", "Bash"), permission]);
  expect(asked.cards).toHaveLength(1);
  // The answer itself is in the stream, so a replay closes the card at the point they chose — not
  // when the tool it authorised eventually finishes. That gap is the whole bug: a Bash that runs
  // for thirty seconds used to leave this card on screen, in every other window, for thirty
  // seconds, with buttons the server refuses because the future is already resolved.
  expect(run([{ type: "request.answered", data: { tool_use_id: "p1" } }], asked).cards).toEqual([]);
  // tool.done still closes it too, and must: this window may never have seen the answer at all,
  // because another window gave it before this one connected.
  expect(run([toolDone("p1")], asked).cards).toEqual([]);
  const question = { type: "question.asked", data: { tool_use_id: "q1", questions: [] } };
  const usage = {
    input_tokens: 0,
    output_tokens: 0,
    cache_read_tokens: 0,
    cache_creation_tokens: 0,
  };
  const ended = run([
    ...userTurn,
    question,
    {
      type: "turn.done",
      data: { status: "success", usage, context_percent: null, duration_ms: 1 },
    },
  ]);
  expect(ended.cards).toEqual([]); // nothing can wait for the user once the turn is over
  const closed = run([
    ...userTurn,
    question,
    { type: "session.closed", data: { reason: "requested" } },
  ]);
  expect(closed.cards).toEqual([]);
});

test("a turn the CLI starts by itself opens a new turn with no owner message", () => {
  const usage = {
    input_tokens: 0,
    output_tokens: 0,
    cache_read_tokens: 0,
    cache_creation_tokens: 0,
  };
  const done = {
    type: "turn.done",
    data: { status: "success", usage, context_percent: null, duration_ms: 1 },
  };
  const view = run([
    ...userTurn,
    done,
    { type: "turn.started", data: { turn_id: "t2" } }, // a background agent finished
    { type: "text.done", data: { block_id: "m2:0", text: "daily.md" } },
    done,
  ]);
  expect(view.turns.map((t) => [t.id, t.user?.text ?? null, t.status])).toEqual([
    ["t1", "hi", "success"],
    ["t2", null, "success"],
  ]);
});

test("notices, status and compaction", () => {
  const view = run([
    ...userTurn,
    { type: "permission.denied", data: { tool: "Bash", reason: "blocked by auto mode" } },
    { type: "hook", data: { name: "PreToolUse:Bash", outcome: "blocked", message: "VIKI guard" } },
    { type: "status", data: { state: "compacting", mode: "plan" } },
  ]);
  expect(view.compacting).toBe(true);
  expect(view.mode).toBe("plan");
  const notices = view.turns[0]?.blocks;
  expect(notices).toEqual([
    {
      kind: "notice",
      id: "permission.denied-3",
      tone: "denied",
      subject: "Bash",
      detail: "blocked by auto mode",
    },
    {
      kind: "notice",
      id: "hook-4",
      tone: "hook",
      subject: "PreToolUse:Bash",
      detail: "VIKI guard",
    },
  ]);
  const after = run([{ type: "compacted", data: { trigger: "auto", pre_tokens: 1 } }], view);
  expect(after.compacting).toBe(false);
  expect(after.turns[0]?.blocks.at(-1)).toMatchObject({ tone: "compacted", subject: "auto" });
});

test("errors land in the running turn, or on the session once it has ended", () => {
  const crash = {
    type: "error",
    data: { code: "crashed", message: "It crashed", recoverable: true },
  };
  const running = run([...userTurn, crash]);
  expect(running.turns[0]?.blocks).toEqual([
    { kind: "notice", id: "error-3", tone: "error", subject: "crashed", detail: "It crashed" },
  ]);
  expect(running.error).toBeNull();
  const usage = {
    input_tokens: 1,
    output_tokens: 2,
    cache_read_tokens: 3,
    cache_creation_tokens: 4,
  };
  const ended = run([
    ...userTurn,
    { type: "turn.done", data: { status: "error", usage, context_percent: 12, duration_ms: 5 } },
    crash,
  ]);
  expect(ended.error).toEqual(crash.data);
  expect(ended.turns[0]).toMatchObject({ status: "error", usage, durationMs: 5 });
  expect(ended.contextPercent).toBe(12);
  const later = run(
    [
      ...userTurn.map((i) => ({ ...i })),
      {
        type: "turn.done",
        data: { status: "success", usage, context_percent: null, duration_ms: 1 },
      },
    ],
    ended,
  );
  expect(later.contextPercent).toBe(12); // null keeps the last known value
  expect(later.error).toBeNull(); // a new turn clears the session error
});

test("session lifecycle", () => {
  const started = {
    type: "session.started",
    data: {
      model: "default",
      mode: "auto",
      effort: "high",
      commands: [],
      agents: [],
      models: [],
      cli_version: "2.1.259",
      resumed: false,
    },
  };
  const holder = { session_id: SID, entrypoint: "claude-vscode", pid: 1, title: "" };
  let view = run([{ type: "session.blocked", data: { reason: "live_elsewhere", holder } }]);
  expect(view.blocked?.holder.entrypoint).toBe("claude-vscode");
  view = run([started], view);
  expect(view).toMatchObject({ blocked: null, mode: "auto", closed: false });
  expect(view.info?.cli_version).toBe("2.1.259");
  view = run([{ type: "session.closed", data: { reason: "requested" } }], view);
  expect(view.closed).toBe(true);
});

test("older history pages are prepended", () => {
  const page = (text: string, cursor: string | null): HistoryPage["data"] => ({
    events: [
      { type: "user.message", data: { text, command: "", at: AT } },
      { type: "text.done", data: { block_id: `m-${text}:0`, text: `re: ${text}` } },
    ],
    next_before_uuid: cursor,
  });
  let view = applyHistoryPage(emptyView(SID), page("newer", "u-1"));
  view = applyHistoryPage(view, page("older", null));
  expect(view.turns.map((t) => [t.id, t.user?.text])).toEqual([
    ["h-start-0", "older"],
    ["h-u-1-0", "newer"],
  ]);
  expect(view.historyCursor).toBeNull();
  expect(view.turns.every((t) => t.history && t.status === "success")).toBe(true);
});

test("presence: idle, writing, thinking", () => {
  expect(presence(emptyView(SID))).toBe("idle");
  const thinking = run([
    ...userTurn,
    { type: "thinking.delta", data: { block_id: "m:0", text: "hmm" } },
  ]);
  expect(presence(thinking)).toBe("thinking");
  const writing = run(
    [{ type: "thinking.done", data: { block_id: "m:0", text: "hmm", duration_ms: 5 } }],
    thinking,
  );
  expect(presence(writing)).toBe("writing");
});
