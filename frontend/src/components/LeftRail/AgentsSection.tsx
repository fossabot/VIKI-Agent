/**
 * The agent rows in the right rail (after Needs review, before Routines, inside the
 * right drawer below 1280 px). This is the Library roster: App folds it into a closed
 * `Library · N subagents` `<details>` under the run list, and the runs themselves live in
 * `AgentRuns` (components/RightRail/AgentRuns.tsx), not here. VIKI's row and the roster's order,
 * data-agent, presence and `.s`/`s-on` wording are the contract
 * src/accept/m3b/agents.accept.test.tsx pins; each roster row
 * also toggles its own profile.
 *
 * Rules:
 * - One `.agent` row per entry, in this order: VIKI first (a plain div: Face size 24
 *   with presence = vikiOnline, the assistant name, and `.s` "here now" — class `s-on`, orange —
 *   when online, else no `.s`), then `agents` in the given order (rails.agents arrives sorted by
 *   name): AgentFace(name, active = running here), the name, and `.s` = agentStatus(agent,
 *   running, nowMs); a "here now" `.s` carries `s-on`.
 * - A roster row (never VIKI's) is a `<button type="button" class="agent" data-agent
 *   aria-expanded aria-controls>`. Clicking it, independently of every other row, toggles a
 *   sibling `.agent-details` with: its description (or "No description.") and "Model <model or
 *   inherit> · Tools <tools or all tools> · Last run <time or never>".
 * - Model, tool and description text comes from the vault, not from this app: every one of them
 *   renders as a plain text node, never markup.
 * - Ports the design's .agent / .s rules into AgentsSection.css, which holds the only copy of those
 *   rules: 34px rows, flex, 10px gap, 0 8px
 *   padding, 13px #D4D4D4; .s pushed right (margin-left auto), 12px the design's #737373
 *   (var(--t3) here); .s-on #D77757.
 */
import { Fragment, type ReactElement, useState } from "react";
import type { AgentInfo } from "../../protocol";
import { agentStatus, timeLabel } from "../../state/rows";
import { useIdentity } from "../Avatar/identity";
import { AgentFace } from "../Face/AgentFace";
import { Face } from "../Face/Face";
import "./AgentsSection.css";

export interface AgentsSectionProps {
  agents: readonly AgentInfo[];
  /** Names of agents running right now (state/rows.ts runningAgents). */
  running: readonly string[];
  vikiOnline: boolean;
  nowMs: number;
}

export function AgentsSection({
  agents,
  running,
  vikiOnline,
  nowMs,
}: AgentsSectionProps): ReactElement {
  const { assistantName } = useIdentity();
  const [expanded, setExpanded] = useState<ReadonlySet<number>>(new Set());

  function toggle(index: number): void {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  }

  return (
    <div>
      <div className="agent" data-agent="VIKI">
        <Face size={24} presence={vikiOnline} />
        <span>{assistantName}</span>
        {vikiOnline ? <span className="s s-on">here now</span> : null}
      </div>
      {agents.map((agent, index) => {
        const active = running.includes(agent.name);
        const status = agentStatus(agent, running, nowMs);
        const isExpanded = expanded.has(index);
        const detailsId = `agent-details-${index}`;
        return (
          // Keyed with the index too: two `.claude/agents/*.md` files could declare the same
          // `name` in frontmatter, which would otherwise collide.
          // biome-ignore lint/suspicious/noArrayIndexKey: see above
          <Fragment key={`${agent.name}:${index}`}>
            <button
              type="button"
              className="agent"
              data-agent={agent.name}
              aria-expanded={isExpanded}
              aria-controls={detailsId}
              onClick={() => toggle(index)}
            >
              <AgentFace name={agent.name} size={24} active={active} />
              <span>{agent.name}</span>
              <span className={status === "here now" ? "s s-on" : "s"}>{status}</span>
            </button>
            {isExpanded ? (
              <div className="agent-details" id={detailsId}>
                <p>{agent.description || "No description."}</p>
                <p>
                  {`Model ${agent.model || "inherit"} · Tools ${
                    agent.tools.length > 0 ? agent.tools.join(", ") : "all tools"
                  } · Last run ${agent.last_run ? timeLabel(agent.last_run, nowMs) : "never"}`}
                </p>
              </div>
            ) : null}
          </Fragment>
        );
      })}
    </div>
  );
}
