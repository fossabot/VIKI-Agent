/**
 * createApprovalExecutor's branches — vault approve/reject, skill approve/reject,
 * question-proposal approve/reject (relabelled Undo once in use), change-proposal approve/reject
 * (relabelled Draft again once approved) — the other-vault and
 * not-supported guards — plus call order and the changed contract.
 */
import { expect, test, vi } from "vitest";
import type { ChangeActResult, DecisionLoopApi } from "../../clients/decisionLoop/types";
import type {
  ChangeProposalItem,
  DecisionsApi,
  ProposalDetail,
  QuestionProposalItem,
  VaultProposalItem,
} from "../../state/decisionsApi";
import {
  type SkillProposalDetail,
  type SkillProposalItem,
  type SkillProposalsApi,
  SkillProposalsApiError,
} from "../../state/skillProposalsApi";
import { ApprovalActionError, createApprovalExecutor } from "./approvalExecutor";
import { proposalHandoffText, proposalRejectText } from "./handoff";
import { applyHandoffText } from "./skillHandoff";

const HASH_A = "a".repeat(64);
const HASH_B = "b".repeat(64);

function notUsed(): never {
  throw new Error("not used in this test");
}

function vaultRow(over: Partial<VaultProposalItem> = {}): VaultProposalItem {
  return {
    kind: "vault_proposal",
    id: "p-1",
    origin: { workspace_id: "ws-1", session_id: null },
    revision: HASH_A,
    state: "open",
    attention: true,
    title: "a.md",
    ref: "a.md",
    origin_kind: "note_edit",
    base_revision: HASH_A,
    text_bytes: 5,
    handed_at: null,
    created_at: "2026-10-02T03:04:05.678Z",
    ...over,
  };
}

function detailFor(row: VaultProposalItem, over: Partial<ProposalDetail> = {}): ProposalDetail {
  return {
    ...row,
    current_revision: row.base_revision,
    proposed_text: "new text",
    diff: "--- a/a.md\n+++ b/a.md\n-old\n+new",
    diff_reason: null,
    ...over,
  };
}

function skillRow(over: Partial<SkillProposalItem> = {}): SkillProposalItem {
  return {
    kind: "skill_proposal",
    id: "sk-1",
    origin: { workspace_id: "ws-1", session_id: null },
    revision: HASH_A,
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

function questionRow(over: Partial<QuestionProposalItem> = {}): QuestionProposalItem {
  return {
    kind: "question_proposal",
    // isQuestionProposalItem (the executor's own guard) requires a canonical UUID, unlike the
    // vault/skill rows above.
    id: "22222222-2222-4222-8222-222222222222",
    origin: { workspace_id: "ws-1", session_id: null },
    revision: HASH_A,
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

function changeRow(over: Partial<ChangeProposalItem> = {}): ChangeProposalItem {
  return {
    kind: "change_proposal",
    id: "33333333-3333-4333-8333-333333333333",
    origin: { workspace_id: "ws-1", session_id: null },
    revision: HASH_A,
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

function skillDetailFor(
  row: SkillProposalItem,
  over: Partial<SkillProposalDetail> = {},
): SkillProposalDetail {
  return {
    ...row,
    text: "SKILL.md body",
    current_text: null,
    diff: "",
    diff_reason: "no_source",
    description: "does a thing",
    unsupported_fields: [],
    shadowed: false,
    policy: "ordinary",
    ...over,
  };
}

/** A fake `DecisionsApi` plus the three injected callbacks, all pushing a tag to one shared
 * `order` array so a test can assert both which calls happened and in what sequence. */
function harness(
  config: {
    activeWorkspaceId?: string | null;
    detail?: ProposalDetail;
    openDraftResult?: boolean;
    proposalActError?: Error;
    skillDetail?: SkillProposalDetail;
    skillActError?: unknown;
    noSkills?: boolean;
    questionActError?: unknown;
    noQuestions?: boolean;
    changeActError?: unknown;
    changeDraft?: string;
  } = {},
) {
  const order: string[] = [];
  const api: DecisionsApi = {
    decisions: notUsed,
    async proposalDetail() {
      order.push("proposalDetail");
      return config.detail ?? detailFor(vaultRow());
    },
    async proposalAct(_id, action) {
      order.push(`proposalAct:${action}`);
      if (config.proposalActError) throw config.proposalActError;
      return null;
    },
    createProposal: notUsed,
  };
  const skillDetail = vi.fn(async () => {
    order.push("skillDetail");
    return config.skillDetail ?? skillDetailFor(skillRow());
  });
  const skillAct = vi.fn(async () => {
    order.push("skillAct");
    if (config.skillActError) throw config.skillActError;
    return null;
  });
  const skills: SkillProposalsApi = {
    create: notUsed,
    detail: skillDetail,
    act: skillAct,
  } as unknown as SkillProposalsApi;
  const actProposal = vi.fn(
    async (
      _id: string,
      _action: "approve" | "reject",
      _revision: string,
      _reason?: string,
    ): Promise<QuestionProposalItem> => {
      order.push("actProposal");
      if (config.questionActError) throw config.questionActError;
      return questionRow();
    },
  );
  const actChange = vi.fn(
    async (
      _id: string,
      action2: "approve" | "reject",
      _revision: string,
      _reason?: string,
    ): Promise<ChangeActResult> => {
      order.push("actChange");
      if (config.changeActError) throw config.changeActError;
      return {
        change: changeRow(),
        draft: action2 === "reject" ? null : (config.changeDraft ?? "draft text"),
      };
    },
  );
  const questions: Pick<DecisionLoopApi, "actProposal" | "actChange"> = {
    actProposal,
    actChange,
  };
  const openDraft = vi.fn((_text: string): boolean => {
    order.push("openDraft");
    return config.openDraftResult ?? true;
  });
  const changed = vi.fn((): void => {
    order.push("changed");
  });
  const run = createApprovalExecutor({
    api,
    ...(config.noSkills ? {} : { skills }),
    ...(config.noQuestions ? {} : { questions }),
    activeWorkspaceId: config.activeWorkspaceId ?? "ws-1",
    openDraft,
    changed,
  });
  return {
    order,
    openDraft,
    changed,
    run,
    skillDetail,
    skillAct,
    actProposal,
    actChange,
  };
}

// ---- Vault: approve ----

test("vault approve reads the detail, records the hand-off, then opens the draft", async () => {
  const row = vaultRow();
  const detail = detailFor(row);
  const h = harness({ detail });
  await h.run(row, "approve", "");
  expect(h.order).toEqual(["proposalDetail", "proposalAct:handoff", "openDraft", "changed"]);
  expect(h.openDraft).toHaveBeenCalledWith(proposalHandoffText(detail));
});

test("vault approve throws changed when the fetched detail's revision differs, without acting", async () => {
  const row = vaultRow({ revision: HASH_A });
  const detail = detailFor(row, { revision: HASH_B });
  const h = harness({ detail });
  await expect(h.run(row, "approve", "")).rejects.toMatchObject({
    code: "changed",
  } satisfies Partial<ApprovalActionError>);
  expect(h.order).toEqual(["proposalDetail", "changed"]);
});

test("vault approve throws editor_full after the hand-off is recorded when the draft has no room", async () => {
  const row = vaultRow();
  const detail = detailFor(row);
  const h = harness({ detail, openDraftResult: false });
  await expect(h.run(row, "approve", "")).rejects.toMatchObject({
    code: "editor_full",
    ownerText:
      "The hand-off was recorded, but this window has no room for another draft. Close a draft, then use Create a draft in the proposal.",
  } satisfies Partial<ApprovalActionError>);
  expect(h.order).toEqual(["proposalDetail", "proposalAct:handoff", "openDraft", "changed"]);
});

test("a failed vault hand-off call propagates without opening the draft", async () => {
  const row = vaultRow();
  const detail = detailFor(row);
  const boom = new Error("network down");
  const h = harness({ detail, proposalActError: boom });
  await expect(h.run(row, "approve", "")).rejects.toBe(boom);
  expect(h.order).toEqual(["proposalDetail", "proposalAct:handoff", "changed"]);
  expect(h.openDraft).not.toHaveBeenCalled();
});

// ---- Vault: reject ----

test("vault reject withdraws, then opens the reject draft", async () => {
  const row = vaultRow();
  const h = harness();
  await h.run(row, "reject", "stale info");
  expect(h.order).toEqual(["proposalAct:withdraw", "openDraft", "changed"]);
  expect(h.openDraft).toHaveBeenCalledWith(proposalRejectText(row, "stale info"));
});

test("vault reject throws editor_full after the withdrawal when the draft has no room", async () => {
  const row = vaultRow();
  const h = harness({ openDraftResult: false });
  await expect(h.run(row, "reject", "")).rejects.toMatchObject({
    code: "editor_full",
    ownerText: "The proposal was withdrawn, but this window has no room for the draft to VIKI.",
  } satisfies Partial<ApprovalActionError>);
  expect(h.order).toEqual(["proposalAct:withdraw", "openDraft", "changed"]);
});

// ---- Vault: guards ----

test("vault actions throw other_vault before any call when the proposal is for another vault", async () => {
  const row = vaultRow({ origin: { workspace_id: "ws-2", session_id: null } });
  for (const action of ["approve", "edit", "reject", "later"] as const) {
    const h = harness({ activeWorkspaceId: "ws-1" });
    await expect(h.run(row, action, "")).rejects.toMatchObject({
      code: "other_vault",
    } satisfies Partial<ApprovalActionError>);
    expect(h.order).toEqual(["changed"]);
  }
});

test("vault edit and later throw not_supported without any call", async () => {
  const row = vaultRow();
  for (const action of ["edit", "later"] as const) {
    const h = harness();
    await expect(h.run(row, action, "")).rejects.toMatchObject({
      code: "not_supported",
    } satisfies Partial<ApprovalActionError>);
    expect(h.order).toEqual(["changed"]);
  }
});

// ---- Skill: approve ----

test("skill approve reads the detail, then acts with the detail's revision, then opens the draft", async () => {
  const row = skillRow();
  const detail = skillDetailFor(row);
  const h = harness({ skillDetail: detail });
  await h.run(row, "approve", "");
  expect(h.order).toEqual(["skillDetail", "skillAct", "openDraft", "changed"]);
  expect(h.skillAct).toHaveBeenCalledWith(row.id, "approve", detail.revision);
  expect(h.openDraft).toHaveBeenCalledWith(applyHandoffText(detail));
});

test("skill approve throws changed when the fetched detail's revision differs, without acting", async () => {
  const row = skillRow({ revision: HASH_A });
  const detail = skillDetailFor(row, { revision: HASH_B });
  const h = harness({ skillDetail: detail });
  await expect(h.run(row, "approve", "")).rejects.toMatchObject({
    code: "changed",
  } satisfies Partial<ApprovalActionError>);
  expect(h.order).toEqual(["skillDetail", "changed"]);
  expect(h.skillAct).not.toHaveBeenCalled();
});

test("skill approve throws editor_full after the act is recorded when the draft has no room", async () => {
  const row = skillRow();
  const detail = skillDetailFor(row);
  const h = harness({ skillDetail: detail, openDraftResult: false });
  await expect(h.run(row, "approve", "")).rejects.toMatchObject({
    code: "editor_full",
    ownerText:
      "The approval was recorded, but this window has no room for another draft. Close a draft, then use Approve in the proposal to make the draft again.",
  } satisfies Partial<ApprovalActionError>);
  expect(h.order).toEqual(["skillDetail", "skillAct", "openDraft", "changed"]);
});

test("a refused skill approve surfaces skillRefusal's line instead of the raw server error", async () => {
  const row = skillRow();
  const detail = skillDetailFor(row);
  const h = harness({
    skillDetail: detail,
    skillActError: new SkillProposalsApiError("stale_revision"),
  });
  await expect(h.run(row, "approve", "")).rejects.toMatchObject({
    code: "refused",
    ownerText: "This proposal changed since you opened it. Open it again.",
  } satisfies Partial<ApprovalActionError>);
  expect(h.order).toEqual(["skillDetail", "skillAct", "changed"]);
  expect(h.openDraft).not.toHaveBeenCalled();
});

// ---- Skill: reject ----

test("skill reject with a note calls act with the note and opens no draft", async () => {
  const row = skillRow();
  const h = harness();
  await h.run(row, "reject", "too vague");
  expect(h.skillAct).toHaveBeenCalledWith(row.id, "reject", row.revision, undefined, "too vague");
  expect(h.openDraft).not.toHaveBeenCalled();
  expect(h.order).toEqual(["skillAct", "changed"]);
});

test("skill reject without a note calls act with no reason, and opens no draft", async () => {
  const row = skillRow();
  const h = harness();
  await h.run(row, "reject", "");
  expect(h.skillAct).toHaveBeenCalledWith(row.id, "reject", row.revision, undefined, undefined);
  expect(h.openDraft).not.toHaveBeenCalled();
});

test("a refused skill reject surfaces skillRefusal's line instead of the raw server error", async () => {
  const row = skillRow();
  const h = harness({ skillActError: new SkillProposalsApiError("already_applied") });
  await expect(h.run(row, "reject", "")).rejects.toMatchObject({
    code: "refused",
    ownerText: "The skill file already matches this proposal.",
  } satisfies Partial<ApprovalActionError>);
});

// ---- Skill: guards ----

test("skill actions throw other_vault before any call when the proposal is for another vault", async () => {
  const row = skillRow({ origin: { workspace_id: "ws-2", session_id: null } });
  for (const action of ["approve", "reject"] as const) {
    const h = harness({ activeWorkspaceId: "ws-1" });
    await expect(h.run(row, action, "")).rejects.toMatchObject({
      code: "other_vault",
    } satisfies Partial<ApprovalActionError>);
    expect(h.order).toEqual(["changed"]);
  }
});

test("skill edit and later throw not_supported without any call", async () => {
  const row = skillRow();
  for (const action of ["edit", "later"] as const) {
    const h = harness();
    await expect(h.run(row, action, "")).rejects.toMatchObject({
      code: "not_supported",
    } satisfies Partial<ApprovalActionError>);
    expect(h.order).toEqual(["changed"]);
  }
});

test("no skills api gives not_supported for a skill row, before any call", async () => {
  const row = skillRow();
  const h = harness({ noSkills: true });
  await expect(h.run(row, "approve", "")).rejects.toMatchObject({
    code: "not_supported",
  } satisfies Partial<ApprovalActionError>);
  expect(h.order).toEqual(["changed"]);
});

// ---- Question: approve ----

test("question approve calls actProposal with the row's revision, opens no draft", async () => {
  const row = questionRow({ state: "open", revision: HASH_A });
  const h = harness();
  await h.run(row, "approve", "");
  expect(h.order).toEqual(["actProposal", "changed"]);
  expect(h.actProposal).toHaveBeenCalledWith(row.id, "approve", HASH_A, undefined);
  expect(h.openDraft).not.toHaveBeenCalled();
});

test("a failed question approve call propagates without opening the draft", async () => {
  const row = questionRow();
  const boom = new Error("network down");
  const h = harness({ questionActError: boom });
  await expect(h.run(row, "approve", "")).rejects.toBe(boom);
  expect(h.order).toEqual(["actProposal", "changed"]);
  expect(h.openDraft).not.toHaveBeenCalled();
});

// ---- Question: reject / undo ----

test("question reject with a note calls actProposal with the note, opens no draft", async () => {
  const row = questionRow({ state: "open" });
  const h = harness();
  await h.run(row, "reject", "too soon");
  expect(h.actProposal).toHaveBeenCalledWith(row.id, "reject", row.revision, "too soon");
  expect(h.openDraft).not.toHaveBeenCalled();
  expect(h.order).toEqual(["actProposal", "changed"]);
});

test("question reject (undoing an approved wording) without a note passes no reason", async () => {
  const row = questionRow({ state: "approved" });
  const h = harness();
  await h.run(row, "reject", "");
  expect(h.actProposal).toHaveBeenCalledWith(row.id, "reject", row.revision, undefined);
  expect(h.openDraft).not.toHaveBeenCalled();
});

test("a failed question reject call propagates without opening the draft", async () => {
  const row = questionRow({ state: "approved" });
  const boom = new Error("stale revision");
  const h = harness({ questionActError: boom });
  await expect(h.run(row, "reject", "")).rejects.toBe(boom);
  expect(h.order).toEqual(["actProposal", "changed"]);
  expect(h.openDraft).not.toHaveBeenCalled();
});

// ---- Question: guards ----

test("question actions throw other_vault before any call when the proposal is for another vault", async () => {
  const row = questionRow({ origin: { workspace_id: "ws-2", session_id: null } });
  for (const action of ["approve", "reject", "edit", "later"] as const) {
    const h = harness({ activeWorkspaceId: "ws-1" });
    await expect(h.run(row, action, "")).rejects.toMatchObject({
      code: "other_vault",
    } satisfies Partial<ApprovalActionError>);
    expect(h.order).toEqual(["changed"]);
  }
});

test("question edit and later throw not_supported without any call", async () => {
  const row = questionRow();
  for (const action of ["edit", "later"] as const) {
    const h = harness();
    await expect(h.run(row, action, "")).rejects.toMatchObject({
      code: "not_supported",
    } satisfies Partial<ApprovalActionError>);
    expect(h.order).toEqual(["changed"]);
  }
});

test("no questions api gives not_supported for a question row, before any call", async () => {
  const row = questionRow();
  const h = harness({ noQuestions: true });
  await expect(h.run(row, "approve", "")).rejects.toMatchObject({
    code: "not_supported",
  } satisfies Partial<ApprovalActionError>);
  expect(h.order).toEqual(["changed"]);
});

// ---- Change: approve ----

test("approve opens the change draft", async () => {
  const row = changeRow({ state: "open", revision: HASH_A });
  const draft =
    "Link two notes in this vault by adding one wikilink line to the first note, as described below:\n- ...";
  const h = harness({ changeDraft: draft });
  await h.run(row, "approve", "");
  expect(h.order).toEqual(["actChange", "openDraft", "changed"]);
  expect(h.actChange).toHaveBeenCalledWith(row.id, "approve", HASH_A, undefined);
  expect(h.openDraft).toHaveBeenCalledWith(draft);
});

test("change approve on an approved row still acts (Draft again) and opens the redrawn draft", async () => {
  const row = changeRow({ state: "approved" });
  const h = harness({ changeDraft: "same draft, again" });
  await h.run(row, "approve", "");
  expect(h.actChange).toHaveBeenCalledWith(row.id, "approve", row.revision, undefined);
  expect(h.openDraft).toHaveBeenCalledWith("same draft, again");
});

test("change approve throws editor_full after the act is recorded when the draft has no room", async () => {
  const row = changeRow();
  const h = harness({ openDraftResult: false });
  await expect(h.run(row, "approve", "")).rejects.toMatchObject({
    code: "editor_full",
    ownerText:
      "The approval was recorded, but this window has no room for another draft. Close a draft, then use Draft again in Approvals.",
  } satisfies Partial<ApprovalActionError>);
  expect(h.order).toEqual(["actChange", "openDraft", "changed"]);
});

test("a failed change approve call propagates without opening the draft", async () => {
  const row = changeRow();
  const boom = new Error("network down");
  const h = harness({ changeActError: boom });
  await expect(h.run(row, "approve", "")).rejects.toBe(boom);
  expect(h.order).toEqual(["actChange", "changed"]);
  expect(h.openDraft).not.toHaveBeenCalled();
});

// ---- Change: reject ----

test("change reject with a reason calls actChange with it, opens no draft", async () => {
  const row = changeRow({ state: "open" });
  const h = harness();
  await h.run(row, "reject", "too soon");
  expect(h.actChange).toHaveBeenCalledWith(row.id, "reject", row.revision, "too soon");
  expect(h.openDraft).not.toHaveBeenCalled();
  expect(h.order).toEqual(["actChange", "changed"]);
});

test("change reject without a reason passes none, and opens no draft", async () => {
  const row = changeRow({ state: "open" });
  const h = harness();
  await h.run(row, "reject", "");
  expect(h.actChange).toHaveBeenCalledWith(row.id, "reject", row.revision, undefined);
  expect(h.openDraft).not.toHaveBeenCalled();
});

test("a failed change reject call propagates without opening the draft", async () => {
  const row = changeRow({ state: "open" });
  const boom = new Error("stale revision");
  const h = harness({ changeActError: boom });
  await expect(h.run(row, "reject", "")).rejects.toBe(boom);
  expect(h.order).toEqual(["actChange", "changed"]);
  expect(h.openDraft).not.toHaveBeenCalled();
});

// ---- Change: guards ----

test("change actions throw other_vault before any call when the proposal is for another vault", async () => {
  const row = changeRow({ origin: { workspace_id: "ws-2", session_id: null } });
  for (const action of ["approve", "reject", "edit", "later"] as const) {
    const h = harness({ activeWorkspaceId: "ws-1" });
    await expect(h.run(row, action, "")).rejects.toMatchObject({
      code: "other_vault",
    } satisfies Partial<ApprovalActionError>);
    expect(h.order).toEqual(["changed"]);
  }
});

test("change edit and later throw not_supported without any call", async () => {
  const row = changeRow();
  for (const action of ["edit", "later"] as const) {
    const h = harness();
    await expect(h.run(row, action, "")).rejects.toMatchObject({
      code: "not_supported",
    } satisfies Partial<ApprovalActionError>);
    expect(h.order).toEqual(["changed"]);
  }
});

test("no questions api gives not_supported for a change row, before any call", async () => {
  const row = changeRow();
  const h = harness({ noQuestions: true });
  await expect(h.run(row, "approve", "")).rejects.toMatchObject({
    code: "not_supported",
  } satisfies Partial<ApprovalActionError>);
  expect(h.order).toEqual(["changed"]);
});

test("ApprovalActionError carries its code and owner text", () => {
  const error = new ApprovalActionError("other_vault", "This proposal is for another vault.");
  expect(error).toBeInstanceOf(Error);
  expect(error.code).toBe("other_vault");
  expect(error.ownerText).toBe("This proposal is for another vault.");
});
