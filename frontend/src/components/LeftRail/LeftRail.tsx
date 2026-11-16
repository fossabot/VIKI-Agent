import type { ReactNode } from "react";
import { isMac } from "../../platform";
import { Icon, type IconName } from "../Icon/Icon";
import "./LeftRail.css";

export type RailDestination =
  | "chat"
  | "capabilities"
  | "messaging"
  | "bots"
  | "kanban"
  | "bot_threads"
  | "vault"
  | "agents"
  | "routines"
  | "code"
  | "decision";

const DESTINATIONS: Record<RailDestination, { label: string; icon: IconName }> = {
  chat: { label: "Chat", icon: "chat" },
  capabilities: { label: "Capabilities", icon: "capabilities" },
  messaging: { label: "Messaging", icon: "messaging" },
  bots: { label: "Bots", icon: "bots" },
  kanban: { label: "Kanban", icon: "kanban" },
  bot_threads: { label: "Bot threads", icon: "threads" },
  vault: { label: "Vault", icon: "folder" },
  agents: { label: "Agents", icon: "agents" },
  routines: { label: "Routines", icon: "routines" },
  code: { label: "Code", icon: "code" },
  decision: { label: "Decision", icon: "decision" },
};

export interface LeftRailProps {
  onSearch?: () => void;
  onNewConversation?: (modalOrigin?: HTMLElement) => void;
  /** Refused creation actions stay focusable so their readiness reason can be announced. */
  newSession?: { disabled: boolean; describedBy?: string };
  onNavigate?: (destination: RailDestination, modalOrigin?: HTMLElement) => void;
  destinations?: readonly RailDestination[];
  /** Destinations that keep their slot but cannot be used yet. */
  waiting?: readonly RailDestination[];
  currentView?: string;
  routinesAttention?: number;
  workspace?: { label: string; detail?: string };
  onWorkspace?: (modalOrigin?: HTMLElement) => void;
  /** The existing session list and its controls. */
  children?: ReactNode;
}

/** Optional destinations share App's navigation actions; the session list stays in its slot. */
export function LeftRail({
  onSearch,
  onNewConversation,
  newSession,
  onNavigate,
  destinations = ["chat", "capabilities"],
  waiting = [],
  currentView,
  routinesAttention = 0,
  workspace,
  onWorkspace,
  children,
}: LeftRailProps) {
  return (
    <nav className="left-rail" data-region="left-rail" aria-label="Navigation and sessions">
      {onNewConversation || onNavigate ? (
        <div className="rail-navigation">
          {onNewConversation ? (
            <button
              type="button"
              className="rail-nav-button rail-new new-session"
              data-region="new-session"
              aria-disabled={newSession?.disabled === true ? true : undefined}
              aria-describedby={newSession?.disabled ? newSession.describedBy : undefined}
              onClick={(event) => {
                if (newSession?.disabled) return;
                onNewConversation(
                  event.currentTarget.closest<HTMLElement>('[aria-modal="true"]') ?? undefined,
                );
              }}
            >
              <Icon name="plus" />
              <span>New session</span>
            </button>
          ) : null}
          {onNavigate
            ? destinations.map((destination) => (
                <button
                  type="button"
                  key={destination}
                  className="rail-nav-button"
                  disabled={waiting.includes(destination)}
                  aria-label={
                    destination === "routines" && routinesAttention > 0
                      ? `Routines, ${routinesAttention} need you`
                      : undefined
                  }
                  aria-current={currentView === destination ? "page" : undefined}
                  onClick={(event) =>
                    onNavigate(
                      destination,
                      event.currentTarget.closest<HTMLElement>('[aria-modal="true"]') ?? undefined,
                    )
                  }
                >
                  <Icon name={DESTINATIONS[destination].icon} />
                  <span>{DESTINATIONS[destination].label}</span>
                  {destination === "routines" && routinesAttention > 0 ? (
                    <span className="decisions-count rail-count" aria-hidden="true">
                      {routinesAttention}
                    </span>
                  ) : null}
                </button>
              ))
            : null}
        </div>
      ) : null}
      <button type="button" className="rail-search" onClick={onSearch}>
        <Icon name="search" size={14} />
        <span>Search sessions</span>
        <span className="rail-search-hint mono">{isMac() ? "⌘K" : "Ctrl K"}</span>
      </button>
      <div className="rail-sessions">{children}</div>
      {workspace || onWorkspace ? (
        <div className="rail-footer">
          {onWorkspace ? (
            <button
              type="button"
              className="rail-workspace"
              aria-label="Choose workspace"
              onClick={(event) =>
                onWorkspace(
                  event.currentTarget.closest<HTMLElement>('[aria-modal="true"]') ?? undefined,
                )
              }
            >
              <Icon name="folder" />
              <span>
                <strong>{workspace?.label ?? "Workspace"}</strong>
                {workspace?.detail ? <small>{workspace.detail}</small> : null}
              </span>
            </button>
          ) : (
            <div className="rail-workspace">
              <span>
                <strong>{workspace?.label}</strong>
                {workspace?.detail ? <small>{workspace.detail}</small> : null}
              </span>
            </div>
          )}
          <small className="rail-local">Local app · Claude permissions</small>
        </div>
      ) : null}
    </nav>
  );
}
