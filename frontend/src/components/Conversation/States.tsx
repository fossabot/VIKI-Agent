/** Render conversation empty and unavailable states. A Bot's face takes the mark's slot; a bound chat shows no portrait row or chips. */
import type { ReactElement } from "react";

export interface HistoryNoteProps {
  /** True once this window has taken a turn in the transcript: the invitation to resume has
   * been accepted and must stop being offered. */
  resumed?: boolean;
}

import { useIdentity } from "../Avatar/identity";
import { TileWordmark } from "../Brand/TileWordmark";
import { Face } from "../Face/Face";
import "./States.css";

export interface LandingSession {
  sessionId: string;
  title: string;
  pinned?: boolean;
  description?: string;
}

export interface EmptyStateProps {
  landingSessions?: readonly LandingSession[];
  onOpenSession?: (sessionId: string) => void;
}

export function EmptyState({ landingSessions, onOpenSession }: EmptyStateProps): ReactElement {
  const { assistantName, profile, unresolved } = useIdentity();
  const sessions = !unresolved && onOpenSession ? (landingSessions ?? []).slice(0, 6) : [];
  const interactive = !unresolved && Boolean(onOpenSession);
  const bound = !unresolved && profile !== null;
  return (
    <div className={`empty${interactive ? " empty-interactive" : ""}`}>
      {bound ? null : <Face size={40} presence />}
      <div className="empty-title jp">New conversation</div>
      <div className="empty-sub jp">
        {unresolved
          ? "Start talking. Use / to call a skill."
          : `Talk to ${assistantName}. Use / to call a skill.`}
      </div>
      {!unresolved ? (
        <div className="empty-brand">
          {profile?.botProfileId ? (
            <div className="empty-brand-face" aria-hidden="true">
              <Face size={160} />
            </div>
          ) : null}
          {/* Screen readers read the label; the hidden text keeps the name in the page text. */}
          <div className="empty-wordmark" role="img" aria-label="VIKI Agent">
            <TileWordmark size="large" />
            <span className="sr-only">VIKI</span>
            <span className="sr-only">AGENT</span>
          </div>
          <div className="empty-acronym">Virtual Interactive Kinetic Intelligence</div>
        </div>
      ) : null}
      {sessions.length > 0 ? (
        <div className="empty-session-desk">
          {[true, false].map((pinned) => {
            const rows = sessions.filter((session) => Boolean(session.pinned) === pinned);
            return rows.length > 0 ? (
              <section
                key={String(pinned)}
                aria-label={pinned ? "Pinned sessions" : "Recent sessions"}
              >
                <h2 className="lbl">{pinned ? "Pinned" : "Recent sessions"}</h2>
                <div className="empty-session-tiles">
                  {rows.map((session) => (
                    <button
                      key={session.sessionId}
                      type="button"
                      className="empty-session-tile"
                      data-landing-session-id={session.sessionId}
                      aria-label={
                        session.description
                          ? `${session.title}, ${session.description}`
                          : session.title
                      }
                      onClick={() => onOpenSession?.(session.sessionId)}
                    >
                      <strong>{session.title}</strong>
                      {session.description ? <small>{session.description}</small> : null}
                    </button>
                  ))}
                </div>
              </section>
            ) : null;
          })}
        </div>
      ) : null}
    </div>
  );
}

export function HistoryNote({ resumed = false }: HistoryNoteProps): ReactElement {
  return (
    <div className="history-note jp">
      <span className="history-ring" aria-hidden="true" />
      {resumed
        ? "This continues a past conversation."
        : "This is a past conversation. Send a message to resume from here."}
      <span className="history-meta">history · usage and thinking times are not recorded</span>
    </div>
  );
}
