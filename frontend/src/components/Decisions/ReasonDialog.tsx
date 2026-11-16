/**
 * A nested "why" prompt for a reject or an edit-with-note: one optional one-line
 * note, submitted by its own confirm button or by Enter. It opens while the Approvals dialog (or
 * a standalone section under test) is already open, so it nests through `useModalFocus`'s own
 * stack rather than replacing whatever is behind it — the same nesting ProposalInspector already
 * relies on, which is why this reuses its `DecisionInbox.css` classes rather than inventing a
 * second set.
 */
import { type KeyboardEvent, type ReactElement, useId, useRef, useState } from "react";
import { isComposingKeydown } from "../Composer/ime";
import { useModalFocus } from "../Dialog/useModalFocus";
import { NOTE_MAX } from "./approvalKinds";
import "./DecisionInbox.css";

export interface ReasonDialogProps {
  /** The item this dialog is about, shown as the heading's text node. */
  title: string;
  noteLabel: string;
  confirmLabel: string;
  onConfirm: (note: string) => void;
  onCancel: () => void;
}

export function ReasonDialog({
  title,
  noteLabel,
  confirmLabel,
  onConfirm,
  onCancel,
}: ReasonDialogProps): ReactElement {
  const [note, setNote] = useState("");
  const dialogRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const titleId = useId();
  const inputId = useId();

  useModalFocus({ active: true, dialogRef, initialFocusRef: inputRef, onDismiss: onCancel });

  function submit(): void {
    onConfirm(note.trim().replace(/\s+/g, " "));
  }

  function onInputKeyDown(event: KeyboardEvent<HTMLInputElement>): void {
    if (event.key !== "Enter" || isComposingKeydown(event.nativeEvent)) return;
    event.preventDefault();
    submit();
  }

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
          <h2 id={titleId} className="decision-inbox-title jp">
            {title}
          </h2>
        </div>
        <label htmlFor={inputId}>{noteLabel}</label>
        <input
          id={inputId}
          ref={inputRef}
          type="text"
          maxLength={NOTE_MAX}
          value={note}
          onChange={(event) => setNote(event.target.value)}
          onKeyDown={onInputKeyDown}
        />
        <div className="decisions-batch">
          <button type="button" className="btn alt sm" onClick={submit}>
            {confirmLabel}
          </button>
          <button type="button" className="btn alt sm" onClick={onCancel}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
