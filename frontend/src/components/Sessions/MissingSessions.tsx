/**
 * App-owned records (a label) whose session is no longer in a complete session listing: the
 * transcript is gone or unreachable, but this app still holds something for it. A confirmed
 * profile draft (prepared, never started) offers Open and no Remove: removing its record would
 * drop only its title and leave the prepared session unreachable. Every other record offers
 * Unpin (when pinned) and Remove.
 */
import type { ReactElement } from "react";
import type { MissingRecord } from "../../state/sessionOrganization";
import "./Sessions.css";

/** A record's own name: its title, or, when untitled, "Untitled session" with the first 8
 * characters of its id. Used by the row below and by App's own failure line alike. */
export function missingRecordLabel(record: MissingRecord): string {
  return record.title || `Untitled session (${record.session_id.slice(0, 8)})`;
}

export interface MissingSessionsProps {
  records: readonly MissingRecord[];
  onUnpin: (sessionId: string) => void;
  onRemove: (sessionId: string) => void;
  /** Opens a record's session (App's own `openSession`). Only reachable through Open, on a
   * record `drafts` names; without it, that record still shows its draft tag, without Open. */
  onOpen?: (sessionId: string) => void;
  pending: ReadonlySet<string>;
  /** Ids of missing records this app has confirmed are a prepared, never-started profile
   * session (a 200 from `profileLaunchApi.inspect` with `started` false), not an actually-gone
   * transcript. */
  drafts?: ReadonlySet<string>;
}

export function MissingSessions({
  records,
  onUnpin,
  onRemove,
  onOpen,
  pending,
  drafts = new Set(),
}: MissingSessionsProps): ReactElement | null {
  if (records.length === 0) return null;
  return (
    <section className="missing-sessions" aria-label="Conversations whose record is missing">
      <h3>Conversations whose record is missing</h3>
      <ul>
        {records.map((record) => {
          const busy = pending.has(record.session_id);
          const isDraft = drafts.has(record.session_id);
          return (
            <li key={record.session_id} className="session-organizer">
              <p>{missingRecordLabel(record)}</p>
              <p className="missing-session-tags">
                <span>App metadata</span>
                {isDraft ? <span>Profile draft, not started</span> : <span>Source missing</span>}
                {record.pinned ? <span>Pinned (in this app)</span> : null}
              </p>
              <div className="session-organizer-actions">
                {record.pinned ? (
                  <button type="button" disabled={busy} onClick={() => onUnpin(record.session_id)}>
                    Unpin
                  </button>
                ) : null}
                {isDraft && onOpen ? (
                  <button type="button" disabled={busy} onClick={() => onOpen(record.session_id)}>
                    Open
                  </button>
                ) : null}
                {isDraft ? null : (
                  <button type="button" disabled={busy} onClick={() => onRemove(record.session_id)}>
                    Remove
                  </button>
                )}
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
