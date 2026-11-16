/**
 * The "+N folder" chip beside a chat pane's other header controls (mode chip, search button):
 * ADR-050's extra folders a chat can also work in (`SessionStartedData.additional_folders`),
 * plus the Bot code copy it works in, and the vault it starts in, with a shortcut into Settings →
 * Workspaces. Renders nothing when there are no extra folders and no code copy — the vault alone
 * is not something this chip announces.
 */
import * as Dropdown from "@radix-ui/react-dropdown-menu";
import { type ReactElement, useId, useState } from "react";
import { t, tf } from "../../i18n/t";
import type { CodeCopy } from "../../protocol";
import "./ChatFoldersChip.css";

/** The last "/" or "\\"-separated segment, for a folder with no registered name — the same
 * reading as Settings/AddFolders.tsx's own `lastSegment`, copied rather than imported: that file
 * does not export it. */
function lastSegment(path: string): string {
  const cut = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
  return cut === -1 ? path : path.slice(cut + 1);
}

export interface ChatFoldersChipProps {
  /** This chat's extra folders (`SessionStartedData.additional_folders`), as full paths. */
  folders: readonly string[];
  /** The vault this chat starts in; `null` when there is no registered active vault. */
  vault: { name: string; root: string } | null;
  /** A registered workspace's root, resolved to its display name. */
  names: ReadonlyMap<string, string>;
  /** The Bot code copy this chat works in (`SessionStartedData.code_copy`), when it has one. */
  codeCopy?: CodeCopy | undefined;
  onManage: () => void;
}

export function ChatFoldersChip({
  folders,
  vault,
  names,
  codeCopy,
  onManage,
}: ChatFoldersChipProps): ReactElement | null {
  // Radix opens the trigger on pointerdown, which a plain click never synthesizes — the same
  // bridge ModelChip.tsx/HeaderTrail.tsx use.
  const [open, setOpen] = useState(false);
  const descId = useId();
  const count = folders.length + (codeCopy ? 1 : 0);
  if (count === 0) return null;

  const vaultPart = vault !== null ? `${vault.name} ${t("shell.chat_folders.starts_here")}` : null;
  // The trigger's own accessible description: a screen reader landing inside the open menu
  // hears only its one real menuitem ("Manage workspaces in Settings") — the vault and folder
  // rows below are plain info `div`s that a `role="menu"` does not expose — so the trigger states
  // the same list up front instead. Same list-join technique as ReadAloudVoicesField.tsx's
  // missing-voice sentence.
  const copyPart = codeCopy
    ? tf("shell.chat_folders.code_copy", { folder: codeCopy.folder, branch: codeCopy.branch })
    : null;
  const folderNames = folders.map((path) => names.get(path) ?? lastSegment(path));
  const parts = [
    ...(vaultPart !== null ? [vaultPart] : []),
    ...(copyPart !== null ? [copyPart] : []),
    ...folderNames,
  ];
  const list = new Intl.ListFormat(["en"], {
    style: "long",
    type: "conjunction",
  }).format(parts);
  const description = `${t("shell.chat_folders.title")}: ${list}`;

  return (
    <Dropdown.Root open={open} onOpenChange={setOpen}>
      <Dropdown.Trigger
        className="chip"
        aria-describedby={descId}
        onClick={() => {
          if (!open) setOpen(true);
        }}
      >
        {count === 1 ? t("shell.chat_folders.one") : tf("shell.chat_folders.many", { n: count })}
      </Dropdown.Trigger>
      <span id={descId} className="sr-only">
        {description}
      </span>
      <Dropdown.Portal>
        <Dropdown.Content
          className="chat-folders-menu menu-panel"
          aria-label={t("shell.chat_folders.label")}
          side="bottom"
          align="end"
          sideOffset={6}
        >
          <Dropdown.Label className="lbl">{t("shell.chat_folders.title")}</Dropdown.Label>
          {vault !== null ? (
            <div className="mi">
              <span>{vaultPart}</span>
              <span className="mono d">{vault.root}</span>
            </div>
          ) : null}
          {codeCopy ? (
            <div className="mi">
              <span>{copyPart}</span>
              <span className="mono d">{codeCopy.path}</span>
            </div>
          ) : null}
          {folders.map((path) => (
            <div key={path} className="mi">
              <span>{names.get(path) ?? lastSegment(path)}</span>
              <span className="mono d">{path}</span>
            </div>
          ))}
          <Dropdown.Separator className="chat-folders-sep" />
          <Dropdown.Item className="mi" onSelect={() => onManage()}>
            {t("shell.chat_folders.manage")}
          </Dropdown.Item>
        </Dropdown.Content>
      </Dropdown.Portal>
    </Dropdown.Root>
  );
}
