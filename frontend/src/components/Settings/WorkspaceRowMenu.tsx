/** A workspace row's "⋯" menu (Details and trust, Open in Code, Remove from list…) and
 * RemoveConfirm, the small inline alertdialog that "Remove from list…" leads to.
 * `WorkspaceRowMenu` only reports which item was picked; a caller decides when `RemoveConfirm`
 * itself is shown and owns its `busy`/`error` state. */
import * as Dropdown from "@radix-ui/react-dropdown-menu";
import { type ReactElement, useState } from "react";
import { t, tf } from "../../i18n/t";
import { ConfirmDialog } from "../Dialog/ConfirmDialog";
import "./WorkspaceRowMenu.css";

export interface WorkspaceRowMenuProps {
  name: string;
  onDetails: () => void;
  onOpenInCode?: () => void;
  openDisabled?: boolean;
  onRemove?: () => void;
}

export function WorkspaceRowMenu({
  name,
  onDetails,
  onOpenInCode,
  openDisabled,
  onRemove,
}: WorkspaceRowMenuProps): ReactElement {
  // Radix opens the trigger on pointerdown (real pointer devices), which a plain `click`
  // (jsdom's `fireEvent.click` included) never synthesizes — the same bridge ModelChip.tsx uses.
  const [open, setOpen] = useState(false);

  return (
    <Dropdown.Root open={open} onOpenChange={setOpen}>
      <Dropdown.Trigger
        className="ws-row-menu-trigger btn txt sm"
        aria-label={tf("workspaces.row.more", { name })}
        onClick={() => {
          if (!open) setOpen(true);
        }}
      >
        <svg className="ico" width="14" height="14" viewBox="0 0 16 16" aria-hidden="true">
          <path d="M4 8h.01 M8 8h.01 M12 8h.01" />
        </svg>
      </Dropdown.Trigger>
      <Dropdown.Portal>
        <Dropdown.Content className="ws-row-menu menu-panel" align="end" sideOffset={6}>
          <Dropdown.Item className="mi" onSelect={() => onDetails()}>
            {t("workspaces.row.details")}
          </Dropdown.Item>
          {onOpenInCode ? (
            <Dropdown.Item
              className="mi"
              disabled={openDisabled ?? false}
              onSelect={() => onOpenInCode()}
            >
              {t("workspaces.row.open_code")}
            </Dropdown.Item>
          ) : null}
          {onRemove ? (
            <>
              <Dropdown.Separator className="ws-row-menu-sep" />
              <Dropdown.Item className="mi" onSelect={() => onRemove()}>
                {t("workspaces.row.remove_menu")}
              </Dropdown.Item>
            </>
          ) : null}
        </Dropdown.Content>
      </Dropdown.Portal>
    </Dropdown.Root>
  );
}

export interface RemoveConfirmProps {
  name: string;
  busy: boolean;
  error: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}

export function RemoveConfirm({
  name,
  busy,
  error,
  onConfirm,
  onCancel,
}: RemoveConfirmProps): ReactElement {
  return (
    <ConfirmDialog
      label={tf("workspaces.row.remove_label", { name })}
      body={tf("workspaces.row.remove_confirm", { name })}
      confirmLabel={t("workspaces.row.remove")}
      destructive
      busy={busy}
      error={error}
      onConfirm={onConfirm}
      onCancel={onCancel}
      className="ws-remove-confirm"
    />
  );
}
