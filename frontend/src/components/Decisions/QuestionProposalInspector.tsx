/**
 * One question-wording proposal in full: the wording in use beside the proposed one, and how the
 * proposed wording did while it was tried. Approve, reject and undo come from the shared
 * `approvalActions` registry; approving or rejecting applies at once, with no chat draft. Every
 * server string — both wordings' JSON — reaches the DOM only as a text node, never parsed as
 * markdown or HTML.
 */
import { type ReactElement, useEffect, useId, useRef, useState } from "react";
import {
  type DecisionLoopApi,
  DecisionLoopApiError,
  type QuestionProposalView,
  type TrialRate,
} from "../../clients/decisionLoop/types";
import type { QuestionProposalItem } from "../../state/decisionsApi";
import { useModalFocus } from "../Dialog/useModalFocus";
import "./DecisionInbox.css";
import { type ApprovalActionSpec, approvalActions } from "./approvalKinds";
import { ReasonDialog } from "./ReasonDialog";

export interface QuestionProposalInspectorProps {
  item: QuestionProposalItem;
  api: Pick<DecisionLoopApi, "proposal" | "actProposal">;
  activeWorkspaceId: string | null;
  onClose: () => void;
  onChanged: () => void;
}

const DASH = "–";

/** `n` with thousands separators, pinned to en-US so a test's locale never moves the digits
 * (matches `LearningParts.tsx`'s own `formatNumber`). */
function formatNumber(n: number): string {
  return n.toLocaleString("en-US");
}

/** A 0..1 rate as a whole-number percentage ("68%"), or the dash while that side has no labelled
 * decision yet. */
function formatPercent(rate: number | null): string {
  return rate !== null ? `${Math.round(rate * 100)}%` : DASH;
}

/** One wording's own useful-when-Yes/useful-when-No line, labelled by which wording it is. */
function usefulLine(label: string, rate: TrialRate): string {
  return `${label} — Useful when Yes: ${formatPercent(rate.yes_rate)} (${formatNumber(rate.yes_n)}) / when No: ${formatPercent(rate.no_rate)} (${formatNumber(rate.no_n)})`;
}

/** The user-facing line for a failed approve/reject/undo; `not_applicable` means the proposal
 * moved (another tab, or the beat) since this dialog loaded it. */
function actionRefusal(error: unknown): string {
  if (error instanceof DecisionLoopApiError && error.code === "not_applicable") {
    return "This proposal changed since you opened it. Close and reopen it before acting again.";
  }
  return "Couldn't do that. Try again.";
}

export function QuestionProposalInspector({
  item,
  api,
  activeWorkspaceId,
  onClose,
  onChanged,
}: QuestionProposalInspectorProps): ReactElement {
  const [view, setView] = useState<QuestionProposalView | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // The action spec ReasonDialog is open for; null while it is closed.
  const [confirming, setConfirming] = useState<ApprovalActionSpec | null>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const titleId = useId();

  useModalFocus({ active: true, dialogRef, initialFocusRef: closeRef, onDismiss: onClose });

  useEffect(() => {
    const controller = new AbortController();
    setLoadFailed(false);
    api
      .proposal(item.id, controller.signal)
      .then(setView)
      .catch(() => {
        if (!controller.signal.aborted) setLoadFailed(true);
      });
    return () => controller.abort();
  }, [api, item.id]);

  // One action in flight at a time; approve and reject are the only wire actions a question
  // proposal ever offers (no edit, no later), whatever label approvalActions puts on the button.
  function runAction(spec: ApprovalActionSpec, reason?: string): void {
    if (!view || busy || (spec.action !== "approve" && spec.action !== "reject")) return;
    const proposalId = view.proposal.id;
    const revision = view.proposal.revision;
    const action = spec.action;
    setBusy(true);
    setActionError(null);
    api
      .actProposal(proposalId, action, revision, reason)
      .then(() => api.proposal(proposalId))
      .then((next) => {
        setView(next);
        onChanged();
      })
      .catch((error: unknown) => setActionError(actionRefusal(error)))
      .finally(() => setBusy(false));
  }

  function onAction(spec: ApprovalActionSpec): void {
    if (spec.note === "reason") {
      setConfirming(spec);
      return;
    }
    runAction(spec);
  }

  const actions = view ? approvalActions(view.proposal, { activeWorkspaceId }) : [];

  return (
    <>
      <div className="decision-inbox-backdrop">
        <div
          ref={dialogRef}
          role="dialog"
          aria-modal="true"
          aria-labelledby={titleId}
          className="decision-inbox"
        >
          <div className="decision-inbox-head">
            <h2 id={titleId} className="decision-inbox-title">
              {item.title}
            </h2>
            <button type="button" ref={closeRef} className="btn txt sm" onClick={onClose}>
              Close
            </button>
          </div>
          {loadFailed ? <p className="decision-inbox-note">Could not read this proposal.</p> : null}
          {view ? (
            <div className="decision-inbox-details">
              <h3>Wording in use</h3>
              <pre className="mono" style={{ maxHeight: "40vh", overflow: "auto" }}>
                {view.current_text}
              </pre>
              <h3>Proposed wording</h3>
              <pre className="mono" style={{ maxHeight: "40vh", overflow: "auto" }}>
                {view.proposed_text}
              </pre>
              <p className="decision-inbox-note">
                {view.tried.decisions === 0
                  ? "Not tried yet"
                  : `Tried on ${formatNumber(view.tried.decisions)} decisions · same answer on ${formatNumber(view.tried.agree)}`}
              </p>
              {view.tried.decisions > 0 && view.tried.decisions < 20 ? (
                <p className="decision-inbox-note">Few decisions so far; the numbers may move.</p>
              ) : null}
              {view.tried.labels > 0 ? (
                <ul>
                  <li>{usefulLine("Wording in use", view.tried.live)}</li>
                  <li>{usefulLine("Proposed wording", view.tried.trial)}</li>
                </ul>
              ) : null}
              {actionError ? (
                <p className="decision-inbox-note" role="status">
                  {actionError}
                </p>
              ) : null}
              <div className="decisions-batch">
                {actions.map((spec) => (
                  <button
                    key={spec.action}
                    type="button"
                    className="btn alt sm"
                    title={spec.hint}
                    disabled={!spec.enabled || busy}
                    onClick={() => onAction(spec)}
                  >
                    {spec.label}
                  </button>
                ))}
              </div>
            </div>
          ) : null}
        </div>
      </div>
      {confirming ? (
        <ReasonDialog
          title={item.title}
          noteLabel="Reason (optional)"
          confirmLabel={confirming.label}
          onConfirm={(note) => {
            const spec = confirming;
            setConfirming(null);
            runAction(spec, note || undefined);
          }}
          onCancel={() => setConfirming(null)}
        />
      ) : null}
    </>
  );
}
