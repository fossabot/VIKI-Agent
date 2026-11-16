/** Render thought, tool, notice and routing blocks within a reply. Provider and tool content stays text; thought text remains selectable, tool previews are bounded, and routing rows expose only known usage. Targeted routing rows can receive focus and expand their nested tools. */
import { type ReactElement, useId, useState } from "react";
import type {
  NoticeBlock,
  RoutingBlock,
  ThinkingBlock,
  ToolBlock,
  ToolRowView,
} from "../../state/view";
import { AgentFace } from "../Face/AgentFace";
import { ArtifactInspector } from "./ArtifactInspector";
import "./Conversation.css";

export interface ThoughtRowProps {
  block: ThinkingBlock;
}

export function ThoughtRow({ block }: ThoughtRowProps): ReactElement {
  const [open, setOpen] = useState(false);
  const detailsId = useId();
  const hasText = block.text !== "";
  const seconds =
    !hasText && block.done && block.durationMs !== null
      ? Math.round(block.durationMs / 1000)
      : null;

  return (
    <div className="thought-block">
      <button
        type="button"
        className="thought"
        aria-expanded={open}
        aria-controls={open ? detailsId : undefined}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="thought-label">Thought</span>
        {hasText ? (
          <>
            <span className="thought-sep" aria-hidden="true">
              ›
            </span>
            <span className="thought-summary">{block.text}</span>
          </>
        ) : seconds !== null ? (
          <span className="thought-duration">{` · ${seconds}s`}</span>
        ) : null}
      </button>
      {open ? (
        <div className="thought-full" id={detailsId}>
          {block.text}
        </div>
      ) : null}
    </div>
  );
}

const VISIBLE_WHEN_RUNNING = 3;

/** Which rows to show, and how many are folded into "+N more" — see the collapsing rule above. */
function visibleRows(
  rows: readonly ToolRowView[],
  expanded: boolean,
): { shown: readonly ToolRowView[]; more: number } {
  if (expanded || rows.length <= VISIBLE_WHEN_RUNNING) return { shown: rows, more: 0 };
  const running = rows.some((row) => !row.done);
  if (running) {
    return { shown: rows.slice(-VISIBLE_WHEN_RUNNING), more: rows.length - VISIBLE_WHEN_RUNNING };
  }
  return { shown: [], more: rows.length };
}

export interface ToolBlockRowsProps {
  block: ToolBlock;
  /** Shows every row regardless of the collapsing rule above (RoutingRow sets this from its own
   * `target` prop) — merged with the row's own `expanded` state below, not replacing it. */
  expandAll?: boolean;
}

/** The rows-only content of one tool block, with no panel of its own; `ToolRows` below
 * wraps this in the standalone `.tools mono` panel, and Conversation.tsx uses it directly to
 * fold several adjacent tools/notice blocks into ONE shared panel instead. */
export function ToolBlockRows({ block, expandAll = false }: ToolBlockRowsProps): ReactElement {
  const [expanded, setExpanded] = useState(false);
  const [openIds, setOpenIds] = useState<ReadonlySet<string>>(new Set());
  const previewPrefix = useId();
  const { shown, more } = visibleRows(block.rows, expanded || expandAll);
  const errorCount = block.rows.filter((row) => row.isError).length;
  // Rows append in arrival order; the offset keeps IDs stable when folded rows are revealed.
  const firstShownIndex = block.rows.length - shown.length;

  function togglePreview(id: string): void {
    setOpenIds((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <>
      {block.rows.length > VISIBLE_WHEN_RUNNING ? (
        <div className="tool-group-summary">
          <span>{`${block.rows.length} tool calls`}</span>
          {errorCount > 0 ? (
            <span className="tool-error-count">{`${errorCount} error${errorCount === 1 ? "" : "s"}`}</span>
          ) : null}
        </div>
      ) : null}
      {shown.map((row, index) => {
        const classes = row.isError ? "tool-row tool-row--error" : "tool-row";
        const open = openIds.has(row.id);
        const previewId = `${previewPrefix}-${firstShownIndex + index}`;
        const line = (
          <>
            <span className="tool-head">{row.row.head}</span>
            {row.isError ? <span className="tool-error-label">Error</span> : null}
            <span className="tool-rest">{row.row.rest}</span>
            {row.row.tail ? <span className="tool-tail">{row.row.tail}</span> : null}
          </>
        );
        return (
          <div key={row.id}>
            {row.done ? (
              <button
                type="button"
                className={classes}
                aria-expanded={open}
                aria-controls={open ? previewId : undefined}
                onClick={() => togglePreview(row.id)}
              >
                {line}
              </button>
            ) : (
              <div className={classes}>{line}</div>
            )}
            {row.done && open ? (
              <div className="tool-preview" id={previewId}>
                <ArtifactInspector
                  key={`${row.id}:${row.artifactResult?.source_revision ?? ""}`}
                  row={row}
                />
              </div>
            ) : null}
          </div>
        );
      })}
      {more > 0 ? (
        <button
          type="button"
          className="tool-more"
          aria-label={`Show ${more} more tool call${more === 1 ? "" : "s"}`}
          onClick={() => setExpanded(true)}
        >
          {`+${more} more`}
        </button>
      ) : null}
    </>
  );
}

export interface ToolRowsProps {
  block: ToolBlock;
  /** Forwarded to `ToolBlockRows` unchanged; see its own doc. */
  expandAll?: boolean;
}

/** A tool block in its own standalone panel — RoutingRow's nested tool calls, and any tools
 * block Conversation.tsx did not need to share a panel with a neighbouring notice. */
export function ToolRows({ block, expandAll = false }: ToolRowsProps): ReactElement {
  return (
    <div className="tools mono">
      <ToolBlockRows block={block} expandAll={expandAll} />
    </div>
  );
}

export interface RoutingRowProps {
  block: RoutingBlock;
  /** True once this row's own turn has ended while the subagent itself is still running —
   * Conversation.tsx's `renderBlock` passes `block.status === "running" && !turnRunning`. Swaps
   * the live word from "Running" to "Running in background"; unused once the subagent is done. */
  background?: boolean;
  /** True for the one row a block-level jump names (Conversation.tsx's `renderBlock` passes
   * `block.id === targetBlockId`). Outlines and briefly flashes the row (`.routing-target`),
   * lets the jump's layout effect focus the row itself via `tabIndex={-1}`, and shows its own
   * nested tool calls in full instead of collapsing them. */
  target?: boolean;
}

const STATUS_SUFFIX: Readonly<Record<RoutingBlock["status"], string>> = {
  running: "",
  completed: "",
  failed: "failed",
  stopped: "stopped",
};

function toolUseCount(n: number): string {
  return `${n} tool use${n === 1 ? "" : "s"}`;
}

function tokenCount(n: number): string {
  return n < 1000 ? `${n} tokens` : `${(n / 1000).toFixed(1)}k tokens`;
}

function elapsed(ms: number): string {
  const tenths = Math.round(ms / 100);
  if (tenths < 600) return `${(tenths / 10).toFixed(1)}s`;
  const seconds = Math.round(ms / 1000);
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

/** The `.routing-meta` line's text — see this file's RoutingRow header comment for the rule. */
function routingMeta(block: RoutingBlock, background: boolean): string {
  const word =
    block.status === "completed"
      ? "Done"
      : block.status === "running"
        ? background
          ? "Running in background"
          : "Running"
        : "";
  const parts = [word];
  if (block.toolUses !== undefined) {
    parts.push(toolUseCount(block.toolUses));
    if (block.totalTokens !== undefined) parts.push(tokenCount(block.totalTokens));
    if (block.durationMs !== undefined) parts.push(elapsed(block.durationMs));
  } else if (block.tools.length > 0) {
    parts.push(toolUseCount(block.tools.length));
  }
  return [block.description, parts.filter(Boolean).join(" · ")].filter(Boolean).join(" — ");
}

export function RoutingRow({
  block,
  background = false,
  target = false,
}: RoutingRowProps): ReactElement {
  const [summaryOpen, setSummaryOpen] = useState(false);
  const summaryId = useId();
  const suffix = STATUS_SUFFIX[block.status];
  return (
    <div
      className={`routing${target ? " routing-target" : ""}`}
      data-routing-id={block.id}
      tabIndex={target ? -1 : undefined}
    >
      <div className="routing-head">
        <AgentFace name={block.agent} size={18} active={block.status === "running"} />
        <span className="routing-text mono">{`▸ routing: ${block.agent} → ${block.model}`}</span>
        {block.reason ? <span className="routing-reason jp">{`(${block.reason})`}</span> : null}
        {suffix ? <span className="routing-status">{`· ${suffix}`}</span> : null}
      </div>
      <div className="routing-meta">{routingMeta(block, background)}</div>
      {block.summary ? (
        <>
          <div
            className={`routing-summary${summaryOpen ? " routing-summary--open" : ""}`}
            id={summaryId}
            title={block.summary}
          >
            {block.summary}
          </div>
          <button
            type="button"
            className="routing-summary-toggle"
            aria-expanded={summaryOpen}
            aria-controls={summaryId}
            aria-label={`${summaryOpen ? "Collapse" : "Expand"} summary from ${block.agent}`}
            onClick={() => setSummaryOpen((open) => !open)}
          >
            {summaryOpen ? "Show less" : "Show full summary"}
          </button>
        </>
      ) : null}
      {block.tools.length > 0 ? (
        <div className="routing-tools">
          <ToolRows
            block={{ kind: "tools", id: `${block.id}-tools`, rows: block.tools }}
            expandAll={target}
          />
        </div>
      ) : null}
    </div>
  );
}

export interface NoticeLine {
  mark: string;
  head: string;
  tail: string;
}

const NOTICE_MARK: Readonly<Record<NoticeBlock["tone"], string>> = {
  denied: "⊘",
  hook: "⊘",
  compacted: "↻",
  error: "⊘",
};

/** The one place a notice is worded — see the " revises NoticeRow" rules at the top of this
 * file. `compacted` reads its tail from `subject` (the trigger); every other tone reads it from
 * `detail`. */
export function noticeLine(block: NoticeBlock): NoticeLine {
  const mark = NOTICE_MARK[block.tone];
  switch (block.tone) {
    case "denied":
      return { mark, head: `auto mode blocked ${block.subject}`, tail: block.detail };
    case "hook":
      return { mark, head: `hook ${block.subject}`, tail: block.detail };
    case "compacted":
      return { mark, head: "context compacted", tail: block.subject };
    case "error":
      return { mark, head: `error ${block.subject}`, tail: block.detail };
  }
}

export interface NoticeRowProps {
  block: NoticeBlock;
}

export function NoticeRow({ block }: NoticeRowProps): ReactElement {
  const { mark, head, tail } = noticeLine(block);
  return (
    <div className="notice-row">
      <span className="notice-mark" aria-hidden="true">
        {mark}
      </span>
      <span className="notice-text">
        {head}
        {tail ? <span className="notice-tail">{` · ${tail}`}</span> : null}
      </span>
    </div>
  );
}
