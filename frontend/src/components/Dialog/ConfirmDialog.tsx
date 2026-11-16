/** A small inline alertdialog that asks to confirm one action. Cancel takes the initial focus and
 * the card scrolls into view when it opens; the caller owns `busy` and `error`. Bodies render as
 * text nodes.
 *
 * The card can open under a pointer that is still clicking what opened it: the second click of a
 * double-click on a Delete button lands where Confirm has just been drawn. A press or a click the
 * system counts as part of a multi-click is therefore dropped across the action row
 * (`dropMultiClick`). That covers a multi-click and nothing else: a single click that comes a
 * little later is a click like any other, and holding every click for a moment after the card
 * opens would turn away a user who confirms at once. */
import { type MouseEvent, type ReactElement, useId, useLayoutEffect, useRef } from "react";
import { t } from "../../i18n/t";
import { Icon } from "../Icon/Icon";
import "./ConfirmDialog.css";
import { useModalFocus } from "./useModalFocus";

/** `detail` counts the clicks of a multi-click: 1 for a single click, 2 for the second click of a
 * double-click wherever the first one landed, 0 for a keyboard activation. Anything past 1
 * started before this click, possibly before the card was there, so it neither answers nor moves
 * focus off Cancel. Cancel drops it too: the card would close on the click that opened it,
 * before it was read. */
function dropMultiClick(event: MouseEvent): void {
  if (event.detail <= 1) return;
  event.preventDefault();
  event.stopPropagation();
}

export interface ConfirmSection {
  heading: string;
  items: readonly string[];
}

export interface ConfirmDialogProps {
  label: string;
  body: string;
  sections?: readonly ConfirmSection[] | undefined;
  confirmLabel: string;
  destructive?: boolean | undefined;
  irreversible?: boolean | undefined;
  busy?: boolean | undefined;
  error?: string | null | undefined;
  onConfirm: () => void;
  onCancel: () => void;
  className?: string | undefined;
}

export function ConfirmDialog({
  label,
  body,
  sections,
  confirmLabel,
  destructive,
  irreversible,
  busy,
  error,
  onConfirm,
  onCancel,
  className,
}: ConfirmDialogProps): ReactElement {
  const dialogRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const actionsRef = useRef<HTMLDivElement>(null);
  const bodyId = useId();
  const sectionsId = useId();
  const shownSections = sections?.filter((section) => section.items.length > 0);
  useModalFocus({ active: true, dialogRef, initialFocusRef: cancelRef, onDismiss: onCancel });
  // The card sits in the page's flow, wherever its host mounts it, and useModalFocus focuses
  // Cancel without scrolling while it makes the rest of the window inert. Mounted outside the
  // visible part of its scroller, the card would leave a window that shows no change and answers
  // no click, so it reveals itself. The actions follow because "nearest" aligns the top of a card
  // that, with its scroll margins, is taller than its scroller, which can leave the focused
  // Cancel below the fold. jsdom has no scrollIntoView, hence the optional calls.
  useLayoutEffect(() => {
    dialogRef.current?.scrollIntoView?.({ block: "nearest" });
    actionsRef.current?.scrollIntoView?.({ block: "nearest" });
  }, []);

  return (
    <div
      ref={dialogRef}
      role="alertdialog"
      aria-modal="true"
      aria-label={label}
      aria-describedby={
        shownSections?.length
          ? [bodyId, ...shownSections.map((_, index) => `${sectionsId}-${index}`)].join(" ")
          : undefined
      }
      tabIndex={-1}
      className={className ? `confirm-dialog ${className}` : "confirm-dialog"}
    >
      <p id={shownSections?.length ? bodyId : undefined}>{body}</p>
      {shownSections?.map((section, index) => (
        <div key={section.heading}>
          <p className="confirm-dialog-heading">{section.heading}</p>
          <ul className="confirm-dialog-list" id={`${sectionsId}-${index}`}>
            {section.items.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      ))}
      {irreversible ? (
        <p className="confirm-dialog-irreversible">{t("common.confirm.irreversible")}</p>
      ) : null}
      {error ? (
        <p className="settings-error" role="alert">
          {error}
        </p>
      ) : null}
      <div
        className="confirm-dialog-actions"
        ref={actionsRef}
        onMouseDownCapture={dropMultiClick}
        onClickCapture={dropMultiClick}
      >
        <button
          type="button"
          className="btn go sm"
          aria-disabled={busy ?? false}
          onClick={() => {
            if (!busy) onConfirm();
          }}
        >
          {destructive ? <Icon name="trash" size={14} /> : null}
          {confirmLabel}
        </button>
        <button type="button" className="btn alt sm" ref={cancelRef} onClick={onCancel}>
          {t("common.button.cancel")}
        </button>
      </div>
    </div>
  );
}
