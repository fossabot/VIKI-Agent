/**
 * What a settings window is shown when the document moved under it: per key the
 * user just changed, the value they entered and the value that is current now, with its source.
 *
 * The panel never saves and never overwrites. `stale_revision`'s body carries the current
 * document, but the store deliberately does not commit it — only "Load the current values and
 * re-enter" does, and the controls keep the user's typed values so they can save again against the new
 * revision with a fresh `command_id`. `command_conflict` has no document to show, so the panel
 * is the sentence and the reload alone.
 *
 * Every value here comes off the wire, so every one of them is a text node; a value with no
 * catalog row is shown as its raw token in `mono` rather than interpreted.
 */

import { type ReactElement, useRef } from "react";
import type { ShellApiError } from "../../clients/app/shellApi";
import { errorText, t } from "../../i18n/t";
import { useModalFocus } from "../Dialog/useModalFocus";
import { keyLabel, valueLabel } from "./pages";

export interface ReviewPanelProps {
  /** The 409 that opened the panel: `stale_revision` (with `current`) or `command_conflict`. */
  error: ShellApiError;
  /** The changes the refused patch carried, keyed as they were sent. */
  changes: Readonly<Record<string, unknown>>;
  /** Adopt the current document (or re-read it when the body carried none) and close. */
  onReload: () => void;
  onClose: () => void;
}

function Value({ settingKey, value }: { settingKey: string; value: unknown }): ReactElement {
  if (value === null || value === undefined || value === "")
    return <span className="settings-review-value">{t("settings.value.empty")}</span>;
  const label = valueLabel(settingKey, value);
  return label === null ? (
    <span className="settings-review-value mono">{String(value)}</span>
  ) : (
    <span className="settings-review-value">{label}</span>
  );
}

export function ReviewPanel({ error, changes, onReload, onClose }: ReviewPanelProps): ReactElement {
  const dialogRef = useRef<HTMLDivElement>(null);
  const reloadRef = useRef<HTMLButtonElement>(null);
  const current = error.current;

  useModalFocus({
    active: true,
    dialogRef,
    initialFocusRef: reloadRef,
    onDismiss: onClose,
  });

  return (
    <div
      ref={dialogRef}
      className="settings-review"
      role="dialog"
      aria-modal="true"
      aria-labelledby="settings-review-title"
      tabIndex={-1}
    >
      <h2 id="settings-review-title">{t("settings.review.title")}</h2>
      <p className="settings-review-reason">{errorText(error)}</p>
      {current !== null && (
        <ul className="settings-review-rows">
          {Object.keys(changes).map((key) => (
            <li key={key} data-setting-key={key}>
              <span className="settings-review-key">{keyLabel(key)}</span>
              <span className="settings-review-cell">
                <span className="lbl">{t("settings.review.you_entered")}</span>
                <Value settingKey={key} value={changes[key]} />
              </span>
              <span className="settings-review-cell">
                <span className="lbl">{t("settings.review.current")}</span>
                <Value settingKey={key} value={current.effective[key]} />
                <span className="chip dim">
                  {t(`common.source.${current.sources[key] ?? "default"}`)}
                </span>
              </span>
            </li>
          ))}
        </ul>
      )}
      <div className="settings-review-actions">
        <button type="button" className="btn go sm" ref={reloadRef} onClick={onReload}>
          {t("settings.review.reload_and_reenter")}
        </button>
        {error.code === "stale_revision" && (
          <button type="button" className="btn txt sm" onClick={onClose}>
            {t("common.button.close")}
          </button>
        )}
      </div>
    </div>
  );
}
