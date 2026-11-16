/**
 * Skill and question-wording actions for one finished reply, drawn into Conversation's
 * `.turn-actions` row through its `renderTurnActions` slot (see that file's header note, beside
 * Retry… — Quote and New chat with this turn are in the More actions menu).
 *
 * "Make this a skill" fills the message box with a drafting prompt (`draftPrompt`) and quiets the
 * hint line this turn may be showing (`onHintsOff`), the one the user can also dismiss with
 * "Not now" — left out entirely while `hintInLauncher` is true, since the hint then shows only as
 * the launcher's own item. "Propose this skill" takes a SKILL.md block already in this reply's
 * text (`skillBlock`) and sends it straight to Approvals through `api`. "Propose this wording"
 * does the same for a `decision-question` block (`questionBlock`), creating it through
 * `questions.createProposal` when that API is given.
 */
import { type ReactElement, useMemo, useState } from "react";
import { type DecisionLoopApi, DecisionLoopApiError } from "../../clients/decisionLoop/types";
import type { SessionRow } from "../../protocol";
import type { QuestionProposalItem } from "../../state/decisionsApi";
import {
  type CreatedSkillProposal,
  type SkillProposalItem,
  type SkillProposalsApi,
  skillRefusal,
} from "../../state/skillProposalsApi";
import {
  draftPrompt,
  lastFinishedTurnId,
  questionBlock,
  skillBlock,
  skillHint,
} from "../../state/skillTriggers";
import type { SessionView, Turn } from "../../state/view";

export interface SkillTurnActionsProps {
  turn: Turn;
  view: SessionView;
  sessions: readonly SessionRow[];
  api: SkillProposalsApi;
  workspaceId: string;
  /** True once the user dismissed this session's hint (Not now) or already acted on it (Make). */
  hintsOff: boolean;
  onHintsOff: () => void;
  /** Puts `text` in the message box; the user's own Send decides whether it goes anywhere. */
  onDraft: (text: string) => void;
  /** Opens Approvals on the given proposal; null for a replay, which has no fresh item to show. */
  onOpenProposal: (item: SkillProposalItem | null) => void;
  /** True while the hint line has moved into the launcher (Proactive VIKI on, not paused, and
   * Self-improvement on): the buttons below stay, only the inline hint note and its Not now
   * button are left out. */
  hintInLauncher: boolean;
  /** The question-proposal create call, when Decision is reachable; without it, a well-formed
   * `decision-question` block offers no Propose button. */
  questions?: Pick<DecisionLoopApi, "createProposal">;
  /** Opens Approvals on a freshly created question proposal. */
  onOpenQuestionProposal?: (item: QuestionProposalItem) => void;
}

type ProposeStatus = "idle" | "proposing" | "done" | "failed";

const QUESTION_REFUSALS: Record<string, string> = {
  not_applicable: "Not added: another wording is being tried, or this one is already in use.",
  invalid_request: "That wording doesn't fit the rules for Decision's questions.",
};
const QUESTION_FALLBACK_REFUSAL = "Couldn't add it to Approvals.";

/** The line for a proposal refusal's code; the fallback for anything else. */
function questionRefusal(error: unknown): string {
  const code = error instanceof DecisionLoopApiError ? error.code : "";
  return QUESTION_REFUSALS[code] ?? QUESTION_FALLBACK_REFUSAL;
}

export function SkillTurnActions({
  turn,
  view,
  sessions,
  api,
  workspaceId,
  hintsOff,
  onHintsOff,
  onDraft,
  onOpenProposal,
  hintInLauncher,
  questions,
  onOpenQuestionProposal,
}: SkillTurnActionsProps): ReactElement | null {
  const [status, setStatus] = useState<ProposeStatus>("idle");
  const [result, setResult] = useState<CreatedSkillProposal | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  const [questionStatus, setQuestionStatus] = useState<ProposeStatus>("idle");
  const [questionResult, setQuestionResult] = useState<QuestionProposalItem | null>(null);
  const [questionFailure, setQuestionFailure] = useState<string | null>(null);

  const isLast = turn.id === lastFinishedTurnId(view);
  const hint = useMemo(() => (isLast ? skillHint(view, sessions) : null), [isLast, view, sessions]);
  const block = useMemo(() => (turn.status === "success" ? skillBlock(turn) : null), [turn]);
  const question = useMemo(() => (turn.status === "success" ? questionBlock(turn) : null), [turn]);

  if (turn.status !== "success") return null;

  function make(): void {
    onDraft(draftPrompt(turn, hint));
    onHintsOff();
  }

  async function propose(): Promise<void> {
    if (!block || status === "proposing") return;
    setStatus("proposing");
    setFailure(null);
    try {
      const created = await api.create(
        workspaceId,
        block,
        hint?.trigger ?? "button",
        hint?.sessions ?? [view.sessionId],
      );
      setResult(created);
      setStatus("done");
    } catch (error) {
      setFailure(skillRefusal(error));
      setStatus("failed");
    }
  }

  async function proposeWording(): Promise<void> {
    if (!question || !questions || questionStatus === "proposing") return;
    setQuestionStatus("proposing");
    setQuestionFailure(null);
    try {
      const item = await questions.createProposal(question.name, question.question);
      setQuestionResult(item);
      setQuestionStatus("done");
    } catch (error) {
      setQuestionFailure(questionRefusal(error));
      setQuestionStatus("failed");
    }
  }

  return (
    <>
      <button type="button" onClick={make}>
        Make this a skill
      </button>
      {block && status !== "done" ? (
        <button type="button" onClick={() => void propose()} disabled={status === "proposing"}>
          {status === "proposing" ? "Proposing…" : "Propose this skill"}
        </button>
      ) : null}
      {status === "failed" && failure ? <span role="status">{failure}</span> : null}
      {status === "done" && result ? (
        <>
          <span role="status">
            {result.replayed ? "Already in Approvals." : "Added to Approvals."}
          </span>
          <button type="button" onClick={() => onOpenProposal(result.item)}>
            Open in Approvals
          </button>
        </>
      ) : null}
      {question && questions && questionStatus !== "done" ? (
        <button
          type="button"
          onClick={() => void proposeWording()}
          disabled={questionStatus === "proposing"}
        >
          {questionStatus === "proposing" ? "Proposing…" : "Propose this wording"}
        </button>
      ) : null}
      {questionStatus === "failed" && questionFailure ? (
        <span role="status">{questionFailure}</span>
      ) : null}
      {questionStatus === "done" && questionResult ? (
        <>
          <span role="status">Added to Approvals.</span>
          <button type="button" onClick={() => onOpenQuestionProposal?.(questionResult)}>
            Open in Approvals
          </button>
        </>
      ) : null}
      {isLast && hint && !hintsOff && !hintInLauncher ? (
        <>
          <span role="note" className="skill-hint">
            {hint.line}
          </span>
          <button type="button" onClick={onHintsOff}>
            Not now
          </button>
        </>
      ) : null}
    </>
  );
}
