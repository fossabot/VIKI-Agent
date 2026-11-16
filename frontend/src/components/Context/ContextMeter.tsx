/**
 * The status bar's context meter: the "context {percent}%" trigger from the design, a disclosure
 * button that opens a popover with the live breakdown. Opening fetches the full report once;
 * the fetch is aborted on close, unmount or a session change, one AbortController per request
 * (ArtifactInspector's own pattern). Escape/outside-click/Tab closing mirrors AppMenu.tsx,
 * trimmed to this panel's single always-present action instead of a row list.
 */
import { Fragment, useEffect, useRef, useState } from "react";
import {
  type ContextApi,
  ContextApiError,
  type ContextReport,
} from "../../clients/conversations/contextApi";
import { Loading } from "../Loading/Loading";
import {
  AUTO_COMPACT_NOT_REPORTED_TEXT,
  AUTO_COMPACT_OFF_TEXT,
  failedReportText,
  readAutoCompact,
} from "./autoCompactRead";
import { ContextBar, contextTone } from "./ContextBar";
import "./ContextMeter.css";

export interface ContextMeterProps {
  /** 0-100, already computed by the caller. */
  percent: number;
  sessionId: string;
  api: ContextApi;
  onOpenPage: () => void;
}

type Status =
  | { kind: "loading" }
  | { kind: "loaded"; report: ContextReport }
  | { kind: "error"; message: string };

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

/** Local zero-padded HH:MM, or a dash placeholder when there is no timestamp to read. */
function timeOf(observedAt: string | null): string {
  if (observedAt === null) return "--:--";
  const at = new Date(observedAt);
  if (Number.isNaN(at.getTime())) return "--:--";
  return `${pad2(at.getHours())}:${pad2(at.getMinutes())}`;
}

const fmt = (n: number): string => n.toLocaleString("en-US");

function errorMessage(error: unknown): string {
  const status = error instanceof ContextApiError ? error.status : null;
  if (status === 409) return "Claude Code is not running for this chat.";
  if (status === 404) return "This chat is not open.";
  return "Could not read the context.";
}

function usedSegments(report: ContextReport): { label: string; tokens: number }[] {
  return (report.categories ?? [])
    .filter((category) => category.kind === "used")
    .map((category) => ({ label: category.name, tokens: category.tokens }));
}

function reservedTokens(report: ContextReport): number {
  return (report.categories ?? [])
    .filter((category) => category.kind === "buffer")
    .reduce((sum, category) => sum + category.tokens, 0);
}

/** The top three used categories, largest first. */
function topUsedCategories(report: ContextReport) {
  const used = (report.categories ?? []).filter((category) => category.kind === "used");
  return [...used].sort((a, b) => b.tokens - a.tokens).slice(0, 3);
}

/** The bar's mark: the same read as the line below, so the two never disagree — shown only once
 * `readAutoCompact` calls the report "on". */
function autoCompactMark(report: ContextReport): number | null {
  const state = readAutoCompact(report.auto_compact);
  return state.status === "on" ? state.threshold : null;
}

function autoCompactLine(report: ContextReport): string {
  const state = readAutoCompact(report.auto_compact);
  if (state.status === "on") return `Auto-compact at ${fmt(state.threshold)} tokens`;
  if (state.status === "off") return AUTO_COMPACT_OFF_TEXT;
  return AUTO_COMPACT_NOT_REPORTED_TEXT;
}

export function ContextMeter({ percent, sessionId, api, onOpenPage }: ContextMeterProps) {
  const [open, setOpen] = useState(false);
  const [status, setStatus] = useState<Status>({ kind: "loading" });
  const wrapRef = useRef<HTMLSpanElement>(null);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const actionRef = useRef<HTMLButtonElement>(null);

  // Fetch exactly once per opening; close, unmount or a session change all run this cleanup,
  // which is the only place the request is aborted.
  useEffect(() => {
    if (!open) return;
    setStatus({ kind: "loading" });
    const controller = new AbortController();
    (async () => {
      try {
        const report = await api.get(sessionId, controller.signal);
        if (!controller.signal.aborted) setStatus({ kind: "loaded", report });
      } catch (error) {
        if (!controller.signal.aborted) {
          setStatus({ kind: "error", message: errorMessage(error) });
        }
      }
    })();
    return () => controller.abort();
  }, [open, sessionId, api]);

  // Escape, outside mousedown and Tab-out, same shape as AppMenu.tsx's own effect: attach while
  // open, move focus onto the panel's one constant control, detach on close.
  useEffect(() => {
    if (!open) return;
    function onKeyDown(event: globalThis.KeyboardEvent): void {
      if (event.isComposing || event.keyCode === 229) return;
      if (event.key === "Escape") {
        event.stopImmediatePropagation();
        setOpen(false);
        toggleRef.current?.focus();
        return;
      }
      if (!wrapRef.current?.contains(event.target as Node)) return;
      if (event.key === "Tab") setOpen(false);
    }
    function onOutside(event: MouseEvent): void {
      if (wrapRef.current && !wrapRef.current.contains(event.target as Node)) setOpen(false);
    }
    // Capture phase: App's own window keydown listener (bubble phase, added once at mount) must
    // never see an Escape this popover already handled — capturing runs window-down-to-target
    // before bubbling runs target-up-to-window, so this fires first regardless of which of the
    // two listeners was added to window first.
    window.addEventListener("keydown", onKeyDown, true);
    window.addEventListener("mousedown", onOutside);
    actionRef.current?.focus();
    return () => {
      window.removeEventListener("keydown", onKeyDown, true);
      window.removeEventListener("mousedown", onOutside);
    };
  }, [open]);

  const loaded = status.kind === "loaded" ? status.report : null;
  const totalOfMax = loaded
    ? `${loaded.total_tokens === null ? "—" : fmt(loaded.total_tokens)} of ${loaded.max_tokens === null ? "—" : fmt(loaded.max_tokens)} tokens`
    : "";
  const topCategories = loaded ? topUsedCategories(loaded) : [];

  return (
    <span className="statusbar-context" ref={wrapRef}>
      <button
        type="button"
        ref={toggleRef}
        className="context-meter"
        aria-haspopup="dialog"
        aria-expanded={open}
        title="Reported by Claude Code after the last reply. Show the breakdown."
        onClick={() => setOpen((shown) => !shown)}
      >
        context {percent}%
        <span className="statusbar-context-track">
          <span className="statusbar-context-fill" style={{ width: `${(percent / 100) * 64}px` }} />
        </span>
      </button>
      {open ? (
        <div role="dialog" aria-label="Context" className="context-popover menu-panel">
          {status.kind === "loading" ? (
            <Loading announce={false}>Reading the context…</Loading>
          ) : null}
          {status.kind === "error" ? <p>{status.message}</p> : null}
          {loaded ? (
            loaded.observed_at === null ? (
              <p>{failedReportText(loaded)}</p>
            ) : (
              <>
                <ContextBar
                  label={totalOfMax}
                  max={loaded.max_tokens ?? 0}
                  used={usedSegments(loaded)}
                  reserved={reservedTokens(loaded)}
                  mark={autoCompactMark(loaded)}
                />
                <p>{totalOfMax}</p>
                <p>{autoCompactLine(loaded)}</p>
                {topCategories.length > 0 ? (
                  <p className="context-popover-categories context-category-colors">
                    {topCategories.map((category, index) => (
                      <Fragment key={category.name}>
                        {index > 0 ? " · " : null}
                        <span
                          className="context-popover-swatch"
                          aria-hidden="true"
                          style={{ background: `var(${contextTone(category.name)})` }}
                        />
                        {`${category.name} ${fmt(category.tokens)}`}
                      </Fragment>
                    ))}
                  </p>
                ) : null}
                <p className="context-popover-meta">{`Reported by Claude Code at ${timeOf(loaded.observed_at)}`}</p>
              </>
            )
          ) : null}
          <button
            type="button"
            ref={actionRef}
            className="btn alt sm"
            onClick={() => {
              onOpenPage();
              setOpen(false);
            }}
          >
            Open Context & loadout
          </button>
        </div>
      ) : null}
    </span>
  );
}
