/**
 * The unload guard: what counts as work the user would lose by closing the window.
 * Views are built through the real reducer, so the rules are pinned against real event shapes.
 */
import { expect, test } from "vitest";
import type { Envelope } from "../protocol";
import { applyEnvelope } from "./sessionReducer";
import type { AppSnapshot } from "./store";
import { unsavedWork } from "./unload";
import { emptyView, type SessionView } from "./view";

const AT = "2026-09-12T08:00:00.000Z";
const usage = { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_creation_tokens: 0 };

interface Item {
  type: string;
  data: Record<string, unknown>;
}

function run(items: Item[], sessionId = "s1"): SessionView {
  return items
    .map((item, i) => ({ v: 1, session_id: sessionId, seq: i + 1, ...item }) as unknown as Envelope)
    .reduce(applyEnvelope, emptyView(sessionId));
}

const turn: Item[] = [
  { type: "user.message", data: { text: "hi", command: "", at: AT } },
  { type: "turn.started", data: { turn_id: "t1" } },
];

function tool(id: string, name: string): Item {
  return {
    type: "tool.started",
    data: { block_id: id, tool: name, row: { head: name, rest: "x", tail: "" }, parent_id: "" },
  };
}

const done: Item = {
  type: "turn.done",
  data: { status: "success", usage, context_percent: null, duration_ms: 1 },
};

function snapshot(...views: SessionView[]): AppSnapshot {
  return {
    sessions: Object.fromEntries(views.map((view) => [view.sessionId, view])),
    others: [],
    status: "open",
  };
}

test("a finished turn never blocks the window, whatever tools it ran", () => {
  const view = run([...turn, tool("b1", "Write"), done]);
  expect(unsavedWork(snapshot(view))).toBe(false);
});

test("a running turn blocks the unload", () => {
  expect(unsavedWork(snapshot(run(turn)))).toBe(true);
});

test("an open card blocks the unload after the turn ended", () => {
  const view = run([
    ...turn,
    {
      type: "permission.requested",
      data: { tool_use_id: "u1", kind: "tool", tool: "Bash", summary: "ls", reason: "", agent: "" },
    },
    done,
  ]);
  // turn.done clears cards, so raise one after it: the user is still being asked.
  const waiting = applyEnvelope(view, {
    v: 1,
    session_id: "s1",
    seq: 99,
    type: "permission.requested",
    data: { tool_use_id: "u2", kind: "tool", tool: "Bash", summary: "ls", reason: "", agent: "" },
  } as unknown as Envelope);
  expect(waiting.cards).toHaveLength(1);
  expect(unsavedWork(snapshot(waiting))).toBe(true);
});

test("a session closed mid-turn stops blocking the window", () => {
  const running = run(turn);
  expect(unsavedWork(snapshot(running))).toBe(true);
  const closed = applyEnvelope(running, {
    v: 1,
    session_id: "s1",
    seq: 50,
    type: "session.closed",
    data: { reason: "browser_gone" },
  } as unknown as Envelope);
  expect(closed.turns.at(-1)?.status).toBe("running"); // the reducer leaves it as it was
  expect(unsavedWork(snapshot(closed))).toBe(false);
});

test("one busy session among idle ones is enough", () => {
  const idle = run([...turn, done], "s1");
  const busy = run(turn, "s2");
  expect(unsavedWork(snapshot(idle, busy))).toBe(true);
  expect(unsavedWork(snapshot(idle))).toBe(false);
  expect(unsavedWork({ sessions: {}, others: [], status: "open" })).toBe(false);
});
