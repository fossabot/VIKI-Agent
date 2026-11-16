import { type ReactElement, useEffect, useId, useRef, useState } from "react";
import type { SessionMetadata } from "../../clients/conversations/sessionMetadataApi";
import { isComposingKeydown } from "../Composer/ime";
import { useModalFocus } from "../Dialog/useModalFocus";
import "./SessionRenameDialog.css";

export interface SessionRenameDialogProps {
  session: SessionMetadata;
  pending: boolean;
  errorCode: string | null;
  onSave: (title: string) => Promise<SessionMetadata | null>;
  onReload: () => void;
  onClose: () => void;
  restoreFocus?: () => HTMLElement | null;
}

export function SessionRenameDialog({
  session,
  pending,
  errorCode,
  onSave,
  onReload,
  onClose,
  restoreFocus,
}: SessionRenameDialogProps): ReactElement {
  const [title, setTitle] = useState(session.title);
  const [saving, setSaving] = useState(false);
  const [failed, setFailed] = useState(false);
  const busy = useRef(false);
  const mounted = useRef(true);
  const dialogRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const headingId = useId();
  const inputId = useId();
  useModalFocus({
    active: true,
    dialogRef,
    initialFocusRef: inputRef,
    onDismiss: onClose,
    restoreFocus,
  });
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  async function save(): Promise<void> {
    if (pending || busy.current) return;
    busy.current = true;
    setSaving(true);
    setFailed(false);
    try {
      const result = await onSave(title.trim());
      if (!mounted.current) return;
      if (result) onClose();
      else setFailed(true);
    } catch {
      if (mounted.current) setFailed(true);
    } finally {
      busy.current = false;
      if (mounted.current) setSaving(false);
    }
  }

  return (
    <div className="session-rename-backdrop">
      <div
        ref={dialogRef}
        className="session-rename-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={headingId}
      >
        <h2 id={headingId}>Rename session</h2>
        <label htmlFor={inputId}>Session name</label>
        <input
          id={inputId}
          ref={inputRef}
          value={title}
          maxLength={256}
          onChange={(event) => setTitle(event.target.value)}
          onKeyDown={(event) => {
            if (event.key !== "Enter" || isComposingKeydown(event.nativeEvent)) return;
            event.preventDefault();
            void save();
          }}
        />
        {failed ? (
          <p role="alert">
            {errorCode === "conflict"
              ? "The session changed elsewhere. Reload before saving."
              : "Could not save. Reload and check the saved state."}{" "}
            <button type="button" className="btn txt sm" onClick={onReload}>
              Reload
            </button>
          </p>
        ) : null}
        <div className="session-rename-actions">
          <button
            type="button"
            className="btn alt sm"
            disabled={pending || saving}
            onClick={() => void save()}
          >
            Save
          </button>
          <button type="button" className="btn txt sm" onClick={onClose}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
