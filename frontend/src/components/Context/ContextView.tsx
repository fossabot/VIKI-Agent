/**
 * ContextView: the Context & loadout page. Frame shared with AgentsView (back control,
 * a focused `<h1>`, then the open chat's title); below it, either the plain Context report — the
 * ContextBar headline and ContextItems breakdown, read from the contextApi — or, once
 * a caller supplies the `decision` slot (DecisionTab), an AgentsView-style tablist switching
 * between this report and that Decision panel.
 */
import { type ReactElement, type ReactNode, useEffect, useRef, useState } from "react";
import {
  type ContextApi,
  ContextApiError,
  type ContextReport,
} from "../../clients/conversations/contextApi";
import {
  AUTO_COMPACT_NOT_REPORTED_TEXT,
  AUTO_COMPACT_OFF_TEXT,
  failedReportText,
  readAutoCompact,
} from "./autoCompactRead";
import { ContextBar, type ContextBarSegment } from "./ContextBar";
import { ContextItems } from "./ContextItems";
import "./ContextView.css";

export interface ContextViewChat {
  sessionId: string;
  title: string;
}

export interface ContextViewProps {
  api: ContextApi;
  chat: ContextViewChat | null;
  /** A reply is currently running for this chat, so the report below may not reflect it yet. */
  busy: boolean;
  /** Bumped by the caller (e.g. once a reply ends) to silently reload the current session. */
  refreshKey: unknown;
  /** "Back to chat," the way out every non-chat view offers. */
  onBack: () => void;
  renderBack?: (onBack: () => void) => ReactNode;
  /** The DecisionTab. Rendered exactly as given — this view adds no behaviour of its own to
   * it. Its presence turns on the "Context" | "Decision" tablist. */
  decision?: ReactNode;
  /** A fresh value from the caller selects the "Decision" tab, even while this view is open on
   * "Context". Ignored without `decision`; never read back or reset here. */
  decisionRequest?: unknown;
}

type ContextViewTabId = "context" | "decision";

const TABS: readonly { id: ContextViewTabId; label: string }[] = [
  { id: "context", label: "Context" },
  { id: "decision", label: "Decision" },
];

type LoadState =
  | { sessionId: string; report: ContextReport; error: null }
  | { sessionId: string; report: null; error: { status: number; code: string } };

function numberText(value: number | null): string {
  return value === null ? "—" : value.toLocaleString("en-US");
}

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

/** Local HH:MM:SS — the same reading a person glancing at their system clock would get. */
function clockText(iso: string): string {
  const at = new Date(iso);
  return `${pad2(at.getHours())}:${pad2(at.getMinutes())}:${pad2(at.getSeconds())}`;
}

function headline(report: ContextReport): string {
  return `${numberText(report.total_tokens)} of ${numberText(report.max_tokens)} tokens (${numberText(report.percentage)}%)`;
}

/** Non-empty "used" categories only — ContextBar's own contract: deferred, buffer and free
 * kinds never go in `used`; buffer goes through `reserved` instead, below. */
function usedSegments(report: ContextReport): ContextBarSegment[] {
  return (report.categories ?? [])
    .filter((category) => category.kind === "used" && category.tokens > 0)
    .map((category) => ({ label: category.name, tokens: category.tokens }));
}

function reservedTokens(report: ContextReport): number | null {
  if (report.categories === null) return null;
  const buffer = report.categories.filter((category) => category.kind === "buffer");
  return buffer.length === 0 ? null : buffer.reduce((sum, category) => sum + category.tokens, 0);
}

function Report({ report, busy }: { report: ContextReport; busy: boolean }): ReactElement {
  const autoCompact = report.auto_compact;
  const autoCompactRead = readAutoCompact(autoCompact);
  const mark = autoCompactRead.status === "on" ? autoCompactRead.threshold : null;
  return (
    <div className="context-view-report">
      {report.observed_at === null ? (
        <p>{failedReportText(report)}</p>
      ) : (
        <>
          <ContextBar
            size="large"
            used={usedSegments(report)}
            max={report.max_tokens ?? 0}
            reserved={reservedTokens(report)}
            mark={mark}
            label={headline(report)}
          />
          <p>{headline(report)}</p>
          {report.raw_max_tokens !== report.max_tokens ? (
            <p>
              Effective window {numberText(report.max_tokens)} of the model's{" "}
              {numberText(report.raw_max_tokens)} tokens
            </p>
          ) : null}
          {autoCompactRead.status === "on" ? (
            <p>
              Auto-compact at {numberText(autoCompactRead.threshold)} tokens (source:{" "}
              {autoCompact?.source ?? "—"})
            </p>
          ) : autoCompactRead.status === "off" ? (
            <p>{AUTO_COMPACT_OFF_TEXT}</p>
          ) : (
            <p>{AUTO_COMPACT_NOT_REPORTED_TEXT}</p>
          )}
          {report.observed_at !== null ? (
            <p>Reported by Claude Code at {clockText(report.observed_at)}</p>
          ) : null}
        </>
      )}
      {busy ? (
        <p className="context-view-busy">
          <span className="context-view-busy-dot breathe" aria-hidden="true" />A reply is running;
          this updates when it ends.
        </p>
      ) : null}
      <ContextItems report={report} />
    </div>
  );
}

/** The Context tab's whole content: exactly one of the four named states, or the Report. A
 * session switch in flight (state still belongs to the previous sessionId) renders nothing yet,
 * the same gate ProfileSessionReview uses — only a same-session refresh keeps showing stale
 * content while it reloads. */
function ContextPanel({
  chat,
  state,
  busy,
  onRetry,
}: {
  chat: ContextViewChat | null;
  state: LoadState | null;
  busy: boolean;
  onRetry: () => void;
}): ReactElement | null {
  if (chat === null) return <p>Open a chat to see its context.</p>;
  if (state === null || state.sessionId !== chat.sessionId) return null;
  if (state.error !== null) {
    if (state.error.status === 409)
      return (
        <p>
          Claude Code is not running for this chat yet. Send a message; the breakdown appears after
          the reply.
        </p>
      );
    if (state.error.status === 404) return <p>This chat is not open in this window.</p>;
    return (
      <>
        <p>Could not read the context.</p>
        <button type="button" onClick={onRetry}>
          Try again
        </button>
      </>
    );
  }
  return <Report report={state.report} busy={busy} />;
}

export function ContextView({
  api,
  chat,
  busy,
  refreshKey,
  onBack,
  renderBack,
  decision,
  decisionRequest,
}: ContextViewProps): ReactElement {
  const headingRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    headingRef.current?.focus();
  }, []);

  const [tab, setTab] = useState<ContextViewTabId>("context");
  // A fresh `decisionRequest` selects "Decision" on the first render of a mount, since
  // effects run once on mount regardless of a dependency "change," and again on any later
  // request while this view stays open.
  useEffect(() => {
    if (decisionRequest !== undefined) setTab("decision");
  }, [decisionRequest]);
  const [state, setState] = useState<LoadState | null>(null);
  const [retry, setRetry] = useState(0);
  const sessionId = chat?.sessionId ?? null;

  // biome-ignore lint/correctness/useExhaustiveDependencies(refreshKey): a parent-driven reload trigger, not read in the body.
  // biome-ignore lint/correctness/useExhaustiveDependencies(retry): the Try again button's reload trigger, not read in the body.
  useEffect(() => {
    if (sessionId === null) return;
    const controller = new AbortController();
    api.get(sessionId, controller.signal).then(
      (report) => {
        if (controller.signal.aborted) return;
        setState({ sessionId, report, error: null });
      },
      (error: unknown) => {
        if (controller.signal.aborted) return;
        setState({
          sessionId,
          report: null,
          error: {
            status: error instanceof ContextApiError ? error.status : 0,
            code: error instanceof ContextApiError ? error.code : "unavailable",
          },
        });
      },
    );
    return () => controller.abort();
  }, [api, sessionId, refreshKey, retry]);

  const showTabs = decision !== undefined;
  const panelContent =
    showTabs && tab === "decision" ? (
      decision
    ) : (
      <ContextPanel
        chat={chat}
        state={state}
        busy={busy}
        onRetry={() => setRetry((value) => value + 1)}
      />
    );

  return (
    <div className="context-view">
      {renderBack?.(onBack) ?? (
        <button type="button" className="btn txt sm" onClick={onBack}>
          Back to chat
        </button>
      )}
      <h1 ref={headingRef} tabIndex={-1}>
        Context & loadout
      </h1>
      {chat !== null ? <p className="context-view-chat-title">{chat.title}</p> : null}
      {showTabs ? (
        <div className="context-view-tabs" role="tablist" aria-label="Context & loadout">
          {TABS.map((item) => (
            <button
              key={item.id}
              type="button"
              id={`context-view-tab-${item.id}`}
              role="tab"
              aria-selected={tab === item.id}
              aria-controls="context-view-panel"
              onClick={() => setTab(item.id)}
            >
              {item.label}
            </button>
          ))}
        </div>
      ) : null}
      {showTabs ? (
        <div id="context-view-panel" role="tabpanel" aria-labelledby={`context-view-tab-${tab}`}>
          {panelContent}
        </div>
      ) : (
        <div id="context-view-panel">{panelContent}</div>
      )}
    </div>
  );
}
