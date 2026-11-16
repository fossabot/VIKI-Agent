/**
 * One proposed skill in full: what it does, where it came from, the diff against the installed
 * file (when there is one), and the user's controls (`SkillProposalActions`). Every server
 * string — the diff, the description, a rejection reason, a session label — reaches the DOM only
 * as a text node, never parsed as markdown or HTML.
 */
import { type ReactElement, useEffect, useId, useRef, useState } from "react";
import type {
  SkillProposalDetail,
  SkillProposalItem,
  SkillProposalsApi,
} from "../../state/skillProposalsApi";
import {
  SKILL_STATE_WORDS,
  SKILL_TRIGGER_WORDS,
  skillRefusal,
} from "../../state/skillProposalsApi";
import { CopyButton } from "../Conversation/Copy";
import { useModalFocus } from "../Dialog/useModalFocus";
import "./DecisionInbox.css";
import { SkillProposalActions } from "./SkillProposalActions";
import { applyHandoffText, trialHandoffText } from "./skillHandoff";

export interface SkillProposalInspectorProps {
  api: SkillProposalsApi;
  item: SkillProposalItem;
  /** The window's active workspace: the proposal's own controls need this to match its origin. */
  activeWorkspaceId: string | null;
  sessionLabel: (id: string) => string;
  onClose: () => void;
  onChanged: () => void;
  onHandoff: (text: string) => void;
  onOpenChat: (sessionId: string, origin: HTMLDivElement) => void;
}

export function SkillProposalInspector({
  api,
  item,
  activeWorkspaceId,
  sessionLabel,
  onClose,
  onChanged,
  onHandoff,
  onOpenChat,
}: SkillProposalInspectorProps): ReactElement {
  const [detail, setDetail] = useState<SkillProposalDetail | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [retryKey, setRetryKey] = useState(0);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Whether SkillProposalActions is mid-edit with a draft that differs from detail.text.
  const [dirty, setDirty] = useState(false);
  // Whether Escape has already stepped the actions view back to idle for the dirty draft held
  // now; cleared once the draft is clean again, so a later edit gets its own first free step.
  const [steppedBack, setSteppedBack] = useState(false);
  const [leaveEditKey, setLeaveEditKey] = useState(0);
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const titleId = useId();

  function trackDirty(next: boolean): void {
    setDirty(next);
    if (!next) setSteppedBack(false);
  }

  // Escape must not throw away an unsaved edit: the first Escape while dirty steps the actions
  // view back to idle (keeping the draft, same as the design's Cancel contract) instead of
  // closing the dialog. A second Escape, with the draft still kept, asks the same confirm as
  // Close: it closes on yes and leaves the dialog and the draft alone on no.
  function dismiss(): void {
    if (dirty) {
      if (!steppedBack) {
        setSteppedBack(true);
        setLeaveEditKey((key) => key + 1);
        return;
      }
      if (!window.confirm("Discard your unsaved changes to this skill?")) return;
    }
    onClose();
  }

  useModalFocus({ active: true, dialogRef, initialFocusRef: closeRef, onDismiss: dismiss });

  // biome-ignore lint/correctness/useExhaustiveDependencies: retryKey (bumped by Retry) is an intentional re-fetch trigger, not a value this effect's body reads.
  useEffect(() => {
    const controller = new AbortController();
    setLoadFailed(false);
    api
      .detail(item.id, controller.signal)
      .then(setDetail)
      .catch(() => {
        if (!controller.signal.aborted) setLoadFailed(true);
      });
    return () => controller.abort();
  }, [api, item.id, retryKey]);

  // Shared action runner: one in flight at a time, clears the last refusal, and turns a thrown
  // error into the design's refusal line. The action itself decides what a success reports.
  function run(action: () => Promise<void>): void {
    if (busy) return;
    setBusy(true);
    setActionError(null);
    action()
      .catch((error: unknown) => setActionError(skillRefusal(error)))
      .finally(() => setBusy(false));
  }

  function approve(): void {
    if (dirty && !window.confirm("Discard your unsaved changes to this skill?")) return;
    if (!detail) return;
    const current = detail;
    run(async () => {
      await api.act(current.id, "approve", current.revision);
      onChanged();
      onHandoff(applyHandoffText(current));
    });
  }

  function edit(text: string): void {
    if (!detail) return;
    const current = detail;
    run(async () => {
      await api.act(current.id, "edit", current.revision, text);
      setDetail(await api.detail(current.id));
      onChanged();
    });
  }

  function reject(reason: string | null): void {
    if (!detail) return;
    const current = detail;
    run(async () => {
      await api.act(current.id, "reject", current.revision, undefined, reason ?? undefined);
      setDetail(await api.detail(current.id));
      onChanged();
    });
  }

  function withdraw(): void {
    if (!detail) return;
    const current = detail;
    run(async () => {
      await api.act(current.id, "withdraw", current.revision);
      onChanged();
      onClose();
    });
  }

  const state = (detail ?? item).state;
  const enabled = !!detail && detail.origin.workspace_id === activeWorkspaceId;

  return (
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
            {item.name}
          </h2>
          <button
            type="button"
            ref={closeRef}
            className="btn txt sm"
            onClick={() => {
              if (!dirty || window.confirm("Discard your unsaved changes to this skill?"))
                onClose();
            }}
          >
            Close
          </button>
        </div>
        <span className="state-chip" data-state={state}>
          {SKILL_STATE_WORDS[state]}
        </span>
        {loadFailed ? (
          <p className="decision-inbox-note">
            Could not read this proposal.{" "}
            <button
              type="button"
              className="btn alt sm"
              onClick={() => setRetryKey((key) => key + 1)}
            >
              Retry
            </button>
          </p>
        ) : null}
        {detail ? (
          <div className="decision-inbox-details">
            <p className="decision-inbox-note">
              {detail.base_revision === null ? "New skill" : "Changes the vault's skill"}{" "}
              <code className="mono">.claude/{detail.relative_id}</code>
            </p>
            <h3>What it does</h3>
            <p>{detail.description}</p>
            <h3>Where it came from</h3>
            <p>{SKILL_TRIGGER_WORDS[detail.trigger]}</p>
            <ul>
              {detail.sessions.map((sessionId) => (
                <li key={sessionId}>
                  <span>{sessionLabel(sessionId)}</span>{" "}
                  <button
                    type="button"
                    className="btn alt sm"
                    onClick={() => {
                      if (dirty && !window.confirm("Discard your unsaved changes to this skill?"))
                        return;
                      if (dialogRef.current) onOpenChat(sessionId, dialogRef.current);
                    }}
                  >
                    Open chat
                  </button>
                </li>
              ))}
            </ul>
            {detail.shadowed === true ? (
              <p className="decision-inbox-note">
                A personal skill with this name is in your Claude folder. Claude Code runs the
                personal one, so this one would not run.
              </p>
            ) : detail.shadowed === null ? (
              <p className="decision-inbox-note">Could not check your personal skills.</p>
            ) : null}
            {detail.unsupported_fields.length > 0 ? (
              <p className="decision-inbox-note">
                Fields VIKI Agent did not check: {detail.unsupported_fields.join(", ")}.
              </p>
            ) : null}
            {detail.state === "stale" ? (
              <p className="decision-inbox-note">
                The skill changed since this proposal was made. Edit it to rebase on the current
                file.
              </p>
            ) : null}
            {detail.state === "rejected" ? (
              <p className="decision-inbox-note">
                {detail.reason ? `Rejected: ${detail.reason}.` : "Rejected."}
              </p>
            ) : null}
            {detail.state === "handed" ? (
              <p className="decision-inbox-note">
                Approved. It applies when you send the apply draft and VIKI writes the file.
              </p>
            ) : null}
            {detail.state === "applied" ? (
              <p className="decision-inbox-note">The skill file matches this proposal.</p>
            ) : null}
            <h3>The file</h3>
            {detail.diff_reason === "too_large" ? (
              <p className="decision-inbox-note">Too large to compare line by line.</p>
            ) : detail.diff_reason === "unreadable" ? (
              <p className="decision-inbox-note">Could not read the installed file.</p>
            ) : null}
            <pre className="mono" style={{ maxHeight: "40vh", overflow: "auto" }}>
              {detail.diff_reason ? detail.text : detail.diff}
            </pre>
            <p className="decision-inbox-note">
              Not evaluated. Try it in a new chat to see it work before you approve.
            </p>
            <div className="decisions-batch">
              <button
                type="button"
                className="btn alt sm"
                onClick={() => {
                  if (dirty && !window.confirm("Discard your unsaved changes to this skill?"))
                    return;
                  onHandoff(trialHandoffText(detail));
                }}
              >
                Try in a new chat
              </button>
              <CopyButton getText={() => detail.text} label="Copy SKILL.md" />
            </div>
          </div>
        ) : null}
        {detail ? (
          <>
            {enabled ? null : (
              <p className="decision-inbox-note">Open this proposal's vault to act on it.</p>
            )}
            {actionError ? (
              <p className="decision-inbox-note" role="status">
                {actionError}
              </p>
            ) : null}
            <SkillProposalActions
              state={detail.state}
              text={detail.text}
              enabled={enabled}
              busy={busy}
              onApprove={approve}
              onEdit={edit}
              onReject={reject}
              onWithdraw={withdraw}
              onDirty={trackDirty}
              leaveEditKey={leaveEditKey}
            />
          </>
        ) : null}
      </div>
    </div>
  );
}
