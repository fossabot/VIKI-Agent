/**
 * RunPeek: a read-only, polling view of one subagent run's own transcript — its task, the lines
 * it has written so far, and, once it is no longer running, its result. It replaces the runs
 * list when a row's Peek is opened; Back returns to the list.
 *
 * Polling: the run's own transcript is a whole-file read (src/clients/conversations/subagentsApi.ts), so this component
 * keeps at most one such read in flight. It reads once on mount, and again `pollMs` after each
 * settle: a read issued while the run was still "running" is always followed by one more read,
 * even if the run finished while that read was in flight, so Result normally comes from a read
 * made after the run's end; if that one read fails, the transcript already on screen stays in
 * place under the paused note below (see renderBody) rather than being cleared. The first read
 * issued once the row is no longer running schedules no further read, so polling stops for good
 * once that happens. `row` is read through a ref rather than a dependency, so a parent re-render
 * that only updates `row` (the common case, as the underlying session data changes) does not
 * restart or abort the loop; the loop simply sees the fresh value the next time it runs.
 *
 * Untrusted text: the prompt, tool text and every line's own text come from the run's own
 * transcript and are rendered as plain text nodes only — no Markdown, link detection or raw HTML
 * — so nothing a transcript contains can inject markup.
 *
 * Stop and answers: `stop` (the caller's own `useRunStops` ask/cancel/send for this row) renders
 * `RunStop` right after "Jump to chat"; `requests` (set by the caller only once it has an
 * `onDecide`) renders a `section.run-peek-requests` labelled "Waiting for you" with one
 * `RequestActions` per `kind: "tool"` item among the row's own pending requests, every one
 * sharing the row's single `waitingSince`. Both are omitted, and their controls stay out of the
 * page entirely, without them. That section takes focus itself (`tabIndex={-1}`): when a request
 * goes while focus is inside it, RequestActions puts focus there, on the list, and not on the
 * next request's Allow once, which a key already on its way down would press. `liveRunning`
 * below is the one place that decides whether a row counts as running — an `ended` row (its
 * session closed while the block was still "running") never does, for the header's `AgentFace`,
 * the polling loop, and the Result section alike.
 */
import { type ReactElement, useEffect, useRef, useState } from "react";
import type {
  PeekLine,
  RunTranscript,
  SubagentsApi,
} from "../../clients/conversations/subagentsApi";
import type { PermissionRequestedData } from "../../protocol";
import { timeLabel } from "../../state/rows";
import { type RunRow, runCounts } from "../../state/runGroups";
import { AgentFace } from "../Face/AgentFace";
import { Loading } from "../Loading/Loading";
import { type AnswerOutcome, RequestActions } from "./RequestActions";
import { RunStop, type StopPhase } from "./RunStop";
import "./RunPeek.css";

export const PEEK_POLL_MS = 2000;

export interface RunPeekProps {
  row: RunRow;
  chat: string;
  api: SubagentsApi;
  nowMs: number;
  onBack: () => void;
  onJump: () => void;
  pollMs?: number;
  stop?:
    | {
        phase: StopPhase | undefined;
        onAsk: () => void;
        onCancel: () => void;
        onSend: () => void;
      }
    | undefined;
  requests?:
    | {
        items: readonly PermissionRequestedData[];
        waitingSince: number | null;
        onDecide(toolUseId: string, allow: boolean): Promise<AnswerOutcome>;
      }
    | undefined;
}

/** A callback ref, module-level so it keeps one identity across renders: React calls it only
 * when the Back button mounts, not on every render — which is what "focused on mount" means. */
function focusOnMount(element: HTMLButtonElement | null): void {
  element?.focus();
}

function errorCode(error: unknown): string {
  return error instanceof Error ? error.message : "unavailable";
}

/** The one place that decides whether a row counts as running (see the module doc's "Stop and
 * answers"): an `ended` row never does, even while its own `block.status` still reads "running". */
function liveRunning(row: RunRow): boolean {
  return row.block.status === "running" && !row.ended;
}

/** One transcript line, worded by its kind; text nodes only (see the module doc above). */
function renderLine(line: PeekLine): ReactElement {
  switch (line.kind) {
    case "text":
      return (
        <p key={line.id} className="run-peek-text">
          {line.text}
        </p>
      );
    case "message":
      return (
        <p key={line.id} className="run-peek-message">
          {line.text}
        </p>
      );
    case "agent":
      return (
        <p key={line.id} className="run-peek-agent">
          {`▸ ${line.agent} — ${line.description} · ${line.status}`}
        </p>
      );
    case "tool": {
      const parts: ReactElement[] = [
        <span key="head" className="tool-head">
          {line.head}
        </span>,
      ];
      if (line.rest) {
        parts.push(
          <span key="rest" className="run-peek-tool-rest">
            {` ${line.rest}`}
          </span>,
        );
      }
      if (line.tail) {
        parts.push(
          <span key="tail" className="run-peek-tool-tail">
            {` ${line.tail}`}
          </span>,
        );
      }
      if (line.error) {
        parts.push(
          <span key="error" className="run-peek-tool-error">
            {" ✗"}
          </span>,
        );
      }
      if (!line.done) {
        parts.push(
          <span key="pending" className="run-peek-tool-pending">
            {" …"}
          </span>,
        );
      }
      return (
        <p key={line.id} className="run-peek-tool mono">
          {parts}
        </p>
      );
    }
    default: {
      const exhaustive: never = line;
      return exhaustive;
    }
  }
}

/** The right rail's own look at one run's transcript: task, lines, and, once it is finished, the
 * result — re-read while the run is active (see the module doc for the polling rule). */
export function RunPeek({
  row,
  chat,
  api,
  nowMs,
  onBack,
  onJump,
  pollMs = PEEK_POLL_MS,
  stop,
  requests,
}: RunPeekProps): ReactElement {
  const [transcript, setTranscript] = useState<RunTranscript | null>(null);
  const [lastError, setLastError] = useState<string | null>(null);
  const rowRef = useRef(row);
  rowRef.current = row;

  useEffect(() => {
    let stopped = false;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;

    function run(): void {
      if (stopped) return;
      const current = rowRef.current;
      // Captured at issue time: a run that finishes while this very read is in flight must still
      // get one more read afterwards (see the module doc), so settling alone can't decide this.
      const issuedRunning = liveRunning(current);
      api.read(current.sessionId, current.block.id, controller.signal).then(
        (result) => {
          if (stopped) return;
          setTranscript(result);
          setLastError(null);
          if (issuedRunning || liveRunning(rowRef.current)) {
            timer = setTimeout(run, pollMs);
          }
        },
        (error: unknown) => {
          if (stopped) return;
          setLastError(errorCode(error));
          if (issuedRunning || liveRunning(rowRef.current)) {
            timer = setTimeout(run, pollMs);
          }
        },
      );
    }

    run();

    return () => {
      stopped = true;
      controller.abort();
      if (timer !== undefined) clearTimeout(timer);
    };
    // row is read through rowRef on purpose (see the module doc): only a new api or poll
    // interval should restart this loop, never a row update by itself.
  }, [api, pollMs]);

  const agent = row.block.agent;
  const running = liveRunning(row);
  const toolRequests = requests?.items.filter((item) => item.kind === "tool") ?? [];
  const startedLabel =
    transcript !== null && transcript.startedAt !== null
      ? timeLabel(new Date(transcript.startedAt).toISOString(), nowMs)
      : "";
  const counts = runCounts(row, transcript ?? undefined, nowMs);
  const metaParts = [chat];
  if (startedLabel) metaParts.push(`started ${startedLabel}`);
  if (counts) metaParts.push(counts);

  function renderBody(): ReactElement {
    if (transcript === null) {
      if (lastError === null)
        return (
          <Loading className="run-peek-note" announce={false}>
            Reading the transcript…
          </Loading>
        );
      if (lastError === "not_found") {
        return (
          <p className="run-peek-note">
            {running ? "No transcript for this run yet." : "This run's transcript was not found."}
          </p>
        );
      }
      return <p className="run-peek-note">Couldn't read this run's transcript.</p>;
    }

    let lastText: (PeekLine & { kind: "text" }) | undefined;
    for (const line of transcript.lines) {
      if (line.kind === "text") lastText = line;
    }

    return (
      <>
        {transcript.prompt !== "" ? (
          <details className="run-peek-task">
            <summary>Task</summary>
            <p className="run-peek-text">{transcript.prompt}</p>
          </details>
        ) : null}
        {transcript.partial ? (
          <p className="run-peek-note">Showing the newest part of a long transcript.</p>
        ) : null}
        {transcript.omitted > 0 ? (
          <p className="run-peek-note">{`${transcript.omitted} earlier entries are not shown.`}</p>
        ) : null}
        {transcript.lines.map(renderLine)}
        {!running && lastText ? (
          <div className="run-peek-result">
            <p className="run-peek-result-head">Result</p>
            <p className="run-peek-text">{lastText.text}</p>
          </div>
        ) : null}
        {lastError !== null ? (
          <p className="run-peek-note">Live updates paused: the transcript could not be read.</p>
        ) : null}
      </>
    );
  }

  return (
    <section className="run-peek" aria-label={`Peek: ${agent}`}>
      <header className="run-peek-header">
        <button type="button" className="run-peek-back" ref={focusOnMount} onClick={onBack}>
          Back
        </button>
        <AgentFace name={agent} size={18} active={running} />
        <div className="run-peek-id">
          <p className="run-peek-title">{`${agent} · ${row.block.model}`}</p>
          <p className="run-peek-meta">{metaParts.join(" · ")}</p>
        </div>
        <button type="button" className="run-peek-jump" onClick={onJump}>
          Jump to chat
        </button>
        {stop ? (
          <RunStop
            agent={agent}
            chat={chat}
            phase={stop.phase}
            nowMs={nowMs}
            onAsk={stop.onAsk}
            onCancel={stop.onCancel}
            onSend={stop.onSend}
          />
        ) : null}
      </header>
      {requests && toolRequests.length > 0 ? (
        <section className="run-peek-requests" aria-label="Waiting for you" tabIndex={-1}>
          {toolRequests.map((item) => (
            <RequestActions
              key={item.tool_use_id}
              request={item}
              waitingSince={requests.waitingSince}
              nowMs={nowMs}
              onDecide={(allow) => requests.onDecide(item.tool_use_id, allow)}
            />
          ))}
        </section>
      ) : null}
      <div className="run-peek-body">{renderBody()}</div>
    </section>
  );
}
