/**
 * A rail shown as a modal drawer when it is too narrow to dock.
 *
 * It holds the same React nodes as the docked rail — the session list, agents, VIKI's header,
 * the gates, routines and tiles — so nothing is dropped because a rail is not on screen. The
 * drawer is opened only by its own top-bar toggle, one at a time, and App closes it on anything
 * that takes the user elsewhere (a row opened, forked or closed, a gate picked, a file opened,
 * search opened, or the window growing until that rail docks again).
 *
 * Rules:
 * - Closed renders nothing. `useModalFocus` is still called, unconditionally and with
 *   `active: open`, the way SearchPalette calls it — a hook may not be skipped.
 * - The panel is `role="dialog"` `aria-modal="true"`, labelled like its toggle. The hook inerts
 *   the conversation, the docked rail, the top bar and the status bar for as long as it is open,
 *   focuses Close on entry, cycles Tab and Shift+Tab inside, and returns focus to the toggle.
 * - Escape closes the drawer, except while an IME is composing (the hook's own guard) and except
 *   while a session row's menu is open inside it — that menu owns the first Escape.
 * - The panel is nested INSIDE the backdrop rather than beside it, exactly as the palette nests
 *   its own. The hook inerts every sibling of the panel on the way up the tree, and an `inert`
 *   element takes no clicks at all: a backdrop that was the panel's sibling would be dismissable
 *   by nothing. Nested, it is the panel's parent instead, and stays live.
 * - A mousedown directly on the backdrop dismisses (the palette's rule, same reason: a mousedown
 *   that starts on the panel's own chrome must not close what the user is reading).
 */
import { type MouseEvent, type ReactNode, useRef } from "react";
import { useModalFocus } from "../Dialog/useModalFocus";
import "./RailDrawer.css";

export interface RailDrawerProps {
  side: "left" | "right";
  /** The accessible name, identical to its top-bar toggle's. */
  label: string;
  open: boolean;
  onClose: () => void;
  /** Stable fallback when the toggle that opened this drawer unmounts (the window grew). */
  restoreFocus?: () => HTMLElement | null;
  children: ReactNode;
}

export function RailDrawer({
  side,
  label,
  open,
  onClose,
  restoreFocus,
  children,
}: RailDrawerProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  useModalFocus({
    active: open,
    dialogRef,
    initialFocusRef: closeRef,
    onDismiss: onClose,
    restoreFocus,
  });

  function handleBackdropMouseDown(event: MouseEvent<HTMLDivElement>): void {
    if (event.target === event.currentTarget) onClose();
  }

  if (!open) return null;

  return (
    // The backdrop is a dismiss-on-click-outside shield, not a control: the dialog is the panel
    // below, and the keyboard closes this drawer with Escape and with the Close button inside
    // it, whether or not a pointer ever touches the backdrop. Same shape as SearchPalette's.
    // biome-ignore lint/a11y/noStaticElementInteractions: see above
    <div className="rail-drawer-backdrop" onMouseDown={handleBackdropMouseDown}>
      <div
        ref={dialogRef}
        className={`rail-drawer rail-drawer--${side}`}
        data-region={`${side}-drawer`}
        role="dialog"
        aria-modal="true"
        aria-label={label}
        tabIndex={-1}
      >
        <div className="rail-drawer-head">
          <button
            type="button"
            className="btn txt sm rail-drawer-close"
            ref={closeRef}
            aria-label="Close"
            onClick={onClose}
          >
            <span aria-hidden="true">✕</span>
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}
