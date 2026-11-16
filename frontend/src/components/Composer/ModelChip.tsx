/** Shows the model and effort reported by the active session and lets the user request a change. Effort changes wait until the session is idle; CLI labels and descriptions render as text. */
import * as Dropdown from "@radix-ui/react-dropdown-menu";
import { type ReactElement, useState } from "react";
import type { ModelOption } from "../../protocol";
import type { ChipNote } from "./modelChoice";
import "./ModelChip.css";

export const EFFORTS = ["low", "medium", "high", "xhigh", "max"] as const;

export interface ModelChipProps {
  models: readonly ModelOption[];
  model: string;
  effort: string;
  running: boolean;
  onModel: (value: string) => void;
  onEffort: (value: string) => void;
  /** The last pick's own pending/applied/failed state (useModelChoice, modelChoice.ts); omit it,
   * or pass null, and nothing renders beside the trigger. */
  note?: ChipNote | null;
  disabledReason?: string | undefined;
}

export function ModelChip({
  models,
  model,
  effort,
  running,
  onModel,
  onEffort,
  note,
  disabledReason,
}: ModelChipProps): ReactElement {
  // Before initialization, a custom model has no menu label yet. Show its native ID while
  // keeping the saved connection's opaque identity out of the label.
  const current = models.find((m) => m.value === model);
  const fallback = model.startsWith("viki-llm/") ? model.split("/").slice(2).join("/") : model;
  const label = current?.label && current.label !== model ? current.label : fallback;

  // Radix opens the trigger on pointerdown (real pointer devices), which a plain `click` never
  // synthesizes; controlled `open` plus this handler is the documented way to also open it on a
  // bare click. Guarding with `if (!open)` rather than an unconditional
  // `setOpen(true)` makes the real-browser case explicit rather than accidental: a real click
  // that *closes* an open menu still fires a click event afterwards, and only Radix's modal
  // dismissable layer setting `pointer-events: none` on the rest of the page while open — not
  // this component — keeps that trailing click from landing on the trigger and reopening what it
  // just closed. If the menu is ever made non-modal, this guard is the one line standing between
  // that and a click that can no longer close its own menu.
  const [open, setOpen] = useState(false);

  return (
    <>
      <Dropdown.Root open={open} onOpenChange={setOpen}>
        <Dropdown.Trigger
          className="model-chip"
          disabled={running || disabledReason !== undefined}
          title={
            disabledReason ??
            (running ? "Available when this reply finishes" : `${label} · ${effort}\n${model}`)
          }
          onClick={() => {
            if (!open) setOpen(true);
          }}
        >
          {label} · {effort} ▾
        </Dropdown.Trigger>
        <Dropdown.Portal>
          <Dropdown.Content className="chip-menu menu-panel" side="top" align="end" sideOffset={8}>
            <Dropdown.Label className="lbl">Model</Dropdown.Label>
            {models.map((m) => (
              <Dropdown.Item
                key={m.value}
                className={`mi${m.value === model ? " on" : ""}`}
                title={m.value}
                onSelect={() => onModel(m.value)}
              >
                <span className="chip-check">{m.value === model ? "✓" : ""}</span>
                <span className="chip-model-text">
                  <span className="chip-model-label">{m.label}</span>
                  <span className="d chip-model-desc">{m.description}</span>
                </span>
              </Dropdown.Item>
            ))}
            <Dropdown.Label className="lbl chip-effort-lbl">
              <span>Effort</span>
              <span className="chip-effort-note">from your next message</span>
            </Dropdown.Label>
            <div className="chip-effort-row">
              {EFFORTS.map((e) => (
                <Dropdown.Item
                  key={e}
                  className={`chip-effort${e === effort ? " on" : ""}`}
                  onSelect={() => onEffort(e)}
                >
                  {e}
                </Dropdown.Item>
              ))}
            </div>
          </Dropdown.Content>
        </Dropdown.Portal>
      </Dropdown.Root>
      {note ? (
        <span className="chip-note" role="status" data-kind={note.kind}>
          {note.text}
        </span>
      ) : null}
    </>
  );
}
