/**
 * The user's controls for one skill proposal: approve it, edit its text, reject it with an
 * optional reason, or withdraw it. A dumb view over the four actions — the caller performs them
 * and reports back the proposal's `state`/`text` (unchanged, on failure). Either one changing
 * always closes whichever mode is open: an unsaved draft or a pending confirmation was about the
 * proposal as it stood before, not the one now in front of the user.
 *
 * Two more hooks exist only for an edit draft the user is still typing: `onDirty` reports
 * whether the draft differs from `text`, and `leaveEditKey` lets the caller step out of edit
 * mode from outside (Escape) without discarding the draft — only Cancel does that.
 */
import { type ReactElement, useEffect, useState } from "react";
import type { SkillState } from "../../state/skillProposalsApi";
import "./DecisionInbox.css";

export interface SkillProposalActionsProps {
  state: SkillState;
  text: string;
  enabled: boolean;
  busy: boolean;
  onApprove: () => void;
  onEdit: (text: string) => void;
  onReject: (reason: string | null) => void;
  onWithdraw: () => void;
  /** Fires whenever the edit draft starts or stops differing from `text`. */
  onDirty: (dirty: boolean) => void;
  /** Bumped by the caller to leave edit mode (keeping the draft) without using Cancel; a no-op
   * outside edit mode. */
  leaveEditKey: number;
}

type Mode = "idle" | "edit" | "reject" | "withdraw";

// Approve additionally needs one of these states; Edit and Reject instead rule a few states
// out, below.
const APPROVABLE: ReadonlySet<SkillState> = new Set(["open", "handed"]);

export function SkillProposalActions({
  state,
  text,
  enabled,
  busy,
  onApprove,
  onEdit,
  onReject,
  onWithdraw,
  onDirty,
  leaveEditKey,
}: SkillProposalActionsProps): ReactElement {
  const [mode, setMode] = useState<Mode>("idle");
  const [draft, setDraft] = useState(text);
  const [reason, setReason] = useState("");
  // A changed proposal is not the one any open draft or confirmation was about: adjust state
  // during this render (react.dev's pattern for this) rather than flash the stale mode for a
  // frame in an effect. The draft itself resets too: it was about the old text, not this one.
  const [prevKey, setPrevKey] = useState<[string, SkillState]>([text, state]);
  if (prevKey[0] !== text || prevKey[1] !== state) {
    setPrevKey([text, state]);
    setMode("idle");
    setDraft(text);
  }
  // The caller (Escape, while dirty) bumps this to leave edit mode without discarding the draft,
  // unlike the reset above: same render-time pattern, but draft is left untouched.
  const [prevLeaveKey, setPrevLeaveKey] = useState(leaveEditKey);
  if (prevLeaveKey !== leaveEditKey) {
    setPrevLeaveKey(leaveEditKey);
    if (mode === "edit") setMode("idle");
  }

  const act = enabled && !busy;
  const dirty = draft !== text;
  useEffect(() => {
    onDirty(dirty);
  }, [dirty, onDirty]);

  if (mode === "edit")
    return (
      <>
        <label>
          SKILL.md
          <textarea
            className="mono"
            rows={18}
            spellCheck={false}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
          />
        </label>
        <div className="decisions-batch">
          <button
            type="button"
            className="btn alt sm"
            disabled={busy || draft === text || draft.trim() === ""}
            onClick={() => onEdit(draft)}
          >
            Save changes
          </button>
          <button
            type="button"
            className="btn alt sm"
            onClick={() => {
              setDraft(text);
              setMode("idle");
            }}
          >
            Cancel
          </button>
        </div>
      </>
    );

  if (mode === "reject")
    return (
      <>
        <label>
          Reason (optional)
          <input
            maxLength={200}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
        </label>
        <div className="decisions-batch">
          <button
            type="button"
            className="btn alt sm"
            onClick={() => {
              const trimmed = reason.trim();
              onReject(trimmed === "" ? null : trimmed);
            }}
          >
            Reject
          </button>
          <button type="button" className="btn alt sm" onClick={() => setMode("idle")}>
            Cancel
          </button>
        </div>
      </>
    );

  if (mode === "withdraw")
    return (
      <>
        <p className="decision-inbox-note">
          Withdraw this proposal? The skill file does not change.
        </p>
        <div className="decisions-batch">
          <button type="button" className="btn alt sm" disabled={busy} onClick={onWithdraw}>
            Withdraw
          </button>
          <button
            type="button"
            className="btn alt sm"
            disabled={busy}
            onClick={() => setMode("idle")}
          >
            Keep it
          </button>
        </div>
      </>
    );

  return (
    <div className="decisions-batch">
      <button
        type="button"
        className="btn alt sm"
        disabled={!act || !APPROVABLE.has(state)}
        onClick={onApprove}
      >
        Approve
      </button>
      <button
        type="button"
        className="btn alt sm"
        disabled={!act || state === "applied"}
        onClick={() => setMode("edit")}
      >
        Edit
      </button>
      <button
        type="button"
        className="btn alt sm"
        disabled={!act || state === "applied" || state === "rejected"}
        onClick={() => {
          setReason("");
          setMode("reject");
        }}
      >
        Reject
      </button>
      <button
        type="button"
        className="btn alt sm"
        disabled={!act}
        onClick={() => setMode("withdraw")}
      >
        Withdraw
      </button>
    </div>
  );
}
