/**
 * Unit tests for Conversation/SkillTurnActions.tsx: the success-only gate, Make this a skill's
 * draft and hint-quieting, Propose this skill's block gate, its arguments and its waiting/
 * success/replay/refusal lines, and the hint showing only on the last live turn and hiding on
 * Not now or Make.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { type DecisionLoopApi, DecisionLoopApiError } from "../../clients/decisionLoop/types";
import type { SessionRow } from "../../protocol";
import type { QuestionProposalItem } from "../../state/decisionsApi";
import type { SkillProposalItem, SkillProposalsApi } from "../../state/skillProposalsApi";
import { SkillProposalsApiError, skillRefusal } from "../../state/skillProposalsApi";
import { draftPrompt, LONG_TURNS, skillBlock, skillHint } from "../../state/skillTriggers";
import { emptyView, type SessionView, type TextBlock, type Turn } from "../../state/view";
import { SkillTurnActions, type SkillTurnActionsProps } from "./SkillTurnActions";

// skillBlock stays the real implementation; wrapping it only counts calls, for the memoisation
// test below.
vi.mock("../../state/skillTriggers", async (importOriginal) => {
  const original = await importOriginal<typeof import("../../state/skillTriggers")>();
  return { ...original, skillBlock: vi.fn(original.skillBlock) };
});

afterEach(cleanup);

// ---- fixtures --------------------------------------------------------------------------------

function turn(id: string, overrides: Partial<Turn> = {}): Turn {
  return {
    id,
    user: null,
    blocks: [],
    status: "success",
    usage: null,
    durationMs: 0,
    history: false,
    ...overrides,
  };
}

function userTurn(id: string, text = "step"): Turn {
  return turn(id, { user: { text, command: "", at: "" } });
}

function textBlock(text: string, id = "b"): TextBlock {
  return { kind: "text", id, text, done: true };
}

const SKILL_BODY = "---\nname: x\ndescription: d\n---\nbody";

/** A finished reply whose own text holds exactly one SKILL.md block. */
function turnWithSkill(id: string): Turn {
  return turn(id, { blocks: [textBlock(["```SKILL.md", SKILL_BODY, "```"].join("\n"))] });
}

const QUESTION_BODY = JSON.stringify({ name: "worth_note", question: { ok: true } });

/** A finished reply whose own text holds exactly one decision-question block. */
function turnWithQuestion(id: string, body: string = QUESTION_BODY): Turn {
  return turn(id, { blocks: [textBlock(["```decision-question", body, "```"].join("\n"))] });
}

function view(turns: Turn[], sessionId = "this"): SessionView {
  return { ...emptyView(sessionId), turns };
}

/** `LONG_TURNS` user turns (so `skillHint` fires long_session) ending in a reply that itself
 * holds a SKILL.md block, so the same fixture can drive both the hint and Propose assertions. */
function longSessionTurns(): { turns: Turn[]; first: Turn; last: Turn } {
  const first = userTurn("u0");
  const middle = Array.from({ length: LONG_TURNS - 1 }, (_, i) => userTurn(`u${i + 1}`));
  const last = turnWithSkill("reply");
  return { turns: [first, ...middle, last], first, last };
}

function proposalItem(overrides: Partial<SkillProposalItem> = {}): SkillProposalItem {
  return {
    kind: "skill_proposal",
    id: "00000000-0000-4000-8000-000000000002",
    origin: { workspace_id: "00000000-0000-4000-8000-000000000001", session_id: null },
    revision: "a".repeat(64),
    state: "open",
    attention: true,
    title: "New skill: my-skill",
    name: "my-skill",
    relative_id: "skills/my-skill/SKILL.md",
    trigger: "button",
    sessions: ["00000000-0000-4000-8000-000000000003"],
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

function questionProposalItem(overrides: Partial<QuestionProposalItem> = {}): QuestionProposalItem {
  return {
    kind: "question_proposal",
    id: "00000000-0000-4000-8000-000000000009",
    origin: { workspace_id: "00000000-0000-4000-8000-000000000001", session_id: null },
    revision: "b".repeat(64),
    state: "open",
    attention: true,
    title: "Question change: Worth a note?",
    name: "worth_note",
    created_at: "2026-10-23T03:04:05.678Z",
    updated_at: "2026-10-23T03:04:05.678Z",
    approved_at: null,
    rejected_at: null,
    reason: null,
    ...overrides,
  };
}

function sessionRowFixture(overrides: Partial<SessionRow> & { session_id: string }): SessionRow {
  return {
    title: "",
    first_prompt: "",
    entrypoint: "",
    modified: "2026-10-02T00:00:00.000Z",
    cli_version: "",
    other: false,
    ...overrides,
  };
}

function fakeApi(overrides: Partial<SkillProposalsApi> = {}): SkillProposalsApi {
  return {
    create: vi.fn<SkillProposalsApi["create"]>(),
    detail: vi.fn<SkillProposalsApi["detail"]>(),
    act: vi.fn<SkillProposalsApi["act"]>(),
    ...overrides,
  };
}

function fakeQuestions(
  overrides: Partial<Pick<DecisionLoopApi, "createProposal">> = {},
): Pick<DecisionLoopApi, "createProposal"> {
  return {
    createProposal: vi.fn<DecisionLoopApi["createProposal"]>(),
    ...overrides,
  };
}

function setup(overrides: Partial<SkillTurnActionsProps> = {}) {
  const defaultTurn = turn("t1");
  const onHintsOff = vi.fn();
  const onDraft = vi.fn();
  const onOpenProposal = vi.fn();
  const onOpenQuestionProposal = vi.fn();
  const props: SkillTurnActionsProps = {
    turn: defaultTurn,
    view: view([defaultTurn]),
    sessions: [],
    api: fakeApi(),
    workspaceId: "w1",
    hintsOff: false,
    onHintsOff,
    onDraft,
    onOpenProposal,
    hintInLauncher: false,
    onOpenQuestionProposal,
    ...overrides,
  };
  const { rerender } = render(<SkillTurnActions {...props} />);
  return { onHintsOff, onDraft, onOpenProposal, onOpenQuestionProposal, rerender, props };
}

const settled = () => act(async () => {});

// ---- the success-only gate ---------------------------------------------------------------------

test("renders nothing for a running turn", () => {
  setup({ turn: turn("t1", { status: "running" }) });
  expect(screen.queryByRole("button", { name: "Make this a skill" })).toBeNull();
});

test("renders nothing for an errored turn", () => {
  setup({ turn: turn("t1", { status: "error" }) });
  expect(screen.queryByRole("button", { name: "Make this a skill" })).toBeNull();
});

// ---- Make this a skill --------------------------------------------------------------------------

test("Make this a skill drafts the prompt and quiets hints", () => {
  const t = turn("t1", { blocks: [textBlock("hello")] });
  const { onDraft, onHintsOff } = setup({ turn: t, view: view([t]) });
  fireEvent.click(screen.getByRole("button", { name: "Make this a skill" }));
  expect(onDraft).toHaveBeenCalledWith(draftPrompt(t, null));
  expect(onHintsOff).toHaveBeenCalledTimes(1);
});

test("Make this a skill folds in the hint's reason when a hint is showing", () => {
  const { turns, last } = longSessionTurns();
  const v = view(turns);
  const { onDraft } = setup({ turn: last, view: v });
  fireEvent.click(screen.getByRole("button", { name: "Make this a skill" }));
  expect(onDraft).toHaveBeenCalledWith(draftPrompt(last, skillHint(v, [])));
});

// ---- Propose this skill -------------------------------------------------------------------------

test("no Propose button without a SKILL.md block", () => {
  setup({ turn: turn("t1", { blocks: [textBlock("just words")] }) });
  expect(screen.queryByRole("button", { name: "Propose this skill" })).toBeNull();
});

test("Propose this skill sends the block, defaults to button, and reports Added to Approvals", async () => {
  const t = turnWithSkill("t1");
  const item = proposalItem();
  const create = vi
    .fn<SkillProposalsApi["create"]>()
    .mockResolvedValue({ id: "p1", item, replayed: false });
  const { onOpenProposal } = setup({
    turn: t,
    view: view([t], "sess-1"),
    workspaceId: "w9",
    api: fakeApi({ create }),
  });

  fireEvent.click(screen.getByRole("button", { name: "Propose this skill" }));
  expect((screen.getByRole("button", { name: "Proposing…" }) as HTMLButtonElement).disabled).toBe(
    true,
  );
  expect(create).toHaveBeenCalledWith("w9", skillBlock(t), "button", ["sess-1"]);

  await settled();
  expect(screen.getByText("Added to Approvals.")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Propose this skill" })).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "Open in Approvals" }));
  expect(onOpenProposal).toHaveBeenCalledWith(item);
});

test("a replay reports Already in Approvals and opens with a null item", async () => {
  const t = turnWithSkill("t1");
  const create = vi
    .fn<SkillProposalsApi["create"]>()
    .mockResolvedValue({ id: "p1", item: null, replayed: true });
  const { onOpenProposal } = setup({ turn: t, view: view([t]), api: fakeApi({ create }) });

  fireEvent.click(screen.getByRole("button", { name: "Propose this skill" }));
  await settled();
  expect(screen.getByText("Already in Approvals.")).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "Open in Approvals" }));
  expect(onOpenProposal).toHaveBeenCalledWith(null);
});

test("a refusal shows skillRefusal's line and keeps the Propose button", async () => {
  const t = turnWithSkill("t1");
  const error = new SkillProposalsApiError("no_vault");
  const create = vi.fn<SkillProposalsApi["create"]>().mockRejectedValue(error);
  setup({ turn: t, view: view([t]), api: fakeApi({ create }) });

  fireEvent.click(screen.getByRole("button", { name: "Propose this skill" }));
  await settled();
  expect(screen.getByText(skillRefusal(error))).toBeTruthy();
  expect(screen.getByRole("button", { name: "Propose this skill" })).toBeTruthy();
});

test("Propose this skill uses the hint's trigger but only this chat's own id", () => {
  const { turns, last } = longSessionTurns();
  const create = vi
    .fn<SkillProposalsApi["create"]>()
    .mockResolvedValue({ id: "p1", item: proposalItem(), replayed: false });
  setup({
    turn: last,
    view: view(turns, "sess-9"),
    workspaceId: "w1",
    api: fakeApi({ create }),
  });
  fireEvent.click(screen.getByRole("button", { name: "Propose this skill" }));
  expect(create).toHaveBeenCalledWith("w1", skillBlock(last), "long_session", ["sess-9"]);
});

test("a repeated_request hint sends this chat plus its matches, newer modified first", () => {
  const pivot = userTurn("u0", "please help me plan the launch");
  const last = turnWithSkill("reply");
  const newer = sessionRowFixture({
    session_id: "newer",
    first_prompt: "please help me plan the launch today",
    modified: "2026-10-02T00:00:00.000Z",
  });
  const older = sessionRowFixture({
    session_id: "older",
    first_prompt: "please help me plan the launch now",
    modified: "2026-10-01T00:00:00.000Z",
  });
  const create = vi
    .fn<SkillProposalsApi["create"]>()
    .mockResolvedValue({ id: "p1", item: proposalItem(), replayed: false });
  setup({
    turn: last,
    view: view([pivot, last], "sess-this"),
    sessions: [older, newer],
    api: fakeApi({ create }),
  });
  fireEvent.click(screen.getByRole("button", { name: "Propose this skill" }));
  expect(create).toHaveBeenCalledWith("w1", skillBlock(last), "repeated_request", [
    "sess-this",
    "newer",
    "older",
  ]);
});

// ---- Propose this wording ------------------------------------------------------------------------

test("no Propose this wording button without a decision-question block", () => {
  setup({
    turn: turn("t1", { blocks: [textBlock("just words")] }),
    questions: fakeQuestions(),
  });
  expect(screen.queryByRole("button", { name: "Propose this wording" })).toBeNull();
});

test("no Propose this wording button without the questions api", () => {
  const t = turnWithQuestion("t1");
  setup({ turn: t, view: view([t]) });
  expect(screen.queryByRole("button", { name: "Propose this wording" })).toBeNull();
});

test("Propose this wording sends the block's name and question, and reports Added to Approvals", async () => {
  const t = turnWithQuestion("t1");
  const item = questionProposalItem();
  const createProposal = vi.fn<DecisionLoopApi["createProposal"]>().mockResolvedValue(item);
  const { onOpenQuestionProposal } = setup({
    turn: t,
    view: view([t]),
    questions: fakeQuestions({ createProposal }),
  });

  fireEvent.click(screen.getByRole("button", { name: "Propose this wording" }));
  expect((screen.getByRole("button", { name: "Proposing…" }) as HTMLButtonElement).disabled).toBe(
    true,
  );
  expect(createProposal).toHaveBeenCalledWith("worth_note", { ok: true });

  await settled();
  expect(screen.getByText("Added to Approvals.")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Propose this wording" })).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "Open in Approvals" }));
  expect(onOpenQuestionProposal).toHaveBeenCalledWith(item);
});

test("a not_applicable refusal shows its own line and keeps the Propose button", async () => {
  const t = turnWithQuestion("t1");
  const createProposal = vi
    .fn<DecisionLoopApi["createProposal"]>()
    .mockRejectedValue(new DecisionLoopApiError("not_applicable", 409));
  setup({ turn: t, view: view([t]), questions: fakeQuestions({ createProposal }) });

  fireEvent.click(screen.getByRole("button", { name: "Propose this wording" }));
  await settled();
  expect(
    screen.getByText("Not added: another wording is being tried, or this one is already in use."),
  ).toBeTruthy();
  expect(screen.getByRole("button", { name: "Propose this wording" })).toBeTruthy();
});

test("an invalid_request refusal shows its own line", async () => {
  const t = turnWithQuestion("t1");
  const createProposal = vi
    .fn<DecisionLoopApi["createProposal"]>()
    .mockRejectedValue(new DecisionLoopApiError("invalid_request", 422));
  setup({ turn: t, view: view([t]), questions: fakeQuestions({ createProposal }) });

  fireEvent.click(screen.getByRole("button", { name: "Propose this wording" }));
  await settled();
  expect(
    screen.getByText("That wording doesn't fit the rules for Decision's questions."),
  ).toBeTruthy();
});

test("any other refusal falls back to a generic line", async () => {
  const t = turnWithQuestion("t1");
  const createProposal = vi
    .fn<DecisionLoopApi["createProposal"]>()
    .mockRejectedValue(new Error("boom"));
  setup({ turn: t, view: view([t]), questions: fakeQuestions({ createProposal }) });

  fireEvent.click(screen.getByRole("button", { name: "Propose this wording" }));
  await settled();
  expect(screen.getByText("Couldn't add it to Approvals.")).toBeTruthy();
});

// ---- the memoised block (perf) ------------------------------------------------------------------

test("rerendering with the same turn object does not call skillBlock again", () => {
  const spy = vi.mocked(skillBlock);
  const before = spy.mock.calls.length;
  const t = turnWithSkill("t1");
  const { rerender, props } = setup({ turn: t, view: view([t]) });
  const afterMount = spy.mock.calls.length;
  expect(afterMount).toBeGreaterThan(before);

  rerender(<SkillTurnActions {...props} />);
  expect(spy.mock.calls.length).toBe(afterMount);
});

// ---- the hint -------------------------------------------------------------------------------------

test("the hint shows only on the last live turn, and never with hintsOff", () => {
  const { turns, first, last } = longSessionTurns();
  const v = view(turns);

  setup({ turn: last, view: v });
  expect(screen.getByRole("note").textContent).toBe(
    "This chat ran a long task to the end. Worth keeping as a skill?",
  );
  expect(screen.getByRole("button", { name: "Not now" })).toBeTruthy();
  cleanup();

  setup({ turn: first, view: v });
  expect(screen.queryByRole("note")).toBeNull();
  cleanup();

  setup({ turn: last, view: v, hintsOff: true });
  expect(screen.queryByRole("note")).toBeNull();
});

test("Not now calls onHintsOff", () => {
  const { turns, last } = longSessionTurns();
  const { onHintsOff } = setup({ turn: last, view: view(turns) });
  fireEvent.click(screen.getByRole("button", { name: "Not now" }));
  expect(onHintsOff).toHaveBeenCalledTimes(1);
});

test("hintInLauncher hides the hint line", () => {
  const { turns, last } = longSessionTurns();
  setup({ turn: last, view: view(turns), hintInLauncher: true });
  expect(screen.queryByRole("note")).toBeNull();
  expect(screen.queryByRole("button", { name: "Not now" })).toBeNull();
  expect(screen.getByRole("button", { name: "Make this a skill" })).toBeTruthy();
});
