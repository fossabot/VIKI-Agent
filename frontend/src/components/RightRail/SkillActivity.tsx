import { type ReactElement, useMemo } from "react";
import type { SessionView, ToolRowView } from "../../state/view";
import "./SkillActivity.css";

export interface SkillCall {
  id: string;
  label: string;
  status: "calling" | "invoked" | "failed" | "unknown";
}

export function observedSkillCalls(view: SessionView | null | undefined): SkillCall[] {
  if (!view) return [];

  const recent: { call: SkillCall; order: number; position: number }[] = [];
  const newestFirst = (a: (typeof recent)[number], b: (typeof recent)[number]) =>
    b.order - a.order || a.position - b.position;
  let position = 0;
  const live = view.synced && !view.closed && !view.blocked && !view.truncated && !view.error;
  for (let t = view.turns.length - 1; t >= 0; t--) {
    const turn = view.turns[t];
    if (!turn) continue;
    for (let b = turn.blocks.length - 1; b >= 0; b--) {
      const block = turn.blocks[b];
      if (block?.kind !== "tools" && block?.kind !== "routing") continue;
      const rows: ToolRowView[] = block.kind === "tools" ? block.rows : block.tools;
      for (let r = rows.length - 1; r >= 0; r--) {
        const row = rows[r];
        if (row?.tool !== "Skill") continue;
        const label = row.row.rest.trim() || "Skill call";
        const order = row.skillCallOrder ?? Number.NEGATIVE_INFINITY;
        const status: SkillCall["status"] = row.done
          ? row.isError
            ? "failed"
            : "invoked"
          : live &&
              turn.status === "running" &&
              !turn.history &&
              (block.kind !== "routing" || block.status === "running")
            ? "calling"
            : "unknown";
        const candidate = { call: { id: row.id, label, status }, order, position: position++ };
        const duplicate = recent.findIndex((item) => item.call.label === label);
        if (duplicate >= 0) {
          const prior = recent[duplicate];
          if (prior && order > prior.order) {
            recent[duplicate] = candidate;
            recent.sort(newestFirst);
          }
          continue;
        }
        if (recent.length < 3) {
          recent.push(candidate);
          recent.sort(newestFirst);
          continue;
        }
        const oldest = recent[2];
        if (oldest && order > oldest.order) {
          recent[2] = candidate;
          recent.sort(newestFirst);
        }
      }
    }
  }
  return recent.map((item) => item.call);
}

const statusText: Record<SkillCall["status"], string> = {
  calling: "Calling",
  invoked: "Invoked",
  failed: "Failed",
  unknown: "Result unknown",
};

export function SkillActivity({
  view,
}: {
  view: SessionView | null | undefined;
}): ReactElement | null {
  const calls = useMemo(() => observedSkillCalls(view), [view]);
  if (calls.length === 0) return null;
  return (
    <div className="skill-activity">
      <p className="skill-activity-title">Recent Skill calls · loaded chat</p>
      <ul className="skill-activity-list">
        {calls.map((call) => (
          <li
            className={`skill-activity-call${call.status === "calling" ? " skill-activity-call--calling" : ""}`}
            key={call.id}
          >
            <span className="skill-activity-name" title={call.label}>
              {call.label}
            </span>
            <span className="skill-activity-status">{statusText[call.status]}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
