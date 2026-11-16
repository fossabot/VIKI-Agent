import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import {
  type DecisionLoopApi,
  DecisionLoopApiError,
  type QuestionProposalView,
  type TrialRate,
} from "../../clients/decisionLoop/types";
import type { QuestionProposalItem } from "../../state/decisionsApi";
import { QuestionProposalInspector } from "./QuestionProposalInspector";

afterEach(cleanup);

const HASH = "a".repeat(64);
const NO_TRIAL: TrialRate = { yes_n: 0, yes_rate: null, no_n: 0, no_rate: null };

function questionItem(over: Partial<QuestionProposalItem> = {}): QuestionProposalItem {
  return {
    kind: "question_proposal",
    id: "11111111-1111-1111-1111-111111111111",
    origin: { workspace_id: "w1", session_id: null },
    revision: HASH,
    state: "open",
    attention: true,
    title: "Question change: New to you?",
    name: "news",
    created_at: "2026-10-19T09:30:00.000Z",
    updated_at: "2026-10-19T09:30:00.000Z",
    approved_at: null,
    rejected_at: null,
    reason: null,
    ...over,
  };
}

function proposalView(over: Partial<QuestionProposalView> = {}): QuestionProposalView {
  return {
    proposal: questionItem(),
    label: "New to you?",
    current_text: '{\n  "type": "noul"\n}',
    proposed_text: '{\n  "type": "noul",\n  "changed": true\n}',
    tried: { decisions: 0, agree: 0, labels: 0, live: NO_TRIAL, trial: NO_TRIAL },
    ...over,
  };
}

function setup(found: QuestionProposalView, active: string | null = "w1") {
  const proposalFn = vi.fn<DecisionLoopApi["proposal"]>().mockResolvedValue(found);
  const actFn = vi.fn<DecisionLoopApi["actProposal"]>();
  const api: Pick<DecisionLoopApi, "proposal" | "actProposal"> = {
    proposal: proposalFn,
    actProposal: actFn,
  };
  const props = { onClose: vi.fn(), onChanged: vi.fn() };
  render(
    <QuestionProposalInspector
      item={found.proposal}
      api={api}
      activeWorkspaceId={active}
      {...props}
    />,
  );
  return { proposalFn, actFn, ...props };
}

const settled = () => act(async () => {});
const button = (name: string) => screen.getByRole("button", { name }) as HTMLButtonElement;

test("shows the title and both wordings as plain text, never parsed as markup", async () => {
  const found = proposalView({
    current_text: "Wraps <b>bold</b> text.",
    proposed_text: "Also <i>wraps</i> text.",
  });
  setup(found);
  await settled();
  const dialog = screen.getByRole("dialog");
  const label = document.getElementById(dialog.getAttribute("aria-labelledby") ?? "");
  expect(label?.textContent).toBe("Question change: New to you?");
  expect(screen.getByText("Wording in use")).toBeTruthy();
  expect(screen.getByText("Proposed wording")).toBeTruthy();
  expect(screen.getByText("Wraps <b>bold</b> text.")).toBeTruthy();
  expect(screen.getByText("Also <i>wraps</i> text.")).toBeTruthy();
  expect(dialog.querySelector("b")).toBeNull();
  expect(dialog.querySelector("i")).toBeNull();
});

test("shows Not tried yet at zero decisions, with no few-decisions note or useful-when lines", async () => {
  setup(proposalView());
  await settled();
  expect(screen.getByText("Not tried yet")).toBeTruthy();
  expect(screen.queryByText("Few decisions so far; the numbers may move.")).toBeNull();
  expect(screen.queryByText(/Useful when Yes/)).toBeNull();
});

test("shows the tried count and agreement, with the few-decisions note below 20", async () => {
  setup(
    proposalView({
      tried: { decisions: 19, agree: 10, labels: 0, live: NO_TRIAL, trial: NO_TRIAL },
    }),
  );
  await settled();
  expect(screen.getByText("Tried on 19 decisions · same answer on 10")).toBeTruthy();
  expect(screen.getByText("Few decisions so far; the numbers may move.")).toBeTruthy();
});

test("drops the few-decisions note once 20 decisions have been tried", async () => {
  setup(
    proposalView({
      tried: { decisions: 20, agree: 11, labels: 0, live: NO_TRIAL, trial: NO_TRIAL },
    }),
  );
  await settled();
  expect(screen.getByText("Tried on 20 decisions · same answer on 11")).toBeTruthy();
  expect(screen.queryByText("Few decisions so far; the numbers may move.")).toBeNull();
});

test("once some decisions are labelled, shows each wording's useful-when-Yes/No rates", async () => {
  setup(
    proposalView({
      tried: {
        decisions: 24,
        agree: 19,
        labels: 6,
        live: { yes_n: 4, yes_rate: 0.75, no_n: 2, no_rate: 0.5 },
        trial: { yes_n: 3, yes_rate: 0.667, no_n: 3, no_rate: 0.333 },
      },
    }),
  );
  await settled();
  expect(
    screen.getByText("Wording in use — Useful when Yes: 75% (4) / when No: 50% (2)"),
  ).toBeTruthy();
  expect(
    screen.getByText("Proposed wording — Useful when Yes: 67% (3) / when No: 33% (3)"),
  ).toBeTruthy();
});

test("a side with no labelled decision yet shows a dash instead of a rate", async () => {
  setup(
    proposalView({
      tried: {
        decisions: 5,
        agree: 5,
        labels: 5,
        live: { yes_n: 0, yes_rate: null, no_n: 5, no_rate: 0.4 },
        trial: { yes_n: 5, yes_rate: 0.2, no_n: 0, no_rate: null },
      },
    }),
  );
  await settled();
  expect(
    screen.getByText("Wording in use — Useful when Yes: – (0) / when No: 40% (5)"),
  ).toBeTruthy();
  expect(
    screen.getByText("Proposed wording — Useful when Yes: 20% (5) / when No: – (0)"),
  ).toBeTruthy();
});

test("Close calls onClose", async () => {
  const { onClose } = setup(proposalView());
  await settled();
  fireEvent.click(button("Close"));
  expect(onClose).toHaveBeenCalledOnce();
});

test("a load failure shows a message and renders no wording", async () => {
  const proposalFn = vi
    .fn<DecisionLoopApi["proposal"]>()
    .mockRejectedValue(new DecisionLoopApiError("unavailable", 503));
  const api: Pick<DecisionLoopApi, "proposal" | "actProposal"> = {
    proposal: proposalFn,
    actProposal: vi.fn(),
  };
  render(
    <QuestionProposalInspector
      item={questionItem()}
      api={api}
      activeWorkspaceId="w1"
      onClose={vi.fn()}
      onChanged={vi.fn()}
    />,
  );
  await settled();
  expect(screen.getByText("Could not read this proposal.")).toBeTruthy();
  expect(screen.queryByText("Wording in use")).toBeNull();
});

test("unmounting aborts the in-flight load", async () => {
  let signal: AbortSignal | undefined;
  const proposalFn = vi.fn((_id: string, s?: AbortSignal) => {
    signal = s;
    return new Promise<QuestionProposalView>(() => {});
  });
  const api: Pick<DecisionLoopApi, "proposal" | "actProposal"> = {
    proposal: proposalFn as DecisionLoopApi["proposal"],
    actProposal: vi.fn(),
  };
  const { unmount } = render(
    <QuestionProposalInspector
      item={questionItem()}
      api={api}
      activeWorkspaceId="w1"
      onClose={vi.fn()}
      onChanged={vi.fn()}
    />,
  );
  unmount();
  expect(signal?.aborted).toBe(true);
});

test("an open proposal in its own vault offers enabled Approve and Reject buttons", async () => {
  setup(proposalView({ proposal: questionItem({ state: "open" }) }), "w1");
  await settled();
  expect(button("Approve").disabled).toBe(false);
  expect(button("Reject").disabled).toBe(false);
});

test("Reject opens ReasonDialog; confirming calls actProposal with the trimmed reason", async () => {
  const proposal = questionItem({ state: "open" });
  const { actFn, onChanged } = setup(proposalView({ proposal }), "w1");
  actFn.mockResolvedValue(questionItem({ state: "rejected", reason: "too noisy" }));
  await settled();
  fireEvent.click(button("Reject"));
  const reasonDialog = screen.getAllByRole("dialog").at(-1) as HTMLElement;
  fireEvent.change(within(reasonDialog).getByLabelText("Reason (optional)"), {
    target: { value: "  too noisy  " },
  });
  fireEvent.click(within(reasonDialog).getByRole("button", { name: "Reject" }));
  await settled();
  expect(actFn).toHaveBeenCalledWith(proposal.id, "reject", proposal.revision, "too noisy");
  expect(onChanged).toHaveBeenCalledOnce();
});

test("Approve calls actProposal with no reason and reports changed", async () => {
  const proposal = questionItem({ state: "open" });
  const { actFn, onChanged } = setup(proposalView({ proposal }), "w1");
  actFn.mockResolvedValue(questionItem({ state: "approved" }));
  await settled();
  fireEvent.click(button("Approve"));
  await settled();
  expect(actFn).toHaveBeenCalledWith(proposal.id, "approve", proposal.revision, undefined);
  expect(onChanged).toHaveBeenCalledOnce();
});

test("a failed action shows a status message", async () => {
  const proposal = questionItem({ state: "open" });
  const { actFn } = setup(proposalView({ proposal }), "w1");
  actFn.mockRejectedValue(new DecisionLoopApiError("not_applicable", 409));
  await settled();
  fireEvent.click(button("Approve"));
  await settled();
  expect(screen.getByRole("status").textContent).toBe(
    "This proposal changed since you opened it. Close and reopen it before acting again.",
  );
});
