/**
 * ContextItems: the per-kind item lists with their costs — a plain breakdown table of
 * categories, then the connectors, skills, memory files and custom agents that make them up.
 * Sits beneath ContextBar's segmented meter and ContextMeter's headline total; reads
 * only `report`, the same ContextReport they read.
 */
import type { ReactElement } from "react";
import type {
  CategoryKind,
  ContextReason,
  ContextReport,
} from "../../clients/conversations/contextApi";
import { contextShare, contextTone } from "./ContextBar";
import "./ContextItems.css";

export interface ContextItemsProps {
  report: ContextReport;
}

export const REASON_TEXT: Record<ContextReason, string> = {
  not_observed: "No reply has ended yet in this Claude Code process.",
  absent: "Claude Code did not report this.",
  unrecognized: "Claude Code reported this in a shape this app does not read.",
  timeout: "The request timed out.",
  unavailable: "This is unavailable.",
};

// The Breakdown table's note for each non-"used" kind, standing in for a share of max_tokens
// that a deferred, held-back or unspent category has none of.
const CATEGORY_NOTE: Record<Exclude<CategoryKind, "used">, string> = {
  deferred: "loaded only when used",
  buffer: "kept free for auto-compact",
  free: "free",
};

function hiddenNote(count: number): string {
  return `${count} more not shown: their names look like secrets to the app's filter, or the list is past its cap.`;
}

// Array.prototype.sort is stable (ES2019+), so equal-token items keep their report order.
function byTokensDesc<T>(items: readonly T[], tokens: (item: T) => number): T[] {
  return [...items].sort((a, b) => tokens(b) - tokens(a));
}

/** The null-or-reason / empty-or-"None" / hidden-note shell shared by every part of the report:
 * each one is either an array, or null with a reason naming why. */
function Part<T>({
  items,
  reason,
  hidden,
  render,
}: {
  items: readonly T[] | null;
  reason: ContextReason | undefined;
  hidden: number;
  render: (items: readonly T[]) => ReactElement;
}): ReactElement {
  return (
    <>
      {items === null ? (
        <p>{reason ? REASON_TEXT[reason] : ""}</p>
      ) : items.length === 0 ? (
        <p>None</p>
      ) : (
        render(items)
      )}
      {hidden > 0 ? <p className="context-items-hidden">{hiddenNote(hidden)}</p> : null}
    </>
  );
}

function shareText(tokens: number, kind: CategoryKind, maxTokens: number | null): string {
  if (kind !== "used") return CATEGORY_NOTE[kind];
  if (maxTokens === null || maxTokens <= 0) return "—";
  return `${Math.round((tokens / maxTokens) * 100)}%`;
}

function Breakdown({ report }: { report: ContextReport }): ReactElement {
  return (
    <section aria-labelledby="context-items-breakdown-h">
      <h2 id="context-items-breakdown-h">Breakdown</h2>
      <p className="context-items-key">
        Shares use the reported context window. Only used categories fill the bar; hatched space is
        kept free for auto-compact.
      </p>
      <Part
        items={report.categories}
        reason={report.reasons.categories}
        hidden={report.hidden.categories}
        render={(categories) => (
          <table>
            <thead>
              <tr>
                <th>Category</th>
                <th>Tokens</th>
                <th>Share</th>
              </tr>
            </thead>
            <tbody>
              {categories.map((category) => (
                <tr key={category.name} data-kind={category.kind}>
                  <td>
                    <span className="context-category-label">
                      <span
                        className="context-category-swatch"
                        aria-hidden="true"
                        style={
                          category.kind === "used"
                            ? { background: `var(${contextTone(category.name)})` }
                            : undefined
                        }
                      />
                      {category.name}
                    </span>
                  </td>
                  <td>{category.tokens}</td>
                  <td>
                    {shareText(category.tokens, category.kind, report.max_tokens)}
                    {category.kind === "used" &&
                    report.max_tokens !== null &&
                    report.max_tokens > 0 ? (
                      <span className="context-category-track" aria-hidden="true">
                        <span
                          className="context-category-fill"
                          style={{
                            width: `${contextShare(category.tokens, report.max_tokens)}%`,
                            background: `var(${contextTone(category.name)})`,
                          }}
                        />
                      </span>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      />
    </section>
  );
}

function Connectors({ report }: { report: ContextReport }): ReactElement {
  return (
    <section aria-labelledby="context-items-connectors-h">
      <h2 id="context-items-connectors-h">Connectors (MCP servers)</h2>
      <Part
        items={report.mcp_servers}
        reason={report.reasons.mcp_servers}
        hidden={report.hidden.mcp_servers}
        render={(servers) => (
          <ul>
            {byTokensDesc(servers, (server) => server.loaded_tokens).map((server) => (
              <li key={server.server}>
                <p>{server.server}</p>
                <p>
                  {server.loaded === 0
                    ? "names only; counted in MCP tools (deferred)"
                    : `${server.tools} tools · ${server.loaded} loaded · ${server.loaded_tokens} tokens`}
                </p>
              </li>
            ))}
          </ul>
        )}
      />
    </section>
  );
}

function Skills({ report }: { report: ContextReport }): ReactElement {
  const skills = report.skills;
  return (
    <section aria-labelledby="context-items-skills-h">
      <h2 id="context-items-skills-h">Skills</h2>
      {skills ? (
        <p>
          {skills.included} of {skills.total} listed
          {skills.tokens === null ? "" : ` · ${skills.tokens} tokens`}
        </p>
      ) : null}
      <Part
        items={skills === null ? null : skills.items}
        reason={report.reasons.skills}
        hidden={report.hidden.skills}
        render={(items) => (
          <ul>
            {byTokensDesc(items, (item) => item.tokens).map((item, index) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: a plain read-only list refreshed whole on every load; no item carries its own stable id.
              <li key={index}>
                {item.name} ({item.plugin === null ? item.source : `plugin ${item.plugin}`}) ·{" "}
                {item.tokens} tokens
              </li>
            ))}
          </ul>
        )}
      />
    </section>
  );
}

function MemoryFiles({ report }: { report: ContextReport }): ReactElement {
  return (
    <section aria-labelledby="context-items-memory-h">
      <h2 id="context-items-memory-h">Memory files</h2>
      <Part
        items={report.memory_files}
        reason={report.reasons.memory_files}
        hidden={report.hidden.memory_files}
        render={(files) => (
          <ul>
            {byTokensDesc(files, (file) => file.tokens).map((file, index) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: a plain read-only list refreshed whole on every load; no item carries its own stable id.
              <li key={index}>
                {file.name} · {file.type} · {file.tokens} tokens
              </li>
            ))}
          </ul>
        )}
      />
    </section>
  );
}

function CustomAgents({ report }: { report: ContextReport }): ReactElement {
  return (
    <section aria-labelledby="context-items-agents-h">
      <h2 id="context-items-agents-h">Custom agents</h2>
      <Part
        items={report.agents}
        reason={report.reasons.agents}
        hidden={report.hidden.agents}
        render={(agents) => (
          <ul>
            {byTokensDesc(agents, (agent) => agent.tokens).map((agent, index) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: a plain read-only list refreshed whole on every load; no item carries its own stable id.
              <li key={index}>
                {agent.type} · {agent.source} · {agent.tokens} tokens
              </li>
            ))}
          </ul>
        )}
      />
    </section>
  );
}

export function ContextItems({ report }: ContextItemsProps): ReactElement {
  return (
    <div className="context-items context-category-colors">
      <Breakdown report={report} />
      <Connectors report={report} />
      <Skills report={report} />
      <MemoryFiles report={report} />
      <CustomAgents report={report} />
    </div>
  );
}
