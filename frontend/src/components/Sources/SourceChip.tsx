/**
 * One source chip: a vault note VIKI read, a folder or file it searched, or a page it fetched.
 * This is a security surface (the chip opens notes and URLs), so every open target is either
 * already known exactly — a note's own `ref`, read straight off the turn — or is looked up
 * through `SourcesEnv.resolve` first; a chip never turns the raw transcript text it displays (a
 * typed URL, a search pattern) into an open target itself. A note chip's hover card lazily loads
 * its peek facts once per chip; a web or search chip calls `resolve` again on every click, since
 * the server, not this component, is the source of truth for where it actually points.
 */
import { type KeyboardEvent, type ReactElement, useEffect, useId, useRef, useState } from "react";
import type { NotePeek } from "../../state/notePeek";
import type { Source } from "../../state/sources";
import { useSourcesEnv } from "./sourcesEnv";

export interface SourceChipProps {
  source: Source;
  sessionId: string;
}

/** Hover/focus delay before the note card opens. */
export const OPEN_DELAY_MS = 250;

/** Mirrors `state/sources.ts`'s `NOTE_PATH`. */
const NOTE_PATH = /\.(md|markdown)$/i;

type PeekState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ready"; data: NotePeek }
  | { status: "error" };

function hostOf(label: string): string {
  try {
    return new URL(label).host;
  } catch {
    return label;
  }
}

/** Appends a dim "by <agent>" bit to an accessible name — omitted for VIKI's own reads (an empty
 * `agent`, straight off `state/sources.ts`). */
function withAgent(name: string, agent: string): string {
  return agent === "" ? name : `${name}, by ${agent}`;
}

/** A chip's visible label, with the reading agent's name as a dim suffix ("<label> · <agent>") —
 * omitted for VIKI's own reads. Every agent's reads are labelled, not only VIKI's own. With
 * an agent, the label alone sits in `.source-chip-label` (the truncating span) and the suffix is
 * a `flex: none` sibling, so an ellipsis on a long label never cuts the agent along with it; with
 * no agent there is nothing to protect, so the label stays a plain text child, as `.source-chip`
 * itself truncates (what SourcesStrip's own tests expect). */
function ChipLabel({ source }: { source: Source }): ReactElement {
  if (source.agent === "") return <>{source.label}</>;
  return (
    <>
      <span className="source-chip-label">{source.label}</span>
      <span className="source-chip-agent"> · {source.agent}</span>
    </>
  );
}

export function SourceChip({ source, sessionId }: SourceChipProps): ReactElement | null {
  const env = useSourcesEnv();
  const tooltipId = useId();
  const [open, setOpen] = useState(false);
  const [peek, setPeek] = useState<PeekState>({ status: "idle" });
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const openTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const peekController = useRef<AbortController | null>(null);
  const resolveController = useRef<AbortController | null>(null);

  // Unmount only: a hide (Esc/leave/blur) keeps any in-flight request alive so a reopen can reuse
  // its result; only the component going away actually abandons it.
  useEffect(
    () => () => {
      if (openTimer.current !== null) clearTimeout(openTimer.current);
      peekController.current?.abort();
      resolveController.current?.abort();
    },
    [],
  );

  if (!env) return null;

  // Arrow expressions, not function declarations: the latter are hoisted and so, as far as the
  // type checker is concerned, callable before the `if (!env) return null;` guard above runs,
  // which would widen `env` back to nullable inside them.
  const startPeek = (): void => {
    if (!env.peek || peek.status !== "idle") return;
    const controller = new AbortController();
    peekController.current = controller;
    setPeek({ status: "loading" });
    env.peek(source.ref, controller.signal).then(
      (data) => {
        if (!controller.signal.aborted) setPeek({ status: "ready", data });
      },
      () => {
        if (!controller.signal.aborted) setPeek({ status: "error" });
      },
    );
  };

  /** Arms the open delay; a no-op without `peek`, since the card would have nothing to show. */
  const scheduleOpen = (): void => {
    if (!env.peek || openTimer.current !== null) return;
    openTimer.current = setTimeout(() => {
      openTimer.current = null;
      setOpen(true);
      startPeek();
    }, OPEN_DELAY_MS);
  };

  const hide = (): void => {
    if (openTimer.current !== null) {
      clearTimeout(openTimer.current);
      openTimer.current = null;
    }
    setOpen(false);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>): void => {
    // Nothing open and nothing pending: there is no card to hide, so let Escape bubble —
    // hands-free and SessionLimitToast have their own document-level listeners for it.
    if (event.key !== "Escape" || (!open && openTimer.current === null)) return;
    // Consumed here, not left to bubble: a global Esc (voice/useHandsFree.ts's own document
    // listener among them) checks defaultPrevented, and stopPropagation keeps it from even
    // reaching a listener that doesn't.
    event.preventDefault();
    event.stopPropagation();
    hide();
  };

  if (source.kind === "note") {
    return (
      <>
        <button
          type="button"
          className="source-chip"
          data-kind="note"
          aria-label={withAgent(`Read note ${source.ref}`, source.agent)}
          aria-describedby={open ? tooltipId : undefined}
          onClick={() => env.openNote(source.ref)}
          onMouseEnter={scheduleOpen}
          onMouseLeave={hide}
          onFocus={scheduleOpen}
          onBlur={hide}
          onKeyDown={onKeyDown}
        >
          <ChipLabel source={source} />
        </button>
        {open && (
          <div id={tooltipId} role="tooltip" className="source-chip-tooltip">
            <code className="source-chip-ref">{source.ref}</code>
            {peek.status === "loading" && <p>Reading…</p>}
            {peek.status === "error" && <p>Couldn't read this note.</p>}
            {peek.status === "ready" && (
              <>
                <p className="source-chip-title">{peek.data.title}</p>
                <p className="source-chip-summary">{peek.data.summary ?? "No summary section."}</p>
                {peek.data.modified !== null && <p>{`Modified ${peek.data.modified}`}</p>}
                {peek.data.confidence !== null && <p>{`Confidence ${peek.data.confidence}`}</p>}
              </>
            )}
          </div>
        )}
      </>
    );
  }

  // web / search: the visible label is the raw transcript text; the exact open target is only
  // ever the one `resolve` hands back, never this label.
  const title = withAgent(
    source.kind === "web"
      ? `Fetched a page on ${hostOf(source.label)}`
      : `Searched ${source.label}`,
    source.agent,
  );

  if (!env.resolve) {
    return (
      <span className="source-chip" data-kind={source.kind} title={title}>
        <ChipLabel source={source} />
      </span>
    );
  }

  const handleClick = async (): Promise<void> => {
    if (busy || !env.resolve) return;
    setBusy(true);
    setStatus(null);
    const controller = new AbortController();
    resolveController.current = controller;
    try {
      const resolved = await env.resolve(sessionId, [source.toolUseId], controller.signal);
      if (controller.signal.aborted) return;
      const entry = resolved.get(source.toolUseId);
      if (source.kind === "web") {
        if (entry?.tool === "WebFetch") env.openUrl(entry.url);
        else setStatus("This page's address isn't available.");
      } else if (!entry || (entry.tool !== "Grep" && entry.tool !== "Glob")) {
        setStatus("The searched folder isn't available.");
      } else if (entry.path_state === "workspace_relative") {
        if (NOTE_PATH.test(entry.path)) env.openNote(entry.path);
        else env.openFolder(entry.path);
      } else if (entry.path_state === "root") {
        env.openFolder("");
      } else {
        setStatus("This search ran outside the vault.");
      }
    } catch {
      if (!controller.signal.aborted) {
        setStatus(
          source.kind === "web"
            ? "This page's address isn't available."
            : "The searched folder isn't available.",
        );
      }
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  };

  return (
    <>
      <button
        type="button"
        className="source-chip"
        data-kind={source.kind}
        aria-label={title}
        aria-busy={busy}
        onClick={() => void handleClick()}
      >
        <ChipLabel source={source} />
      </button>
      {status !== null && <span className="source-chip-status">{status}</span>}
    </>
  );
}
