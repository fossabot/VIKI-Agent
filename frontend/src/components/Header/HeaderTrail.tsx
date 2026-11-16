/** The header's right-hand trail: the running-agents pill, the permission-mode chip, the search
 * button, and whatever the caller appends after them (today, the ⋯ menu) — one owner for the
 * row's single `margin-left: auto` and its order. The mode chip is Radix Dropdown wired the same
 * way Composer/ModelChip.tsx wires its own: controlled `open` plus a click guard, because Radix
 * opens a trigger on pointerdown, which a plain `click` never synthesizes (see that file's own
 * comment for why the `if (!open)` guard, not an unconditional `setOpen(true)`, is load-bearing).
 * Its dropdown reuses ModelChip.css's row classes (`.chip-menu`, `.chip-check`, `.chip-model-*`)
 * rather than redefining them, hence the direct import below. */
import * as Dropdown from "@radix-ui/react-dropdown-menu";
import { type ReactElement, type ReactNode, type Ref, useState } from "react";
import type { Key } from "../../i18n/index";
import { t } from "../../i18n/t";
import { isMac } from "../../platform";
import { Spinner } from "../Loading/Loading";
import "./HeaderTrail.css";
import "../Composer/ModelChip.css";

export type ModeChoice = "default" | "acceptEdits" | "auto" | "plan";

export interface ToolsToggle {
  open: boolean;
  panelId: string;
  onToggle: () => void;
  buttonRef: Ref<HTMLButtonElement>;
}

const MODE_ITEMS: ReadonlyArray<{
  value: ModeChoice;
  label: string;
  description: string;
  descriptionKey?: Key;
}> = [
  {
    value: "default",
    label: "Manual",
    description: "",
    descriptionKey: "settings.mode.manual.desc",
  },
  {
    value: "acceptEdits",
    label: "Accept edits",
    description: "",
    descriptionKey: "settings.mode.accept_edits.desc",
  },
  { value: "auto", label: "Auto", description: "A safety check approves routine actions" },
  { value: "plan", label: "Plan", description: "Plans first; changes nothing until you approve" },
];

export function HeaderTrail({
  running = 0,
  onShowRuns,
  mode = "",
  modeLocked = false,
  onMode,
  tools,
  children,
}: {
  running?: number;
  onShowRuns?: () => void;
  mode?: string;
  modeLocked?: boolean;
  onMode?: (mode: ModeChoice) => void;
  tools?: ToolsToggle;
  children?: ReactNode;
}): ReactElement {
  const [open, setOpen] = useState(false);
  // The CLI sometimes reports "manual" for the same choice `session.set_mode` sends as
  // "default"; folding it in here makes both the checkmark and the label fall out of one lookup.
  const current = mode === "manual" ? "default" : mode;
  const label = MODE_ITEMS.find((item) => item.value === current)?.label ?? mode;
  const findLabel = `Find, select and export (${isMac() ? "⌘F" : "Ctrl+F"})`;

  return (
    <div className="header-trail">
      {running > 0 && onShowRuns ? (
        <button
          type="button"
          className="agents-pill"
          title="Show their runs in the VIKI panel"
          onClick={onShowRuns}
        >
          <Spinner className="agents-pill-ring" />
          {running === 1 ? "1 subagent running" : `${running} subagents running`}
        </button>
      ) : null}
      {mode && onMode ? (
        <Dropdown.Root open={open} onOpenChange={setOpen}>
          <Dropdown.Trigger
            className="mode-chip"
            disabled={modeLocked}
            title={modeLocked ? "Set by the bound profile" : "Permission mode for this chat"}
            aria-label={`Mode: ${label}. Change mode`}
            onClick={() => {
              if (!open) setOpen(true);
            }}
          >
            {label}
            <span aria-hidden="true">▾</span>
          </Dropdown.Trigger>
          <Dropdown.Portal>
            <Dropdown.Content
              className="chip-menu menu-panel"
              side="bottom"
              align="end"
              sideOffset={6}
            >
              <Dropdown.Label className="lbl">Mode</Dropdown.Label>
              {MODE_ITEMS.map((item) => (
                <Dropdown.Item
                  key={item.value}
                  className={`mi${item.value === current ? " on" : ""}`}
                  onSelect={() => {
                    if (item.value !== current) onMode?.(item.value);
                  }}
                >
                  <span className="chip-check">{item.value === current ? "✓" : ""}</span>
                  <span className="chip-model-text">
                    <span className="chip-model-label">{item.label}</span>
                    <span className="d chip-model-desc">
                      {item.descriptionKey ? t(item.descriptionKey) : item.description}
                    </span>
                  </span>
                </Dropdown.Item>
              ))}
            </Dropdown.Content>
          </Dropdown.Portal>
        </Dropdown.Root>
      ) : null}
      {tools ? (
        <button
          type="button"
          className="header-tools-button"
          ref={tools.buttonRef}
          aria-label={findLabel}
          title={findLabel}
          aria-expanded={tools.open}
          aria-controls={tools.panelId}
          onClick={tools.onToggle}
        >
          <svg className="ico" width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
            <circle cx="7" cy="7" r="4.5" />
            <path d="M10.5 10.5 14 14" />
          </svg>
        </button>
      ) : null}
      {children}
    </div>
  );
}
