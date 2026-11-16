/** Renders session row actions and reports the selected action to its caller. The caller owns visibility, placement and behavior; focus and hover styles come from CSS. */
import type { ReactElement } from "react";
import { Icon } from "../Icon/Icon";
import "./RowMenu.css";

export interface RowMenuProps {
  onFork: () => void;
  onOpenTranscript: () => void;
  onClose: () => void;
  onArchive?: () => void;
  archived?: boolean;
}

export function RowMenu({
  onFork,
  onOpenTranscript,
  onClose,
  onArchive,
  archived,
}: RowMenuProps): ReactElement {
  return (
    <div className="row-menu menu-panel" role="menu">
      <button type="button" className="mi" role="menuitem" onClick={onFork}>
        <Icon name="fork" size={14} />
        Fork
      </button>
      <button type="button" className="mi" role="menuitem" onClick={onOpenTranscript}>
        <Icon name="transcript" size={14} />
        Open transcript in VS Code
      </button>
      {onArchive && (
        <button type="button" className="mi" role="menuitem" onClick={onArchive}>
          <Icon name={archived ? "unarchive" : "archive"} size={14} />
          {archived ? "Unarchive" : "Archive"}
        </button>
      )}
      <button type="button" className="mi row-menu-close" role="menuitem" onClick={onClose}>
        <Icon name="close" size={14} />
        Close session
      </button>
    </div>
  );
}
