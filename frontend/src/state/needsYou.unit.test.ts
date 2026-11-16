/**
 * needsYou.ts: the merge rules in the module's own header comment — attribution, proposal
 * filtering, order and keys.
 */
import { expect, test } from "vitest";
import type { ChangeProposalItem, QuestionProposalItem, VaultProposalItem } from "./decisionsApi";
import { type NeedRow, needsYou } from "./needsYou";
import type { SkillProposalItem } from "./skillProposalsApi";
import type { Card, RoutingBlock, SessionView, Turn } from "./view";
import { emptyView } from "./view";

const RUNTIME = "33333333-3333-4333-8333-333333333333";
const TURN_ID = "55555555-5555-4555-8555-555555555555";
const RETRO_HASH = "a".repeat(64);

function permissionCard(requestId: string, agent: string): Card {
  return {
    kind: "permission",
    data: {
      tool_use_id: `tu-${requestId}`,
      kind: "tool",
      tool: "Write",
      summary: "30-knowledge/x.md",
      question: "",
      reason: "",
      agent,
      plan: "",
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

function turn(id: string, blocks: RoutingBlock[]): Turn {
  return { id, user: null, blocks, status: "running", usage: null, durationMs: 0, history: false };
}

function view(sessionId: string, overrides: Partial<SessionView> = {}): SessionView {
  return { ...emptyView(sessionId), generation: "gen-1", ...overrides };
}

function vaultProposal(overrides: Partial<VaultProposalItem> = {}): VaultProposalItem {
  return {
    kind: "vault_proposal",
    id: "p-1",
    origin: { workspace_id: "ws-1", session_id: null },
    revision: RETRO_HASH,
    state: "open",
    attention: true,
    title: "a.md",
    ref: "a.md",
    origin_kind: "note_edit",
    base_revision: RETRO_HASH,
    text_bytes: 5,
    handed_at: null,
    created_at: "2026-10-02T03:04:05.678Z",
    ...overrides,
  };
}

function skillProposal(overrides: Partial<SkillProposalItem> = {}): SkillProposalItem {
  return {
    kind: "skill_proposal",
    id: "sk-1",
    origin: { workspace_id: "ws-1", session_id: null },
    revision: RETRO_HASH,
    state: "open",
    attention: true,
    title: "my-skill",
    name: "my-skill",
    relative_id: "skills/my-skill/SKILL.md",
    trigger: "button",
    sessions: ["11111111-1111-4111-8111-111111111111"],
    base_revision: null,
    text_bytes: 42,
    approved_at: null,
    rejected_at: null,
    reason: null,
    created_at: "2026-10-02T03:04:05.678Z",
    updated_at: "2026-10-02T03:04:05.678Z",
    ...overrides,
  };
}

function questionProposal(overrides: Partial<QuestionProposalItem> = {}): QuestionProposalItem {
  return {
    kind: "question_proposal",
    id: "qp-1",
    origin: { workspace_id: "ws-1", session_id: null },
    revision: RETRO_HASH,
    state: "open",
    attention: true,
    title: "Question change: Worth a note?",
    name: "worth_note",
    created_at: "2026-10-02T03:04:05.678Z",
    updated_at: "2026-10-02T03:04:05.678Z",
    approved_at: null,
    rejected_at: null,
    reason: null,
    ...overrides,
  };
}

function changeProposal(overrides: Partial<ChangeProposalItem> = {}): ChangeProposalItem {
  return {
    kind: "change_proposal",
    id: "ch-1",
    origin: { workspace_id: "ws-1", session_id: null },
    revision: RETRO_HASH,
    state: "open",
    attention: true,
    title: "Link a.md to b.md",
    change: "note_link",
    ref: "a.md",
    created_at: "2026-10-02T03:04:05.678Z",
    updated_at: "2026-10-02T03:04:05.678Z",
    approved_at: null,
    rejected_at: null,
    reason: null,
    ...overrides,
  };
}

function baseInput(
  overrides: Partial<Parameters<typeof needsYou>[0]> = {},
): Parameters<typeof needsYou>[0] {
  return {
    sessions: {},
    workspaceId: null,
    proposals: [],
    ...overrides,
  };
}

function natives(rows: readonly NeedRow[]): Extract<NeedRow, { kind: "native" }>[] {
  return rows.filter((row): row is Extract<NeedRow, { kind: "native" }> => row.kind === "native");
}

// --- attribution -------------------------------------------------------------------------------

test("a permission card naming one agent joins that session's one matching running row", () => {
  const sessions = {
    s1: view("s1", {
      turns: [turn("t0", [routing("r1", { agent: "researcher" })])],
      cards: [permissionCard("req-1", "researcher")],
    }),
  };
  const result = needsYou(baseInput({ sessions }));
  expect(result.rows[0]).toMatchObject({ kind: "native", agent: "researcher", blockId: "r1" });
});

test("two running rows naming the same agent leave the card unattributed: agent falls back, blockId stays null", () => {
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
  const result = needsYou(baseInput({ sessions }));
  expect(result.rows[0]).toMatchObject({ kind: "native", agent: "researcher", blockId: null });
});

test("requests is passed on to groupRuns, so two same-named running rows are told apart by run id", () => {
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
  const requests = new Map([["s1", new Map([["tu-req-1", { run: "r2", raisedAt: 1000 }]])]]);
  const result = needsYou(baseInput({ sessions, requests }));
  expect(result.rows[0]).toMatchObject({ kind: "native", agent: "researcher", blockId: "r2" });
});

test("a permission card naming no agent gets a null agent and block", () => {
  const sessions = { s1: view("s1", { cards: [permissionCard("req-1", "")] }) };
  const result = needsYou(baseInput({ sessions }));
  expect(result.rows[0]).toMatchObject({ kind: "native", agent: null, blockId: null });
});

test("a question card gets a null agent and block", () => {
  const sessions = { s1: view("s1", { cards: [questionCard("req-1")] }) };
  const result = needsYou(baseInput({ sessions }));
  expect(result.rows[0]).toMatchObject({ kind: "native", agent: null, blockId: null });
});

test("the running-row match is scoped to the card's own session, even with a colliding tool_use_id", () => {
  const sessions = {
    s1: view("s1", { cards: [permissionCard("req-1", "researcher")] }),
    s2: view("s2", {
      turns: [turn("t0", [routing("r1", { agent: "researcher" })])],
      cards: [permissionCard("req-1", "researcher")],
    }),
  };
  const result = needsYou(baseInput({ sessions }));
  const [s1Item, s2Item] = natives(result.rows);
  expect(s1Item?.item.origin.session_id).toBe("s1");
  expect(s1Item).toMatchObject({ agent: "researcher", blockId: null });
  expect(s2Item?.item.origin.session_id).toBe("s2");
  expect(s2Item).toMatchObject({ agent: "researcher", blockId: "r1" });
});

// --- proposals: attention filter ----------------------------------------------------------------

test("proposals with attention become rows; without attention they do not", () => {
  const shown = vaultProposal({ id: "p-shown", attention: true });
  const hidden = vaultProposal({ id: "p-hidden", attention: false });
  const result = needsYou(
    baseInput({ workspaceId: shown.origin.workspace_id, proposals: [shown, hidden] }),
  );
  expect(result.rows.map((row) => row.kind)).toEqual(["proposal"]);
  expect(result.rows.map((row) => (row.kind === "proposal" ? row.item.id : null))).toEqual([
    shown.id,
  ]);
});

test("a vault proposal of another vault neither counts nor shows in Needs you; the active vault's still does", () => {
  const active = vaultProposal({ id: "p-active" }); // default origin: ws-1
  const other = vaultProposal({
    id: "p-other",
    origin: { workspace_id: "ws-2", session_id: null },
  });
  const result = needsYou(baseInput({ workspaceId: "ws-1", proposals: [active, other] }));
  expect(result.rows.map((row) => (row.kind === "proposal" ? row.item.id : null))).toEqual([
    active.id,
  ]);
  expect(result.count).toBe(1);
});

test("a skill item with attention becomes a proposal row, its path carried on the item", () => {
  const item = skillProposal();
  const result = needsYou(baseInput({ workspaceId: item.origin.workspace_id, proposals: [item] }));
  expect(result.rows).toEqual([
    { kind: "proposal", key: `proposal:skill_proposal:${item.id}`, item },
  ]);
  expect(result.count).toBe(1);
});

test("a skill proposal of another vault neither counts nor shows in Needs you; the active vault's still does", () => {
  const active = skillProposal({ id: "sk-active" }); // default origin: ws-1
  const other = skillProposal({
    id: "sk-other",
    origin: { workspace_id: "ws-2", session_id: null },
  });
  const result = needsYou(baseInput({ workspaceId: "ws-1", proposals: [active, other] }));
  expect(result.rows.map((row) => (row.kind === "proposal" ? row.item.id : null))).toEqual([
    active.id,
  ]);
  expect(result.count).toBe(1);
});

test("a question proposal item with attention becomes a proposal row", () => {
  const item = questionProposal();
  const result = needsYou(baseInput({ workspaceId: item.origin.workspace_id, proposals: [item] }));
  expect(result.rows).toEqual([
    { kind: "proposal", key: `proposal:question_proposal:${item.id}`, item },
  ]);
  expect(result.count).toBe(1);
});

test("a question proposal of another vault neither counts nor shows in Needs you; the active vault's still does", () => {
  const active = questionProposal({ id: "qp-active" }); // default origin: ws-1
  const other = questionProposal({
    id: "qp-other",
    origin: { workspace_id: "ws-2", session_id: null },
  });
  const result = needsYou(baseInput({ workspaceId: "ws-1", proposals: [active, other] }));
  expect(result.rows.map((row) => (row.kind === "proposal" ? row.item.id : null))).toEqual([
    active.id,
  ]);
  expect(result.count).toBe(1);
});

test("a change proposal item with attention becomes a proposal row", () => {
  const item = changeProposal();
  const result = needsYou(baseInput({ workspaceId: item.origin.workspace_id, proposals: [item] }));
  expect(result.rows).toEqual([
    { kind: "proposal", key: `proposal:change_proposal:${item.id}`, item },
  ]);
  expect(result.count).toBe(1);
});

test("a change proposal of another vault neither counts nor shows in Needs you; the active vault's still does", () => {
  const active = changeProposal({ id: "ch-active" }); // default origin: ws-1
  const other = changeProposal({
    id: "ch-other",
    origin: { workspace_id: "ws-2", session_id: null },
  });
  const result = needsYou(baseInput({ workspaceId: "ws-1", proposals: [active, other] }));
  expect(result.rows.map((row) => (row.kind === "proposal" ? row.item.id : null))).toEqual([
    active.id,
  ]);
  expect(result.count).toBe(1);
});

test("an open change item counts in Needs you the same as other open proposals; a closed one does not", () => {
  const shown = changeProposal({ id: "ch-shown", state: "open", attention: true });
  const hidden = changeProposal({ id: "ch-hidden", state: "approved", attention: false });
  const result = needsYou(
    baseInput({ workspaceId: shown.origin.workspace_id, proposals: [shown, hidden] }),
  );
  expect(result.rows.map((row) => (row.kind === "proposal" ? row.item.id : null))).toEqual([
    shown.id,
  ]);
  expect(result.count).toBe(1);
});

// --- order, keys and count ----------------------------------------------------------------------

test("rows are ordered natives, then proposals, with the spec's key formats", () => {
  const sessions = { s1: view("s1", { cards: [permissionCard("req-1", "")] }) };
  const proposal = vaultProposal({ attention: true });
  const result = needsYou(
    baseInput({
      sessions,
      workspaceId: proposal.origin.workspace_id,
      proposals: [proposal],
    }),
  );
  expect(result.rows.map((row) => row.kind)).toEqual(["native", "proposal"]);
  expect(result.rows.map((row) => row.key)).toEqual([
    "native:s1:tu-req-1",
    `proposal:vault_proposal:${proposal.id}`,
  ]);
  expect(result.count).toBe(2);
});
