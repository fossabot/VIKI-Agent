import { type KeyboardEvent, type ReactNode, useEffect, useId, useState } from "react";
import type { View } from "../../commands";
import { tf } from "../../i18n/t";
import { useIdentity } from "../Avatar/identity";
import { TileWordmark } from "../Brand/TileWordmark";
import "./TopBar.css";

/** One rail's drawer toggle: shown only for a rail this window is too narrow to dock. */
export interface RailToggle {
  open: boolean;
  onToggle: () => void;
  /** When above 0, a dot appears on the toggle to show that something needs attention. */
  attention?: number;
}

export interface TopBarProps {
  /** App's shared sidebar owns primary navigation and creation actions when present. */
  navigationInSidebar?: boolean;
  onChat?: () => void;
  onCapabilities?: () => void;
  onVault?: () => void;
  onCode?: () => void;
  onAgents?: () => void;
  onBots?: () => void;
  /** Makes the Routines tab live; without it the tab stays inert. */
  onRoutines?: () => void;
  /** Routines that need the user; a count above 0 shows a bubble on the Routines tab. */
  routinesAttention?: number;
  onNewSession: () => void;
  /** The drawer toggles. A rail that is docked supplies none, and none is drawn: the
   * top bar never offers to open something already on screen. */
  rails?: { left?: RailToggle; right?: RailToggle };
  /** Which view the user is in, so the Chat tab stops claiming to be the selected one while
   * another view is on screen. Defaults to "chat". */
  view?: View;
  /** The app menu, rendered first in `.topbar-right`. */
  appMenu?: ReactNode;
  /** New session's refusal: the button stays focusable and keeps its place, its
   * click is a no-op, and `describedBy` names the readiness banner's own sentence — omitted when
   * no such sentence is on screen, since an empty IDREF list describes nothing. The whole prop
   * omitted means "nothing refuses it". */
  newSession?: { disabled: boolean; describedBy?: string };
}

/** The app's top bar: identity, menus and drawer controls. Standalone callers keep
 * primary tabs and creation actions unless their sidebar owns them.
 *
 * Below 1280 it also carries the VIKI panel's drawer toggle, and below 800 the sessions
 * drawer's as well; below 800 the tab strip is replaced by the compact primary-view select
 * (TopBar.css), since the narrow bar needs the room. */
export function TopBar({
  navigationInSidebar = false,
  onChat,
  onCapabilities,
  onVault,
  onCode,
  onAgents,
  onBots,
  onRoutines,
  routinesAttention = 0,
  onNewSession,
  rails,
  view = "chat",
  appMenu,
  newSession,
}: TopBarProps) {
  const chatSelected = view === "chat";
  const available: View[] = [];
  if (onChat) available.push("chat");
  if (onCapabilities) available.push("capabilities");
  if (onAgents) available.push("agents");
  if (onBots) available.push("bots");
  if (onRoutines) available.push("routines");
  if (onVault) available.push("vault");
  if (onCode) available.push("code");
  const capabilityKey = available.join(",");
  const fallback = available.includes(view) ? view : available[0];
  const [focused, setFocused] = useState({ view, capabilityKey, tab: fallback });
  useEffect(() => {
    setFocused({ view, capabilityKey, tab: fallback });
  }, [view, capabilityKey, fallback]);
  const tabStop =
    focused.view === view && focused.capabilityKey === capabilityKey ? focused.tab : fallback;
  function moveFocus(event: KeyboardEvent<HTMLDivElement>) {
    if (
      event.nativeEvent.isComposing ||
      event.keyCode === 229 ||
      event.ctrlKey ||
      event.altKey ||
      event.metaKey
    )
      return;
    const buttons = [
      ...event.currentTarget.querySelectorAll<HTMLButtonElement>("button[role=tab]"),
    ];
    const index = buttons.indexOf(event.target as HTMLButtonElement);
    if (index < 0 || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const next =
      event.key === "Home"
        ? 0
        : event.key === "End"
          ? buttons.length - 1
          : (index + (event.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length;
    event.preventDefault();
    buttons[next]?.focus();
  }
  const { assistantName, profile } = useIdentity();
  const rightRailDescriptionId = useId();
  return (
    <header className="topbar" data-region="topbar">
      {rails?.left ? (
        <button
          type="button"
          className="rail-toggle"
          data-region="left-rail-toggle"
          aria-label="Sessions and agents"
          aria-expanded={rails.left.open}
          aria-haspopup="dialog"
          onClick={rails.left.onToggle}
        >
          <svg className="ico" width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
            <path d="M2.5 4h11M2.5 8h11M2.5 12h11" />
          </svg>
        </button>
      ) : null}
      <div className="wordmark" data-region="wordmark">
        {assistantName === "VIKI" && !profile ? (
          <TileWordmark size="small" title="VIKI Agent" />
        ) : (
          assistantName
        )}
      </div>
      {!navigationInSidebar ? (
        <div
          className={`tabs${onCapabilities ? " tabs-expanded" : ""}`}
          role="tablist"
          aria-label="Primary"
          onKeyDown={moveFocus}
          onFocus={(event) => {
            if (!(event.target instanceof HTMLButtonElement)) return;
            const buttons = [
              ...event.currentTarget.querySelectorAll<HTMLButtonElement>("button[role=tab]"),
            ];
            const index = buttons.indexOf(event.target);
            if (index >= 0) setFocused({ view, capabilityKey, tab: available[index] });
          }}
        >
          {onChat ? (
            <button
              type="button"
              role="tab"
              aria-selected={chatSelected}
              tabIndex={tabStop === "chat" ? 0 : -1}
              className={`tab tab-action${chatSelected ? " tab-active" : ""}`}
              onClick={onChat}
            >
              Chat
            </button>
          ) : (
            <span
              className="tab tab-active"
              role="tab"
              aria-selected={chatSelected}
              aria-disabled="true"
              tabIndex={-1}
            >
              Chat
            </span>
          )}
          {onCapabilities ? (
            <button
              type="button"
              role="tab"
              aria-selected={view === "capabilities"}
              tabIndex={tabStop === "capabilities" ? 0 : -1}
              className={`tab tab-action${view === "capabilities" ? " tab-active" : ""}`}
              onClick={onCapabilities}
            >
              Capabilities
            </button>
          ) : null}
          {onAgents ? (
            <button
              type="button"
              role="tab"
              aria-selected={view === "agents"}
              tabIndex={tabStop === "agents" ? 0 : -1}
              className={`tab tab-action${view === "agents" ? " tab-active" : ""}`}
              onClick={onAgents}
            >
              Agents
            </button>
          ) : (
            <span
              className="tab tab-inert"
              role="tab"
              aria-selected="false"
              aria-disabled="true"
              tabIndex={-1}
            >
              Agents
            </span>
          )}
          {onBots ? (
            <button
              type="button"
              role="tab"
              aria-selected={view === "bots"}
              tabIndex={tabStop === "bots" ? 0 : -1}
              className={`tab tab-action${view === "bots" ? " tab-active" : ""}`}
              onClick={onBots}
            >
              Bots
            </button>
          ) : null}
          {onRoutines ? (
            <button
              type="button"
              role="tab"
              aria-selected={view === "routines"}
              tabIndex={tabStop === "routines" ? 0 : -1}
              className={`tab tab-action${view === "routines" ? " tab-active" : ""}`}
              onClick={onRoutines}
            >
              Routines
              {routinesAttention > 0 ? (
                <>
                  <span className="decisions-count tab-count" aria-hidden="true">
                    {routinesAttention}
                  </span>
                  <span className="sr-only">{`${routinesAttention} need you`}</span>
                </>
              ) : null}
            </button>
          ) : (
            <span
              className="tab tab-inert"
              role="tab"
              aria-selected="false"
              aria-disabled="true"
              tabIndex={-1}
            >
              Routines
            </span>
          )}
          {onVault ? (
            <button
              type="button"
              role="tab"
              aria-selected={view === "vault"}
              tabIndex={tabStop === "vault" ? 0 : -1}
              className={`tab tab-action${view === "vault" ? " tab-active" : ""}`}
              onClick={onVault}
            >
              Vault
            </button>
          ) : (
            <span
              className="tab tab-inert"
              role="tab"
              aria-selected="false"
              aria-disabled="true"
              tabIndex={-1}
            >
              Vault
            </span>
          )}
          {onCode ? (
            <button
              type="button"
              role="tab"
              aria-selected={view === "code"}
              tabIndex={tabStop === "code" ? 0 : -1}
              className={`tab tab-action${view === "code" ? " tab-active" : ""}`}
              onClick={onCode}
            >
              Code
            </button>
          ) : (
            <span
              className="tab tab-inert"
              role="tab"
              aria-selected="false"
              aria-disabled="true"
              tabIndex={-1}
            >
              Code
            </span>
          )}
        </div>
      ) : null}
      {!navigationInSidebar &&
      onChat &&
      (onCapabilities || onVault || onAgents || onBots || onRoutines || onCode) ? (
        <select
          className="compact-navigation"
          aria-label="Primary view"
          value={available.includes(view) ? view : ""}
          onChange={(event) => {
            if (event.target.value === "vault") onVault?.();
            else if (event.target.value === "capabilities") onCapabilities?.();
            else if (event.target.value === "code") onCode?.();
            else if (event.target.value === "agents") onAgents?.();
            else if (event.target.value === "bots") onBots?.();
            else if (event.target.value === "routines") onRoutines?.();
            else if (event.target.value === "chat") onChat();
          }}
        >
          {!available.includes(view) ? (
            <option value="" disabled>
              {view === "onboarding" ? "Welcome" : view.charAt(0).toUpperCase() + view.slice(1)}
            </option>
          ) : null}
          <option value="chat">Chat</option>
          {onCapabilities ? <option value="capabilities">Capabilities</option> : null}
          {onAgents ? <option value="agents">Agents</option> : null}
          {onBots ? <option value="bots">Bots</option> : null}
          {onRoutines ? <option value="routines">Routines</option> : null}
          {onVault ? <option value="vault">Vault</option> : null}
          {onCode ? <option value="code">Code</option> : null}
        </select>
      ) : null}
      <div className="topbar-right">
        {rails?.right ? (
          <button
            type="button"
            className="rail-toggle"
            data-region="right-rail-toggle"
            aria-label="VIKI panel"
            aria-expanded={rails.right.open}
            aria-haspopup="dialog"
            {...(rails.right.attention !== undefined && rails.right.attention > 0
              ? {
                  "aria-describedby": rightRailDescriptionId,
                  title: `${rails.right.attention} waiting for you`,
                }
              : {})}
            onClick={rails.right.onToggle}
          >
            <svg className="ico" width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
              <rect x="2.5" y="3" width="11" height="10" rx="1.5" />
              <path d="M10 3v10" />
            </svg>
            {rails.right.attention !== undefined && rails.right.attention > 0 ? (
              <span className="rail-toggle-dot" aria-hidden="true" />
            ) : null}
          </button>
        ) : null}
        {rails?.right?.attention !== undefined && rails.right.attention > 0 ? (
          <span id={rightRailDescriptionId} className="sr-only">
            {tf("shell.rail.waiting", { n: rails.right.attention })}
          </span>
        ) : null}
        {appMenu}
        {!navigationInSidebar ? (
          <button
            type="button"
            className="new-session"
            data-region="new-session"
            // Never the `disabled` attribute: an unfocusable button is never announced, so the
            // user would find the command simply gone instead of gone with a reason.
            {...(newSession?.disabled === true
              ? {
                  "aria-disabled": true,
                  ...(newSession.describedBy === undefined
                    ? {}
                    : { "aria-describedby": newSession.describedBy }),
                }
              : {})}
            onClick={() => {
              if (newSession?.disabled === true) return;
              onNewSession();
            }}
          >
            New session
          </button>
        ) : null}
      </div>
    </header>
  );
}
