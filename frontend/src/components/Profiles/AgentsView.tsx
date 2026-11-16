/**
 * The Agents view's sections behind one tablist: Profiles (the profile list and editor), Running
 * work (this app's live sessions, plus a read-only list of sessions running elsewhere), and the
 * optional Native and Tools sections. Only the selected section's content mounts, so switching
 * tabs unmounts the other one; the caller guards `onSection` the same way it guards leaving the
 * view, so unsaved profile work asks first. Native and Tools each show a tab only when the caller
 * gives its node.
 */
import { type ReactElement, type ReactNode, useEffect, useRef } from "react";
import { t } from "../../i18n/t";
import "./AgentsView.css";

export type AgentsSectionId = "profiles" | "running" | "native" | "tools";

export interface AgentsViewProps {
  section: AgentsSectionId;
  onSection: (section: AgentsSectionId) => void;
  /** "Back to chat", the way out every non-chat view offers. */
  onBack: () => void;
  renderBack?: (onBack: () => void) => ReactNode;
  profiles: ReactNode;
  running: ReactNode;
  native?: ReactNode;
  tools?: ReactNode;
  onSkills?: () => void;
  onBots?: () => void;
}

export function AgentsView({
  section,
  onSection,
  onBack,
  renderBack,
  profiles,
  running,
  native,
  tools,
  onSkills,
  onBots,
}: AgentsViewProps): ReactElement {
  const headingRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    headingRef.current?.focus();
  }, []);
  const panels: Partial<Record<AgentsSectionId, ReactNode>> = { profiles, running, native, tools };
  const tabs: readonly { id: AgentsSectionId; label: string }[] = [
    { id: "profiles", label: "Profiles" },
    { id: "running", label: "Running work" },
    ...(native !== undefined ? [{ id: "native" as const, label: t("bots.subagents.tab") }] : []),
    ...(tools !== undefined ? [{ id: "tools" as const, label: "Tools" }] : []),
  ];
  return (
    <div className="agents-view">
      {renderBack?.(onBack) ?? (
        <button type="button" className="btn txt sm" onClick={onBack}>
          Back to chat
        </button>
      )}
      <header className="page-head">
        <h1 className="page-title" ref={headingRef} tabIndex={-1}>
          Agents
        </h1>
        <p className="page-intro">{t("bots.agents.help")}</p>
        <div className="page-actions">
          {onBots ? (
            <button type="button" className="btn alt" onClick={onBots}>
              {t("bots.chat.all")}
            </button>
          ) : null}
          {native !== undefined && native !== null && onSkills ? (
            <button type="button" className="btn txt sm" onClick={onSkills}>
              Skills
            </button>
          ) : null}
        </div>
      </header>
      <div className="agents-view-tabs" role="tablist" aria-label="Agents">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            type="button"
            id={`agents-view-tab-${tab.id}`}
            role="tab"
            aria-selected={section === tab.id}
            aria-controls="agents-view-panel"
            onClick={() => onSection(tab.id)}
          >
            {tab.label}
          </button>
        ))}
      </div>
      <div id="agents-view-panel" role="tabpanel" aria-labelledby={`agents-view-tab-${section}`}>
        {panels[section]}
      </div>
    </div>
  );
}
