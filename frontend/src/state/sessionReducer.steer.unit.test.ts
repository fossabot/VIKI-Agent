/**
 * Unit tests for the steer events the reducer folds: SessionView.steers (the waiting rows)
 * and Turn.steered (a turn Claude Code folded a later message into before it finished on its
 * own) — see sessionReducer.ts's steer.pending/steer.ended/user.message cases.
 */
import { expect, test } from "vitest";
import {
  AT,
  type Item,
  textDone,
  turnDone,
  turnStarted,
  userMessage,
  view,
} from "../accept/m2b/fakes";
import type { Envelope, SessionSnapshot } from "../protocol";
import { applyEnvelope, applySnapshot } from "./sessionReducer";
import { emptyView, type SessionView } from "./view";

const SID = "s1";
const GEN = "11111111-1111-4111-8111-111111111111";

function steerPending(steerId: string, text: string, images = 0, at = AT): Item {
  return { type: "steer.pending", data: { steer_id: steerId, text, command: "", images, at } };
}

function steerEnded(steerId: string, outcome: "started" | "dropped"): Item {
  return { type: "steer.ended", data: { steer_id: steerId, outcome } };
}

/* --- S1: folded into the turn Claude Code was already running ---------------------------------- */

test("S1: a steer that folds into the running turn closes it and opens `<id>:steer`, which turn.done then closes", () => {
  const running = view([userMessage("hi"), turnStarted]);
  expect(running.turns[0]).toMatchObject({ id: "t1", status: "running" });

  const pending = view([steerPending("s1", "also check Y")], running);
  expect(pending.steers).toEqual([
    { steer_id: "s1", text: "also check Y", command: "", images: 0, at: AT },
  ]);

  // Claude Code reads it: steer.ended{started} clears the waiting row, then the echo arrives.
  // No turn.started follows it — it folded into "t1" instead of opening a new one.
  const folded = view([steerEnded("s1", "started"), userMessage("also check Y")], pending);
  expect(folded.steers).toEqual([]);
  expect(folded.turns.map((t) => [t.id, t.status, t.steered ?? null])).toEqual([
    ["t1", "success", true],
    ["t1:steer", "running", null],
  ]);

  // turn.done carries no turn_id of its own; it still finds and closes the right turn — the
  // last one — exactly as it does for any other turn.
  const ended = view([turnDone("success")], folded);
  expect(ended.turns.map((t) => [t.id, t.status])).toEqual([
    ["t1", "success"],
    ["t1:steer", "success"],
  ]);
});

/* --- S2: the previous turn had already ended ----------------------------------------------------- */

test("S2: a steer after the turn already ended opens an ordinary new turn, same as any send", () => {
  const done = view([userMessage("hi"), turnStarted, textDone("done"), turnDone("success")]);
  expect(done.turns[0]?.status).toBe("success");

  const pending = view([steerPending("s2", "now check Z")], done);
  expect(pending.steers).toHaveLength(1);

  const after = view(
    [
      steerEnded("s2", "started"),
      userMessage("now check Z"),
      { type: "turn.started", data: { turn_id: "t2" } },
    ],
    pending,
  );
  expect(after.steers).toEqual([]);
  expect(after.turns.map((t) => [t.id, t.status, t.steered ?? null])).toEqual([
    ["t1", "success", null],
    ["t2", "running", null],
  ]);
});

/* --- dropped: never reached Claude Code ------------------------------------------------------------ */

test("a dropped steer clears the waiting row and leaves a not-sent notice in the last turn", () => {
  const running = view([userMessage("hi"), turnStarted]);
  const pending = view([steerPending("s3", "never arrives")], running);
  const dropped = view([steerEnded("s3", "dropped")], pending);
  expect(dropped.steers).toEqual([]);
  expect(dropped.turns[0]?.blocks).toEqual([
    {
      kind: "notice",
      id: "steer-s3",
      tone: "error",
      subject: "not_sent",
      detail: "This message didn't reach Claude Code. Find its text in Drafts and send history.",
    },
  ]);
});

/* --- session.closed --------------------------------------------------------------------------------- */

test("session.closed empties the waiting steers along with the cards", () => {
  const running = view([userMessage("hi"), turnStarted]);
  const pending = view([steerPending("s4", "x")], running);
  expect(pending.steers).toHaveLength(1);
  const closed = view([{ type: "session.closed", data: { reason: "requested" } }], pending);
  expect(closed.steers).toEqual([]);
  expect(closed.closed).toBe(true);
});

/* --- reconnect equality: applySnapshot of the server's bucketed items == the live fold ----------- */

function envelopes(items: Item[], first = 1): Envelope[] {
  return items.map(
    (item, i) =>
      ({
        v: 2,
        generation: GEN,
        request_id: "",
        session_id: SID,
        seq: first + i,
        ...item,
      }) as unknown as Envelope,
  );
}

function fold(start: SessionView, envs: Envelope[]): SessionView {
  return envs.reduce(applyEnvelope, start);
}

test("reconnect equality: a snapshot bucketing turn records, waiting steers, then cards, matches the live fold", () => {
  const permission: Item = {
    type: "permission.requested",
    data: {
      tool_use_id: "p1",
      kind: "tool",
      tool: "Bash",
      summary: "ls",
      reason: "",
      agent: "",
      plan: "",
    },
  };
  const pendingSteer = steerPending("s5", "add Y");

  // Chronological arrival: the card opens, then the user steers while it is still open.
  const live = fold(
    emptyView(SID),
    envelopes([userMessage("hi"), turnStarted, permission, pendingSteer]),
  );
  expect(live.cards).toHaveLength(1);
  expect(live.steers).toHaveLength(1);

  // The snapshot buckets the same items turn records first, then waiting steers, then cards
  // (stream_state.py's snapshot order) — not arrival order.
  const snapshot = {
    v: 2,
    type: "session.snapshot",
    session_id: SID,
    generation: GEN,
    request_id: "",
    seq: live.seq,
    data: {
      events: [
        { type: "user.message", data: { text: "hi", command: "", at: AT } },
        { type: "turn.started", data: { turn_id: "t1" } },
        pendingSteer,
        permission,
      ],
      state: {
        live: true,
        info: null,
        mode: "",
        blocked: null,
        closed: false,
        error: null,
        compacting: false,
        context_percent: null,
        rate_limit: null,
      },
      truncated: false,
    },
  } as unknown as SessionSnapshot;

  expect(applySnapshot(emptyView(SID), snapshot)).toEqual(live);
});
