/**
 * collectDecisions: what the decision inbox is allowed to show, and nothing it is not — a
 * closed view or a card without a native request must never surface (hard rule 6).
 */
import { expect, test } from "vitest";
import { collectDecisions, type DecisionItem } from "./decisions";
import type { VaultProposalItem } from "./decisionsApi";
import type { Card, SessionView } from "./view";
import { emptyView } from "./view";

const RUNTIME = "33333333-3333-4333-8333-333333333333";
const TURN = "55555555-5555-4555-8555-555555555555";

function toolCard(requestId: string, summary = "30-knowledge/x.md"): Card {
  return {
    kind: "permission",
    data: {
      tool_use_id: `tu-${requestId}`,
      kind: "tool",
      tool: "Write",
      summary,
      question: "May I rewrite this file?",
      reason: "",
      agent: "",
      plan: "",
    },
    nativeRequest: { runtime_id: RUNTIME, request_id: requestId, turn_id: TURN },
  };
}

function planCard(requestId: string): Card {
  return {
    kind: "permission",
    data: {
      tool_use_id: `tu-${requestId}`,
      kind: "plan",
      tool: "ExitPlanMode",
      summary: "",
      question: "",
      reason: "",
      agent: "VIKI",
      plan: "1. do a thing",
    },
    nativeRequest: { runtime_id: RUNTIME, request_id: requestId, turn_id: TURN },
  };
}

function questionCard(requestId: string, question = "Which one should I use?"): Card {
  return {
    kind: "question",
    data: {
      tool_use_id: `tu-${requestId}`,
      questions: [{ header: "", question, options: [], multi_select: false }],
    },
    nativeRequest: { runtime_id: RUNTIME, request_id: requestId, turn_id: TURN },
  };
}

function view(sessionId: string, over: Partial<SessionView> = {}): SessionView {
  return { ...emptyView(sessionId), generation: "gen-1", ...over };
}

test("two sessions with cards give items in order with the right ids, origins and revisions", () => {
  const sessions = {
    a: view("a", { cards: [toolCard("req-1")] }),
    b: view("b", { cards: [toolCard("req-2"), questionCard("req-3")] }),
  };
  const items = collectDecisions(sessions, "ws-1");
  expect(items.map((item) => item.id)).toEqual(["a:tu-req-1", "b:tu-req-2", "b:tu-req-3"]);
  expect(items.map((item) => item.revision)).toEqual(["req-1", "req-2", "req-3"]);
  expect(items[0]?.origin).toEqual({ workspace_id: "ws-1", session_id: "a", turn_id: TURN });
  expect(items[1]?.origin).toEqual({ workspace_id: "ws-1", session_id: "b", turn_id: TURN });
});

test("a closed view and a card without a native request are absent", () => {
  const openCard = toolCard("req-1");
  const cardWithoutRequest: Card = { ...toolCard("req-2"), nativeRequest: null };
  const sessions = {
    closed: view("closed", { closed: true, cards: [toolCard("req-3")] }),
    noGeneration: { ...view("noGeneration", { cards: [toolCard("req-4")] }), generation: null },
    open: view("open", { cards: [openCard, cardWithoutRequest] }),
  };
  const items = collectDecisions(sessions, null);
  expect(items).toHaveLength(1);
  expect(items[0]?.id).toBe("open:tu-req-1");
});

test("the actor falls back to VIKI when the card names no agent", () => {
  const sessions = { a: view("a", { cards: [toolCard("req-1")] }) };
  expect(collectDecisions(sessions, null)[0]?.actor).toBe("VIKI");
});

test("the actor is the card's own agent when it names one", () => {
  const card = toolCard("req-1");
  if (card.kind === "permission") card.data.agent = "Explore";
  const sessions = { a: view("a", { cards: [card] }) };
  expect(collectDecisions(sessions, null)[0]?.actor).toBe("Explore");
});

test("a plan card and a question are not inline, a tool permission is", () => {
  const sessions = {
    a: view("a", { cards: [toolCard("req-1"), planCard("req-2"), questionCard("req-3")] }),
  };
  const items = collectDecisions(sessions, null);
  expect(items.map((item) => item.inline)).toEqual([true, false, false]);
  expect(items[1]?.kind).toBe("permission");
  expect(items[2]?.kind).toBe("question");
  expect(items[2]?.action).toBe("AskUserQuestion");
  expect(items[2]?.resource).toBe("Which one should I use?");
});

test("the workspace id passes through", () => {
  const sessions = { a: view("a", { cards: [toolCard("req-1")] }) };
  expect(collectDecisions(sessions, "ws-9")[0]?.origin.workspace_id).toBe("ws-9");
  expect(collectDecisions(sessions, null)[0]?.origin.workspace_id).toBeNull();
});

test("a 500-code-point summary is cut to 200 without splitting a surrogate pair", () => {
  const astral = "\u{20000}"; // one code point, two UTF-16 units
  const summary = "a".repeat(199) + astral + "b".repeat(300);
  const sessions = { a: view("a", { cards: [toolCard("req-1", summary)] }) };
  const [item] = collectDecisions(sessions, null);
  expect(Array.from(item?.resource ?? "")).toHaveLength(200);
  expect(item?.resource.length).toBe(201); // the astral code point counts as two UTF-16 units
  expect(item?.resource.endsWith(astral)).toBe(true);
});

const HASH = "a".repeat(64);

const vaultProposal: VaultProposalItem = {
  kind: "vault_proposal",
  id: "p-1",
  origin: { workspace_id: "ws-1", session_id: null },
  revision: HASH,
  state: "open",
  attention: true,
  title: "a.md",
  ref: "a.md",
  origin_kind: "note_edit",
  base_revision: HASH,
  text_bytes: 5,
  handed_at: null,
  created_at: "2026-10-02T03:04:05.678Z",
};

test("collectDecisions yields native kinds only: vault items arrive from the route", () => {
  const sessions = { a: view("a", { cards: [toolCard("req-1"), questionCard("req-2")] }) };
  const items = collectDecisions(sessions, "ws-1");
  expect(items.map((item) => item.kind)).toEqual(["permission", "question"]);
});

test("the union holds both shapes and narrows on kind", () => {
  const items: DecisionItem[] = [
    ...collectDecisions({ a: view("a", { cards: [toolCard("req-1")] }) }, null),
    vaultProposal,
  ];
  const labels = items.map((item) => (item.kind === "vault_proposal" ? item.ref : item.card.kind));
  expect(labels).toEqual(["permission", "a.md"]);
});
