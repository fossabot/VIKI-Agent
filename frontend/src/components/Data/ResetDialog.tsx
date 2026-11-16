/**
 * The typed confirmation `ResetPanel` opens before any reset call:
 * repeats exactly the sentences the user is about to run, states what is never touched, and
 * keeps Reset an `aria-disabled` no-op until "RESET" is typed verbatim (a
 * refused control stays focusable, never the `disabled` attribute).
 */
import { type ReactElement, useRef, useState } from "react";
import { t } from "../../i18n/t";
import { useModalFocus } from "../Dialog/useModalFocus";
import { Loading } from "../Loading/Loading";
import "./Data.css";

export interface ResetDialogProps {
  /** One sentence per category the user checked, already resolved to text by the caller. */
  sentences: readonly string[];
  onConfirm: () => void;
  onCancel: () => void;
  busy: boolean;
}

const CONFIRMATION = "RESET";

export function ResetDialog({
  sentences,
  onConfirm,
  onCancel,
  busy,
}: ResetDialogProps): ReactElement {
  const dialogRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [typed, setTyped] = useState("");

  useModalFocus({ active: true, dialogRef, initialFocusRef: inputRef, onDismiss: onCancel });

  const ready = typed === CONFIRMATION && !busy;

  return (
    <div
      ref={dialogRef}
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="reset-dialog-title"
      tabIndex={-1}
      className="reset-dialog"
    >
      <h2 id="reset-dialog-title">{t("data.reset.dialog.confirm")}</h2>
      <ul>
        {sentences.map((sentence) => (
          <li key={sentence}>{sentence}</li>
        ))}
      </ul>
      <p>{t("data.reset.dialog.note")}</p>
      <label id="reset-confirm-label" htmlFor="reset-confirm-input">
        {t("data.reset.dialog.type_reset")}
      </label>
      <input
        id="reset-confirm-input"
        ref={inputRef}
        value={typed}
        onChange={(e) => setTyped(e.target.value)}
        autoComplete="off"
        spellCheck={false}
      />
      {busy && <Loading id="reset-pending-status">{t("common.state.loading")}</Loading>}
      <div className="reset-dialog-actions">
        <button
          type="button"
          className="btn alt"
          aria-disabled={busy}
          aria-describedby={busy ? "reset-pending-status" : undefined}
          onClick={onCancel}
        >
          {t("common.button.cancel")}
        </button>
        <button
          type="button"
          className="btn go"
          aria-disabled={!ready}
          aria-busy={busy}
          {...(!ready
            ? {
                "aria-describedby": busy ? "reset-pending-status" : "reset-confirm-label",
                title: t(busy ? "common.state.loading" : "data.reset.dialog.type_reset"),
              }
            : {})}
          onClick={() => {
            if (ready) onConfirm();
          }}
        >
          {t("data.reset.dialog.confirm")}
        </button>
      </div>
    </div>
  );
}
