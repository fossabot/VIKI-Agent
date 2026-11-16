/**
 * The gear menu that reaches every non-chat view: one 32px toggle in
 * `.topbar-right` before New session, a `role="menu"` panel below 800px it is the only way into
 * the views (the tabs are hidden there). Escape and the IME guard follow the `HeaderMenu`
 * pattern (copied, not imported: this menu's rows, disabled state and arrow-key movement are all
 * new). A disabled row is a focusable `aria-disabled` button, never hidden, never `disabled`.
 */
import { type MutableRefObject, type ReactElement, useEffect, useRef, useState } from "react";
import { type CommandId, commandFor, shortcutLabel, type View } from "../../commands";
import { t } from "../../i18n/t";
import type { ShellSnapshot } from "../../state/shell";
import "./AppMenu.css";

export interface AppMenuProps {
  snapshot: ShellSnapshot;
  view: View;
  onCommand: (id: CommandId) => void;
}

const ROWS: CommandId[] = [
  "settings.open",
  "workspace.select",
  "diagnostics.open",
  "data.open",
  "data.export_diagnostics",
  "data.import_settings",
  "data.reset",
];

const SEARCH_ROWS: CommandId[] = ["search.conversations", "search.vault"];

export function AppMenu({ snapshot, view, onCommand }: AppMenuProps): ReactElement {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const itemRefs = useRef<(HTMLButtonElement | null)[]>([]);

  const providerSetup = commandFor("provider.setup", snapshot, view);
  const ids = [
    ...ROWS,
    ...SEARCH_ROWS,
    ...(providerSetup.enabled ? (["provider.setup"] as const) : []),
  ];

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
        (el): el is HTMLButtonElement =>
          el?.isConnected === true && window.getComputedStyle(el).display !== "none",
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
    <div className="app-menu-wrap" ref={wrapRef}>
      <button
        type="button"
        ref={toggleRef}
        className="app-menu-toggle"
        aria-label={t("shell.menu.label")}
        data-region="app-menu"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((shown) => !shown)}
      >
        <svg className="ico" width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
          <circle cx="8" cy="8" r="2" />
          <path d="M6.4 1.5h3.2l.5 2a5 5 0 0 1 1 .6l2-.6 1.6 2.8-1.5 1.4a5 5 0 0 1 0 1.2l1.5 1.4-1.6 2.8-2-.6a5 5 0 0 1-1 .6l-.5 2H6.4l-.5-2a5 5 0 0 1-1-.6l-2 .6-1.6-2.8 1.5-1.4a5 5 0 0 1 0-1.2L1.3 6.3l1.6-2.8 2 .6a5 5 0 0 1 1-.6l.5-2Z" />
        </svg>
      </button>
      {open ? (
        <div className="app-menu-panel menu-panel" role="menu" aria-label={t("shell.menu.label")}>
          {ids.map((id, index) => (
            <div key={id} className="app-menu-row-wrap">
              {index === ROWS.length ? <hr className="app-menu-separator" /> : null}
              <AppMenuRow
                id={id}
                index={index}
                snapshot={snapshot}
                view={view}
                itemRefs={itemRefs}
                onSelect={(commandId) => {
                  setOpen(false);
                  // Same rule as Escape (line 52): park focus on the toggle before the panel
                  // unmounts, or the clicked row goes with it and focus falls to <body>. A
                  // command that does mount a new view still wins its own focus, since that
                  // effect runs after this commit.
                  toggleRef.current?.focus();
                  onCommand(commandId);
                }}
              />
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

interface AppMenuRowProps {
  id: CommandId;
  index: number;
  snapshot: ShellSnapshot;
  view: View;
  itemRefs: MutableRefObject<(HTMLButtonElement | null)[]>;
  onSelect: (id: CommandId) => void;
}

function AppMenuRow({
  id,
  index,
  snapshot,
  view,
  itemRefs,
  onSelect,
}: AppMenuRowProps): ReactElement {
  const state = commandFor(id, snapshot, view);
  const reasonId = `app-menu-reason-${id}`;
  const shortcut = shortcutLabel(id);
  return (
    <button
      type="button"
      role="menuitem"
      className="mi"
      ref={(el) => {
        itemRefs.current[index] = el;
      }}
      aria-disabled={!state.enabled}
      {...(state.reason !== null ? { "aria-describedby": reasonId, title: t(state.reason) } : {})}
      onClick={() => {
        if (!state.enabled) return;
        onSelect(id);
      }}
    >
      <span className="app-menu-row-label">{t(`cmd.${id}`)}</span>
      {shortcut !== undefined ? <span className="kbd">{shortcut}</span> : null}
      {state.reason !== null ? (
        <span id={reasonId} className="sr-only">
          {t(state.reason)}
        </span>
      ) : null}
    </button>
  );
}
