/**
 * The "Help" toggle in the top bar: one toggle sized to `AppMenu`'s toggle metrics, opening a
 * `role="menu"` panel of help destinations (Welcome to VIKI Agent, the tour, keyboard shortcuts,
 * what's new, troubleshooting, glossary, and a search entry point). Escape, arrow/Home/End navigation,
 * outside clicks, Tab and the IME guard all follow the `AppMenu` pattern (copied, not imported:
 * same as `AppMenu` does not depend on `TopBar.css`, this file owns its own chrome rather than
 * importing `AppMenu.css`). Every row is always enabled, so unlike `AppMenu` there is no
 * `aria-disabled` state to track.
 */
import { type ReactElement, useEffect, useRef, useState } from "react";
import { t } from "../../i18n/t";
import { type HelpTarget, openHelp } from "../Help/helpStore";
import "./HelpMenu.css";

interface HelpMenuRow {
  id: string;
  label: string;
  /** Absent only on the setup guide row, which calls `onSetupGuide` instead. */
  target?: HelpTarget;
  /** Shown as a `.kbd` badge at the row's end, e.g. the `?` next to "Keyboard shortcuts". */
  shortcut?: string;
}

const ROWS: HelpMenuRow[] = [
  { id: "welcome", label: "Welcome to VIKI Agent", target: { kind: "topic", id: "welcome" } },
  { id: "tour", label: "Take the tour", target: { kind: "tour" } },
  {
    id: "keyboard-shortcuts",
    label: "Keyboard shortcuts",
    target: { kind: "topic", id: "keyboard-shortcuts" },
    shortcut: "?",
  },
  { id: "whats-new", label: "What's new", target: { kind: "whats-new" } },
  {
    id: "troubleshooting",
    label: "Troubleshooting",
    target: { kind: "topic", id: "troubleshooting" },
  },
  { id: "glossary", label: "Glossary", target: { kind: "topic", id: "glossary" } },
];

/** Separated from `ROWS` by the menu's one `<hr>`. */
const SEARCH_ROW: HelpMenuRow = {
  id: "search",
  label: "Ask about VIKI Agent",
  target: { kind: "search", query: "" },
};

const SETUP_GUIDE_ID = "setup-guide";

export function HelpMenu({ onSetupGuide }: { onSetupGuide?: () => void } = {}): ReactElement {
  // The setup guide row follows "Welcome to VIKI Agent" and opens no help target.
  const rows: HelpMenuRow[] =
    onSetupGuide === undefined
      ? ROWS
      : [
          ...ROWS.slice(0, 1),
          { id: SETUP_GUIDE_ID, label: t("onboarding.guide.menu") },
          ...ROWS.slice(1),
        ];
  const allRows = [...rows, SEARCH_ROW];
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const itemRefs = useRef<(HTMLButtonElement | null)[]>([]);

  useEffect(() => {
    if (!open) return;
    function onWindowKeyDown(event: globalThis.KeyboardEvent): void {
      if (event.isComposing || event.keyCode === 229) return;
      if (event.key === "Escape") {
        event.stopImmediatePropagation();
        setOpen(false);
        toggleRef.current?.focus();
        return;
      }
      if (!wrapRef.current?.contains(event.target as Node)) return;
      if (event.key === "Tab") {
        setOpen(false);
        return;
      }
      const items = itemRefs.current.filter(
        (el): el is HTMLButtonElement => el?.isConnected === true,
      );
      if (items.length === 0) return;
      const current = items.indexOf(document.activeElement as HTMLButtonElement);
      if (event.key === "ArrowDown") {
        event.preventDefault();
        items[current < 0 ? 0 : (current + 1) % items.length]?.focus();
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        // The toggle enters at the last row; ArrowDown enters at the first.
        items[
          current < 0 ? items.length - 1 : (current - 1 + items.length) % items.length
        ]?.focus();
      } else if (event.key === "Home") {
        event.preventDefault();
        items[0]?.focus();
      } else if (event.key === "End") {
        event.preventDefault();
        items[items.length - 1]?.focus();
      }
    }
    function onOutside(event: MouseEvent | FocusEvent): void {
      // Outside clicks and focus moves already have a destination; Escape returns to the toggle.
      if (wrapRef.current && !wrapRef.current.contains(event.target as Node)) setOpen(false);
    }
    window.addEventListener("keydown", onWindowKeyDown);
    window.addEventListener("mousedown", onOutside);
    window.addEventListener("focusin", onOutside);
    itemRefs.current[0]?.focus();
    return () => {
      window.removeEventListener("keydown", onWindowKeyDown);
      window.removeEventListener("mousedown", onOutside);
      window.removeEventListener("focusin", onOutside);
    };
  }, [open]);

  return (
    <div className="help-menu-wrap" ref={wrapRef}>
      <button
        type="button"
        ref={toggleRef}
        className="help-menu-toggle"
        aria-label="Help"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((shown) => !shown)}
      >
        ?
      </button>
      {open ? (
        <div className="help-menu-panel menu-panel" role="menu" aria-label="Help">
          {allRows.map((row, index) => (
            <div key={row.id} className="help-menu-row-wrap">
              {index === rows.length ? <hr className="help-menu-separator" /> : null}
              <button
                type="button"
                role="menuitem"
                className="mi"
                ref={(el) => {
                  itemRefs.current[index] = el;
                }}
                onClick={() => {
                  setOpen(false);
                  // Same rule as Escape: park focus on the toggle before the panel unmounts, or
                  // the clicked row goes with it and focus falls to <body>.
                  toggleRef.current?.focus();
                  if (row.target === undefined) onSetupGuide?.();
                  else openHelp(row.target);
                }}
              >
                <span className="help-menu-row-label">{row.label}</span>
                {row.shortcut !== undefined ? <span className="kbd">{row.shortcut}</span> : null}
              </button>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
