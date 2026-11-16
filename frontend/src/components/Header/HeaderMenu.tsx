/**
 * The conversation header's `⋯` menu, whose only item is Wrap up (log + commit).
 *
 * DESIGN NOTE: the design does not draw this menu; only the banner that says another VIKI is live
 * refers to it. Nothing here is invented at full size: the panel and its row are the existing
 * menu primitives (`.menu`-shaped chrome and `.mi`), the wording is wrapUp.ts's, which that
 * banner's design quotes verbatim, and there is exactly one item. If the menu ever grows a
 * second, that is a design question, not a component question.
 *
 * Rules:
 * - A `⋯` button in the header (aria-label "Session actions", aria-expanded), then a panel
 *   below and right-aligned to it.
 * - One row: "Wrap up (log + commit)". When wrapUpState(...).enabled is false the row is
 *   disabled and carries the tooltip naming the sessions that must finish first — a refusal
 *   that says what to do is worth more than one that says no.
 * - Picking it calls onWrapUp(), which sends WRAP_UP_PROMPT as an ordinary turn. The app never
 *   writes the vault or runs git itself; claude does, under the vault's own hooks.
 * - Escape closes it, with the same IME guard every other window listener in this app uses.
 * - The panel chrome is `.header-menu`'s own rather than a shared `.menu`, for the reason
 *   RowMenu.tsx records: SlashMenu.css already declares a bare `.menu`.
 * - Session titles reach the tooltip from transcripts, so it is set as a `title` attribute
 *   (text), never as markup.
 */
import { type ReactElement, useEffect, useState } from "react";
import type { OtherSession } from "../../protocol";
import { wrapUpState } from "./wrapUp";
import "./HeaderMenu.css";

export interface HeaderMenuProps {
  /** vault.sessions: the live claude sessions in the vault that this app does not own. */
  others: readonly OtherSession[];
  /** Titles of this app's OTHER live sessions. `others` cannot contain them — the server builds
   * that list by subtracting its own processes — and they hold the vault exactly as hard. */
  ownLive?: readonly string[];
  onWrapUp: () => void;
}

export function HeaderMenu({ others, ownLive = [], onWrapUp }: HeaderMenuProps): ReactElement {
  const [open, setOpen] = useState(false);
  const { enabled, tooltip } = wrapUpState(others, ownLive);

  useEffect(() => {
    if (!open) return;
    function onWindowKeyDown(event: globalThis.KeyboardEvent): void {
      if (event.isComposing || event.keyCode === 229) return;
      if (event.key === "Escape") setOpen(false);
    }
    window.addEventListener("keydown", onWindowKeyDown);
    return () => window.removeEventListener("keydown", onWindowKeyDown);
  }, [open]);

  return (
    <div className="header-menu-wrap">
      <button
        type="button"
        className="header-menu-button"
        aria-label="Session actions"
        aria-expanded={open}
        onClick={() => setOpen((shown) => !shown)}
      >
        ⋯
      </button>
      {open ? (
        <div className="header-menu menu-panel" role="menu">
          <button
            type="button"
            className="mi jp"
            role="menuitem"
            disabled={!enabled}
            title={tooltip}
            onClick={() => {
              setOpen(false);
              onWrapUp();
            }}
          >
            Wrap up (log + commit)
          </button>
        </div>
      ) : null}
    </div>
  );
}
