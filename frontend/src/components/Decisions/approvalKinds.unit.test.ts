/**
 * approvalActions: one test per 4.3 table row, plus the two disabled hints and the ordering rule.
 */
import { expect, test } from "vitest";
import type { NativeDecision } from "../../state/decisions";
import type {
  ChangeProposalItem,
  ChangeProposalState,
  QuestionProposalItem,
  QuestionProposalState,
  VaultProposalItem,
} from "../../state/decisionsApi";
import type { SkillProposalItem, SkillState } from "../../state/skillProposalsApi";
import { approvalActions, NOTE_MAX } from "./approvalKinds";

const HASH = "a".repeat(64);
const NONE = { activeWorkspaceId: null };

function nativeDecision(over: Partial<NativeDecision> = {}): NativeDecision {
  return {
    kind: "permission",
    id: "s1:tu1",
    origin: { workspace_id: "ws-1", session_id: "s1", turn_id: "t1" },
    revision: "abcdef1234567890",
    provider: "Claude Code",
    actor: "VIKI",
    action: "Bash",
    resource: "git status",
    inline: true,
    card: {
      kind: "permission",
      data: {
        tool_use_id: "tu1",
        kind: "tool",
        tool: "Bash",
        summary: "git status",
        question: "",
        reason: "",
        agent: "",
        plan: "",
      },
      nativeRequest: null,
    },
    ...over,
  };
}

function vaultItem(over: Partial<VaultProposalItem> = {}): VaultProposalItem {
  return {
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
    ...over,
  };
}

function questionItem(over: Partial<QuestionProposalItem> = {}): QuestionProposalItem {
  return {
    kind: "question_proposal",
    id: "88888888-8888-4888-8888-888888888888",
    origin: { workspace_id: "ws-1", session_id: null },
    revision: HASH,
    state: "open",
    attention: true,
    title: "Question change: Worth a note?",
    name: "worth_note",
    created_at: "2026-10-02T03:04:05.678Z",
    updated_at: "2026-10-02T03:04:05.678Z",
    approved_at: null,
    rejected_at: null,
    reason: null,
    ...over,
  };
}

function changeItem(over: Partial<ChangeProposalItem> = {}): ChangeProposalItem {
  return {
    kind: "change_proposal",
    id: "77777777-7777-4777-8777-777777777777",
    origin: { workspace_id: "ws-1", session_id: null },
    revision: HASH,
    state: "open",
    attention: true,
    title: "Link proposal: a.md to b.md",
    change: "note_link",
    ref: "a.md",
    created_at: "2026-10-02T03:04:05.678Z",
    updated_at: "2026-10-02T03:04:05.678Z",
    approved_at: null,
    rejected_at: null,
    reason: null,
    ...over,
  };
}

function skillItem(over: Partial<SkillProposalItem> = {}): SkillProposalItem {
  return {
    kind: "skill_proposal",
    id: "99999999-9999-4999-8999-999999999999",
    origin: { workspace_id: "ws-1", session_id: null },
    revision: HASH,
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
    ...over,
  };
}

test("NOTE_MAX is 200", () => {
  expect(NOTE_MAX).toBe(200);
});

// Row 1.
test("a native, inline tool permission offers Allow once and Deny", () => {
  const actions = approvalActions(nativeDecision({ inline: true }), NONE);
  expect(actions).toEqual([
    {
      action: "approve",
      label: "Allow once",
      hint: "Allows this one request",
      note: null,
      enabled: true,
    },
    { action: "reject", label: "Deny", hint: "Denies this request", note: null, enabled: true },
  ]);
});

// Row 2.
test("a plan permission or a question offers nothing", () => {
  const plan = nativeDecision({
    kind: "permission",
    inline: false,
    card: {
      kind: "permission",
      data: {
        tool_use_id: "tu2",
        kind: "plan",
        tool: "",
        summary: "",
        question: "",
        reason: "",
        agent: "",
        plan: "1. do a thing",
      },
      nativeRequest: null,
    },
  });
  const question = nativeDecision({
    kind: "question",
    inline: false,
    card: {
      kind: "question",
      data: { tool_use_id: "tu3", questions: [] },
      nativeRequest: null,
    },
  });
  expect(approvalActions(plan, NONE)).toEqual([]);
  expect(approvalActions(question, NONE)).toEqual([]);
});

// Row 5.
test("an active, open vault proposal offers approve, edit and reject, all enabled, in order", () => {
  const active = { activeWorkspaceId: "ws-1" };
  const actions = approvalActions(vaultItem({ state: "open" }), active);
  expect(actions).toEqual([
    {
      action: "approve",
      label: "Approve",
      hint: "Asks VIKI, in a new draft, to apply this change.",
      note: null,
      enabled: true,
    },
    {
      action: "edit",
      label: "Edit",
      hint: "Opens the proposal to edit the note.",
      note: null,
      enabled: true,
    },
    {
      action: "reject",
      label: "Reject",
      hint: "Withdraws the proposal and drafts a note for VIKI.",
      note: "reason",
      enabled: true,
    },
  ]);
});

test("a handed vault proposal also enables approve", () => {
  const active = { activeWorkspaceId: "ws-1" };
  expect(approvalActions(vaultItem({ state: "handed" }), active)[0]?.enabled).toBe(true);
});

test("active but outside the hand-off states disables approve with the wrong-state hint; edit and reject stay enabled", () => {
  const active = { activeWorkspaceId: "ws-1" };
  const actions = approvalActions(vaultItem({ state: "applied" }), active);
  expect(actions[0]).toEqual({
    action: "approve",
    label: "Approve",
    hint: "Not available in this state.",
    note: null,
    enabled: false,
  });
  expect(actions[1]?.enabled).toBe(true);
  expect(actions[2]?.enabled).toBe(true);
});

test("a proposal from another workspace disables all three with the other-vault hint", () => {
  const active = { activeWorkspaceId: "ws-1" };
  const actions = approvalActions(
    vaultItem({ state: "open", origin: { workspace_id: "ws-2", session_id: null } }),
    active,
  );
  expect(actions).toEqual([
    {
      action: "approve",
      label: "Approve",
      hint: "This proposal is for another vault.",
      note: null,
      enabled: false,
    },
    {
      action: "edit",
      label: "Edit",
      hint: "This proposal is for another vault.",
      note: null,
      enabled: false,
    },
    {
      action: "reject",
      label: "Reject",
      hint: "This proposal is for another vault.",
      note: "reason",
      enabled: false,
    },
  ]);
});

test("no active workspace disables a vault proposal's actions with the other-vault hint", () => {
  const actions = approvalActions(vaultItem({ state: "open" }), NONE);
  expect(
    actions.every((spec) => !spec.enabled && spec.hint === "This proposal is for another vault."),
  ).toBe(true);
});

// Row 6.
test("an active, open skill proposal offers approve, edit and reject, all enabled, no later, in order", () => {
  const active = { activeWorkspaceId: "ws-1" };
  const actions = approvalActions(skillItem({ state: "open" }), active);
  expect(actions).toEqual([
    {
      action: "approve",
      label: "Approve",
      hint: "Opens a draft asking VIKI to apply this skill",
      note: null,
      enabled: true,
    },
    {
      action: "edit",
      label: "Edit",
      hint: "Opens the proposal to edit the skill.",
      note: null,
      enabled: true,
    },
    {
      action: "reject",
      label: "Reject",
      hint: "Records a rejection; an optional reason stays with the proposal.",
      note: "reason",
      enabled: true,
    },
  ]);
});

test("a handed skill proposal also enables approve", () => {
  const active = { activeWorkspaceId: "ws-1" };
  expect(approvalActions(skillItem({ state: "handed" }), active)[0]?.enabled).toBe(true);
});

test("an applied skill proposal disables all three with the wrong-state hint", () => {
  const active = { activeWorkspaceId: "ws-1" };
  const actions = approvalActions(skillItem({ state: "applied" }), active);
  expect(actions).toHaveLength(3);
  expect(
    actions.every((spec) => !spec.enabled && spec.hint === "Not available in this state."),
  ).toBe(true);
});

test("a rejected skill proposal disables approve and reject but keeps edit enabled", () => {
  const active = { activeWorkspaceId: "ws-1" };
  const actions = approvalActions(skillItem({ state: "rejected" }), active);
  expect(actions.map((spec) => spec.enabled)).toEqual([false, true, false]);
});

test("a stale skill proposal disables only approve", () => {
  const active = { activeWorkspaceId: "ws-1" };
  const actions = approvalActions(skillItem({ state: "stale" }), active);
  expect(actions.map((spec) => spec.enabled)).toEqual([false, true, true]);
});

test("a skill proposal from another workspace disables all three with the other-vault hint", () => {
  const active = { activeWorkspaceId: "ws-1" };
  const actions = approvalActions(
    skillItem({ state: "open", origin: { workspace_id: "ws-2", session_id: null } }),
    active,
  );
  expect(actions).toHaveLength(3);
  expect(
    actions.every((spec) => !spec.enabled && spec.hint === "This proposal is for another vault."),
  ).toBe(true);
});

test("no active workspace disables a skill proposal's actions with the other-vault hint", () => {
  const actions = approvalActions(skillItem({ state: "open" }), NONE);
  expect(actions).toHaveLength(3);
  expect(
    actions.every((spec) => !spec.enabled && spec.hint === "This proposal is for another vault."),
  ).toBe(true);
});

test("a skill proposal never offers later, in any of its states", () => {
  const active = { activeWorkspaceId: "ws-1" };
  const states: readonly SkillState[] = [
    "open",
    "handed",
    "applied",
    "rejected",
    "stale",
    "source_missing",
    "unknown",
  ];
  for (const state of states) {
    expect(approvalActions(skillItem({ state }), active).map((spec) => spec.action)).toEqual([
      "approve",
      "edit",
      "reject",
    ]);
  }
});

// Row 7.
test("an active, open question proposal offers approve and reject, both enabled, in order", () => {
  const active = { activeWorkspaceId: "ws-1" };
  const actions = approvalActions(questionItem({ state: "open" }), active);
  expect(actions).toEqual([
    {
      action: "approve",
      label: "Approve",
      hint: "Starts using this wording for new decisions.",
      note: null,
      enabled: true,
    },
    {
      action: "reject",
      label: "Reject",
      hint: "Rejects this wording; nothing changes.",
      note: "reason",
      enabled: true,
    },
  ]);
});

test("an approved (in-use) question proposal disables approve and relabels reject as Undo", () => {
  const active = { activeWorkspaceId: "ws-1" };
  const actions = approvalActions(questionItem({ state: "approved" }), active);
  expect(actions).toEqual([
    {
      action: "approve",
      label: "Approve",
      hint: "Not available in this state.",
      note: null,
      enabled: false,
    },
    {
      action: "reject",
      label: "Undo",
      hint: "Goes back to the wording before this change.",
      note: "reason",
      enabled: true,
    },
  ]);
});

test.each(["replaced", "rejected"] as const)(
  "a %s question proposal disables both approve and reject with the wrong-state hint",
  (state) => {
    const active = { activeWorkspaceId: "ws-1" };
    const actions = approvalActions(questionItem({ state }), active);
    expect(actions).toHaveLength(2);
    expect(
      actions.every((spec) => !spec.enabled && spec.hint === "Not available in this state."),
    ).toBe(true);
  },
);

test("a question proposal from another workspace disables both actions with the other-vault hint", () => {
  const active = { activeWorkspaceId: "ws-1" };
  const actions = approvalActions(
    questionItem({ state: "open", origin: { workspace_id: "ws-2", session_id: null } }),
    active,
  );
  expect(actions).toHaveLength(2);
  expect(
    actions.every((spec) => !spec.enabled && spec.hint === "This proposal is for another vault."),
  ).toBe(true);
});

test("no active workspace disables a question proposal's actions with the other-vault hint", () => {
  const actions = approvalActions(questionItem({ state: "open" }), NONE);
  expect(actions).toHaveLength(2);
  expect(
    actions.every((spec) => !spec.enabled && spec.hint === "This proposal is for another vault."),
  ).toBe(true);
});

test("a question proposal offers approve and reject only", () => {
  const active = { activeWorkspaceId: "ws-1" };
  const states: readonly QuestionProposalState[] = ["open", "approved", "replaced", "rejected"];
  for (const state of states) {
    expect(approvalActions(questionItem({ state }), active).map((spec) => spec.action)).toEqual([
      "approve",
      "reject",
    ]);
  }
});

// Row 8.
test("an active, open change proposal offers approve and reject, both enabled, in order", () => {
  const active = { activeWorkspaceId: "ws-1" };
  const actions = approvalActions(changeItem({ state: "open" }), active);
  expect(actions).toEqual([
    {
      action: "approve",
      label: "Approve",
      hint: "Opens a draft asking VIKI to make this change.",
      note: null,
      enabled: true,
    },
    {
      action: "reject",
      label: "Reject",
      hint: "Rejects this change; nothing in the vault changes.",
      note: "reason",
      enabled: true,
    },
  ]);
});

test("an approved change proposal relabels approve as Draft again, still enabled, and disables reject with the wrong-state hint", () => {
  const active = { activeWorkspaceId: "ws-1" };
  const actions = approvalActions(changeItem({ state: "approved" }), active);
  expect(actions).toEqual([
    {
      action: "approve",
      label: "Draft again",
      hint: "Opens a draft asking VIKI to make this change.",
      note: null,
      enabled: true,
    },
    {
      action: "reject",
      label: "Reject",
      hint: "Not available in this state.",
      note: "reason",
      enabled: false,
    },
  ]);
});

test("a rejected change proposal disables both actions with the wrong-state hint", () => {
  const active = { activeWorkspaceId: "ws-1" };
  const actions = approvalActions(changeItem({ state: "rejected" }), active);
  expect(actions).toHaveLength(2);
  expect(
    actions.every((spec) => !spec.enabled && spec.hint === "Not available in this state."),
  ).toBe(true);
  expect(actions[0]?.label).toBe("Approve");
});

test("a change proposal from another workspace disables both actions with the other-vault hint", () => {
  const active = { activeWorkspaceId: "ws-1" };
  const actions = approvalActions(
    changeItem({ state: "open", origin: { workspace_id: "ws-2", session_id: null } }),
    active,
  );
  expect(actions).toHaveLength(2);
  expect(
    actions.every((spec) => !spec.enabled && spec.hint === "This proposal is for another vault."),
  ).toBe(true);
});

test("no active workspace disables a change proposal's actions with the other-vault hint", () => {
  const actions = approvalActions(changeItem({ state: "open" }), NONE);
  expect(actions).toHaveLength(2);
  expect(
    actions.every((spec) => !spec.enabled && spec.hint === "This proposal is for another vault."),
  ).toBe(true);
});

test("a change proposal offers approve and reject only, in every state", () => {
  const active = { activeWorkspaceId: "ws-1" };
  const states: readonly ChangeProposalState[] = ["open", "approved", "rejected"];
  for (const state of states) {
    expect(approvalActions(changeItem({ state }), active).map((spec) => spec.action)).toEqual([
      "approve",
      "reject",
    ]);
  }
});
