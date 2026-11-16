export interface OrganizedSessionMenuProps {
  sessionId: string;
  pinned: boolean;
  archived: boolean;
  pending: boolean;
  onRename: () => void;
  onPin: () => void;
  onArchive: () => void;
  onFork?: () => void;
  onOpenTranscript?: () => void;
  onClose?: () => void;
  unread?: boolean;
  color?: SessionColor | null;
  onUnread?: () => void;
  onColor?: (color: SessionColor | null) => void;
  onNewWindow?: () => void;
  newWindowPending?: boolean;
  newWindowError?: string | null;
  onExport?: () => void;
  onOpenTerminal?: () => void;
  onNewChatInWorkspace?: () => void;
}

export function OrganizedSessionMenu({
  sessionId,
  pinned,
  archived,
  pending,
  onRename,
  onPin,
  onArchive,
  onFork,
  onOpenTranscript,
  onClose,
  unread = false,
  color = null,
  onUnread,
  onColor,
  onNewWindow,
  newWindowPending = false,
  newWindowError,
  onExport,
  onOpenTerminal,
  onNewChatInWorkspace,
}: OrganizedSessionMenuProps) {
  const [open, setOpen] = useState(false);
  const [copyStatus, setCopyStatus] = useState<string | null>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const pointerClick = useRef(false);

  async function copyId() {
    setCopyStatus(null);
    try {
      if (typeof navigator.clipboard?.writeText !== "function") throw Error("unavailable");
      await navigator.clipboard.writeText(sessionId);
      setCopyStatus("ID copied");
    } catch {
      setCopyStatus("Copy failed");
    }
  }

  return (
    <>
      <Dropdown.Root open={open} onOpenChange={setOpen} modal={false}>
        <Dropdown.Trigger
          ref={triggerRef}
          className="sess-menu"
          aria-label="Session menu"
          onPointerDown={(event) => {
            pointerClick.current = true;
            event.stopPropagation();
          }}
          onKeyDown={(event) => {
            pointerClick.current = false;
            event.stopPropagation();
          }}
          onClick={(event) => {
            event.stopPropagation();
            // Radix already toggled on pointerdown; its trailing click must not reopen it.
            if (!pointerClick.current && !open) setOpen(true);
            pointerClick.current = false;
          }}
        >
          ⋯
        </Dropdown.Trigger>
        <Dropdown.Portal container={triggerRef.current?.closest<HTMLElement>(".rail-drawer")}>
          <Dropdown.Content
            className="organized-session-menu menu-panel"
            align="end"
            sideOffset={6}
            collisionPadding={8}
            onPointerDown={(event) => event.stopPropagation()}
            onClick={(event) => event.stopPropagation()}
            onKeyDown={(event) => event.stopPropagation()}
          >
            {onNewWindow && (
              <Dropdown.Item
                className="mi"
                disabled={newWindowPending}
                onSelect={() => onNewWindow()}
              >
                <Icon name="newWindow" size={14} />
                {newWindowPending ? "Opening window…" : "New window"}
              </Dropdown.Item>
            )}
            {onOpenTerminal && (
              <Dropdown.Item className="mi" onSelect={() => onOpenTerminal()}>
                <Icon name="terminal" size={14} />
                Open in terminal
              </Dropdown.Item>
            )}
            <Dropdown.Item className="mi" disabled={pending} onSelect={onRename}>
              <Icon name="rename" size={14} />
              Rename…
            </Dropdown.Item>
            <Dropdown.Item className="mi" disabled={pending} onSelect={onPin}>
              <Icon name={pinned ? "unpin" : "pin"} size={14} />
              {pinned ? "Unpin" : "Pin"}
            </Dropdown.Item>
            {onUnread && (
              <Dropdown.Item className="mi" disabled={pending} onSelect={() => onUnread()}>
                <Icon name={unread ? "markRead" : "markUnread"} size={14} />
                {unread ? "Mark as read" : "Mark as unread"}
              </Dropdown.Item>
            )}
            {onColor && (
              <SessionColorMenu
                color={color}
                pending={pending}
                onColor={onColor}
                portalContainer={triggerRef.current?.closest<HTMLElement>(".rail-drawer") ?? null}
              />
            )}
            <Dropdown.Item className="mi" onSelect={() => void copyId()}>
              <Icon name="copy" size={14} />
              Copy ID
            </Dropdown.Item>
            {onFork && (
              <Dropdown.Item className="mi" onSelect={onFork}>
                <Icon name="fork" size={14} />
                Fork
              </Dropdown.Item>
            )}
            {onOpenTranscript && (
              <Dropdown.Item className="mi" onSelect={onOpenTranscript}>
                <Icon name="transcript" size={14} />
                Open transcript in VS Code
              </Dropdown.Item>
            )}
            {onExport && (
              <Dropdown.Item className="mi" onSelect={() => onExport()}>
                <Icon name="export" size={14} />
                Select turns to export
              </Dropdown.Item>
            )}
            {onNewChatInWorkspace && (
              <Dropdown.Item className="mi" onSelect={() => onNewChatInWorkspace()}>
                <Icon name="newChat" size={14} />
                New chat in workspace…
              </Dropdown.Item>
            )}
            <Dropdown.Item className="mi" disabled={pending} onSelect={onArchive}>
              <Icon name={archived ? "unarchive" : "archive"} size={14} />
              {archived ? "Unarchive" : "Archive"}
            </Dropdown.Item>
            {onClose && (
              <Dropdown.Item className="mi organized-session-close" onSelect={onClose}>
                <Icon name="close" size={14} />
                Close session
              </Dropdown.Item>
            )}
          </Dropdown.Content>
        </Dropdown.Portal>
      </Dropdown.Root>
      {copyStatus && (
        <span className="sess-copy-status" role="status">
          {copyStatus}
        </span>
      )}
      {onNewWindow && newWindowError && (
        <span className="sess-window-error" role="alert">
          {newWindowError}
        </span>
      )}
    </>
  );
}

import * as Dropdown from "@radix-ui/react-dropdown-menu";
import { useRef, useState } from "react";
import type { SessionColor } from "../../clients/conversations/sessionMetadataApi";
import { Icon } from "../Icon/Icon";
import { SessionColorMenu } from "./SessionColorMenu";
import "./OrganizedSessionMenu.css";
