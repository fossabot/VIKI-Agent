/**
 * ArchiveBar: the left rail's "Archived (N)" chip, a single pill that carries the count and,
 * while the archived list is showing, the retention note: archiving closes an open chat and
 * hides it from this app's list. Claude Code still deletes the transcript on its own schedule
 * (cleanupPeriodDays), unrelated to the `archived` flag.
 *
 * No behaviour of its own: showArchived and archivedCount are read, never derived here, and a
 * click only calls back. Hidden entirely once there is nothing archived and nobody asked to see
 * it; the empty-but-pressed case stays visible so the user can still toggle back off.
 */
import type { ReactElement } from "react";
import "./ArchiveBar.css";

export interface ArchiveBarProps {
  showArchived: boolean;
  archivedCount: number;
  onToggle: () => void;
}

const RETENTION_NOTE =
  "Archiving closes an open chat and hides it from this list. Claude Code still deletes each transcript 30 days " +
  "after its last activity by default (cleanupPeriodDays).";

export function ArchiveBar({
  showArchived,
  archivedCount,
  onToggle,
}: ArchiveBarProps): ReactElement | null {
  if (archivedCount === 0 && !showArchived) return null;

  return (
    <div className="archive-bar">
      <button type="button" className="archive-chip" aria-pressed={showArchived} onClick={onToggle}>
        {`Archived (${archivedCount})`}
      </button>
      {showArchived && <p className="archive-note">{RETENTION_NOTE}</p>}
    </div>
  );
}
