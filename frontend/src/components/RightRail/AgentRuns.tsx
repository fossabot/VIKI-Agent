/**
 * AgentRuns: the right rail's run list — every subagent run of the open chats, grouped by what
 * it needs from the user right now (state/runGroups.ts computes the groups and words each row;
 * this component only renders them). Needs you, Working and Ready for review show only while
 * they hold a row; Done folds under a `<details>` whose open state this component holds (closed
 * by default), newest first within each chat, chats in the reverse of the order this window
 * first added them — so the fold survives Peek replacing and then restoring the groups.
 *
 * Clicking a row's face and text opens Peek (RunPeek.tsx) in place of the groups; Back returns
 * and refocuses the row that opened it, opening the Done fold first when reviewing moved that
 * row straight into it (see "Reviewing" below) — otherwise the row Back focuses would sit inside
 * a closed fold. The same return-and-refocus happens on its own if the peeked run's key
 * disappears from the groups entirely (its session closed while Peek was open) — there is then
 * nothing left to show. Without a `SubagentsApi` a row's face and text sit in a plain,
 * unclickable `<div>` instead of a `<button>`: there is nothing Peek could read.
 *
 * Reviewing: Peek opened on a row already in Ready for review, or a peeked row that reaches that
 * group while Peek stays open, marks it reviewed right away — once the user has looked, listing
 * it as needing a look again would be noise. Jump marks a finished row reviewed too, the same as
 * looking at it, but only once the jump itself goes ahead — one the app refuses marks nothing;
 * a still-running row is never marked just for being jumped to.
 *
 * A run row (of any kind of agent, not only a roster one) carries no `.agent`, `.s`, `.s-on`,
 * `.q`, `.rt` or `.chip` class and no `data-agent`: the roster's own frozen contract
 * (components/LeftRail/AgentsSection.tsx) and App.test.tsx's here-now count tests both count
 * exactly those, and this list must add to neither.
 *
 * Stop and answers: a running row (needs or working, never an `ended` one) shows its own
 * `RunStop` after Jump once `runs` is given, keyed by `useRunStops` with `row.key` — the caller's
 * own `RunStops`, via the optional `stops` prop (App shares one over all groups with
 * Needs you too, so a run Stopped from either keeps its phase in the other), when given, else
 * this component's own hook, so it also works on its own. Peek gets the same
 * control after "Jump to chat", plus a `requests` section offering Allow once/Deny for its own
 * run's pending tool requests once `onDecide` is given; either prop left out keeps the matching
 * control out of both places. An `ended` row is never treated as running here or in Peek: its
 * face, Peek's own polling loop and Peek's Result section all share one check.
 */
import { type ReactElement, type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import type { RunsApi } from "../../clients/conversations/runsApi";
import type { SubagentsApi } from "../../clients/conversations/subagentsApi";
import type { PermissionRequestedData } from "../../protocol";
import {
  groupRuns,
  type RunRequests,
  type RunRow,
  runCounts,
  runLine,
  type SessionNeed,
} from "../../state/runGroups";
import type { SessionView } from "../../state/view";
import { visibleLine } from "../Cards/visibleText";
import { AgentFace } from "../Face/AgentFace";
import type { AnswerOutcome } from "./RequestActions";
import { RunPeek } from "./RunPeek";
import { RunStop, type RunStops, useRunStops } from "./RunStop";
import { useRunStats } from "./runStats";
import "./AgentRuns.css";

export interface AgentRunsProps {
  sessions: Readonly<Record<string, SessionView>>;
  selectedId: string | null;
  labelOf: (sessionId: string) => string;
  nowMs: number;
  reviewed: ReadonlySet<string>;
  onReviewed: (keys: readonly string[]) => void;
  onJump: (sessionId: string, blockId: string | null) => boolean;
  api?: SubagentsApi | undefined;
  hideNeeds?: boolean;
  runs?: RunsApi | undefined;
  /** The caller's own `useRunStops`, shared with another list of the same runs — Needs
   * you, in App's own wiring — so a run Stopped from one keeps its phase in the other. Omitted:
   * this component builds its own, over its own running rows. */
  stops?: RunStops | undefined;
  requests?: RunRequests | undefined;
  onDecide?(sessionId: string, toolUseId: string, allow: boolean): Promise<AnswerOutcome>;
}

/** The sole agent name a chat-level need's requests name, unless they disagree or it is only
 * ever the generic "subagent" fallback — neither tells the user anything a real run name would. */
function needAgent(requests: readonly PermissionRequestedData[]): string | null {
  const names = new Set(requests.map((request) => request.agent));
  if (names.size !== 1) return null;
  const [name] = names;
  return name !== undefined && name !== "subagent" ? name : null;
}

export function AgentRuns({
  sessions,
  selectedId,
  labelOf,
  nowMs,
  reviewed,
  onReviewed,
  onJump,
  api,
  hideNeeds,
  runs,
  stops,
  requests,
  onDecide,
}: AgentRunsProps): ReactElement {
  const groups = useMemo(
    () => groupRuns(sessions, reviewed, requests),
    [sessions, reviewed, requests],
  );
  const [tick, setTick] = useState<number>(() => Date.now());
  const active = groups.needs.length > 0 || groups.working.length > 0;

  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => setTick(Date.now()), 1000);
    return () => clearInterval(id);
  }, [active]);

  const now = Math.max(nowMs, tick);
  const runningRows = [...groups.needs, ...groups.working];
  const statsInputs = runningRows.map((row) => ({
    key: row.key,
    sessionId: row.sessionId,
    blockId: row.block.id,
  }));
  const stats = useRunStats(api, statsInputs, now);
  // This component's own phase machine, used only without the caller's shared one (`stops` the
  // prop) — see the module doc and AgentRunsProps' own comment.
  const ownStops = useRunStops(runs, new Set(runningRows.map((row) => row.key)));
  const runStops = stops ?? ownStops;

  const [peekKey, setPeekKey] = useState<string | null>(null);
  const [doneOpen, setDoneOpen] = useState(false);
  const runRefs = useRef(new Map<string, HTMLButtonElement>());
  const focusKeyRef = useRef<string | null>(null);

  const peekedRow =
    peekKey === null
      ? undefined
      : (groups.needs.find((row) => row.key === peekKey) ??
        groups.working.find((row) => row.key === peekKey) ??
        groups.review.find((row) => row.key === peekKey) ??
        groups.done.find((row) => row.key === peekKey));

  // The peeked run left the groups entirely (its session closed): there is nothing left to show,
  // so this returns to the list on its own, the same as Back — see the module doc.
  useEffect(() => {
    if (peekKey !== null && peekedRow === undefined) {
      focusKeyRef.current = peekKey;
      setPeekKey(null);
    }
  }, [peekKey, peekedRow]);

  // Peek stands in for having looked at the run: this fires the moment the peeked row is one
  // Ready for review lists, whether that was already true when Peek opened or only becomes true
  // while it stays open.
  useEffect(() => {
    if (peekKey !== null && groups.review.some((row) => row.key === peekKey)) {
      onReviewed([peekKey]);
    }
  }, [groups, peekKey, onReviewed]);

  useEffect(() => {
    if (peekKey === null && focusKeyRef.current !== null) {
      runRefs.current.get(focusKeyRef.current)?.focus();
      focusKeyRef.current = null;
    }
  }, [peekKey]);

  function chatLabel(sessionId: string): string {
    return sessionId === selectedId ? "this chat" : labelOf(sessionId);
  }

  function handleJump(row: RunRow): void {
    const jumped = onJump(row.sessionId, row.block.id);
    if (jumped && row.block.status !== "running") onReviewed([row.key]);
  }

  function handleBack(): void {
    focusKeyRef.current = peekKey;
    // Reviewing (see the module doc) may have moved this row straight into Done while Peek was
    // open; open the fold now so the focus set above lands on a row that is actually visible.
    if (peekKey !== null && groups.done.some((row) => row.key === peekKey)) setDoneOpen(true);
    setPeekKey(null);
  }

  function renderRun(row: RunRow): ReactElement {
    const running = row.block.status === "running" && !row.ended;
    const chat = chatLabel(row.sessionId);
    const line1 = [row.block.agent, row.block.description]
      .filter((part) => part !== "")
      .join(" · ");
    const line2 = [chat, runCounts(row, stats.get(row.key), now)]
      .filter((part) => part !== "")
      .join(" · ");
    const line = runLine(row);
    // A row waiting on a request names the command that request would run, in the request's own
    // words: hidden characters are written as their codes and line breaks drawn (visibleText.ts).
    const line3 = !row.ended && row.requests.length > 0 ? visibleLine(line) : line;
    const body: ReactNode = (
      <>
        <AgentFace name={row.block.agent} size={18} active={running} />
        <span className="run-body">
          <span className="run-line">{line1}</span>
          <span className="run-line">{line2}</span>
          {line3 !== "" ? <span className="run-line">{line3}</span> : null}
        </span>
      </>
    );
    return (
      <div className="run" data-run-key={row.key} key={row.key}>
        {api ? (
          <button
            type="button"
            className="run-open"
            title={line3 !== "" ? line3 : undefined}
            onClick={() => setPeekKey(row.key)}
            ref={(el) => {
              if (el) runRefs.current.set(row.key, el);
              else runRefs.current.delete(row.key);
            }}
          >
            {body}
          </button>
        ) : (
          <div className="run-open" title={line3 !== "" ? line3 : undefined}>
            {body}
          </div>
        )}
        <button
          type="button"
          className="run-jump"
          aria-label={`Jump to ${row.block.agent} in ${chat}`}
          onClick={() => handleJump(row)}
        >
          Jump
        </button>
        {running && runs ? (
          <RunStop
            agent={row.block.agent}
            chat={chat}
            phase={runStops.phase(row.key)}
            nowMs={now}
            onAsk={() => runStops.ask(row.key)}
            onCancel={() => runStops.cancel(row.key)}
            onSend={() => runStops.send(row.key, row.sessionId, row.block.id)}
          />
        ) : null}
      </div>
    );
  }

  function renderNeed(need: SessionNeed): ReactElement {
    const agent = needAgent(need.requests);
    const message = `A subagent is waiting for approval${agent ? ` · ${agent}` : ""}`;
    return (
      <div className="run run-need" key={`need:${need.sessionId}`}>
        <span className="run-body">
          <span className="run-line">{chatLabel(need.sessionId)}</span>
          <span className="run-line">{message}</span>
        </span>
        <button type="button" className="run-jump" onClick={() => onJump(need.sessionId, null)}>
          Jump
        </button>
      </div>
    );
  }

  if (peekedRow !== undefined && api) {
    const peekedRunning = peekedRow.block.status === "running" && !peekedRow.ended;
    return (
      <div className="agent-runs">
        <RunPeek
          row={peekedRow}
          chat={chatLabel(peekedRow.sessionId)}
          api={api}
          nowMs={now}
          onBack={handleBack}
          onJump={() => handleJump(peekedRow)}
          stop={
            runs && peekedRunning
              ? {
                  phase: runStops.phase(peekedRow.key),
                  onAsk: () => runStops.ask(peekedRow.key),
                  onCancel: () => runStops.cancel(peekedRow.key),
                  onSend: () =>
                    runStops.send(peekedRow.key, peekedRow.sessionId, peekedRow.block.id),
                }
              : undefined
          }
          requests={
            onDecide
              ? {
                  items: peekedRow.requests,
                  waitingSince: peekedRow.waitingSince,
                  onDecide: (toolUseId, allow) => onDecide(peekedRow.sessionId, toolUseId, allow),
                }
              : undefined
          }
        />
      </div>
    );
  }

  const needCount = groups.needs.length + groups.unattributed.length;
  const empty =
    needCount === 0 &&
    groups.working.length === 0 &&
    groups.review.length === 0 &&
    groups.done.length === 0;

  return (
    <div className="agent-runs">
      {empty ? (
        <p className="runs-empty">No agent runs in open chats.</p>
      ) : (
        <>
          {needCount > 0 && !hideNeeds ? (
            <div className="run-group">
              <div className="run-group-head">
                <span className="lbl">{`Needs you (${needCount})`}</span>
              </div>
              {groups.needs.map(renderRun)}
              {groups.unattributed.map(renderNeed)}
            </div>
          ) : null}
          {groups.working.length > 0 ? (
            <div className="run-group">
              <div className="run-group-head">
                <span className="lbl">{`Working (${groups.working.length})`}</span>
              </div>
              {groups.working.map(renderRun)}
            </div>
          ) : null}
          {groups.review.length > 0 ? (
            <div className="run-group">
              <div className="run-group-head">
                <span className="lbl">{`Ready for review (${groups.review.length})`}</span>
                <button
                  type="button"
                  className="run-mark"
                  onClick={() => onReviewed(groups.review.map((row) => row.key))}
                >
                  Mark reviewed
                </button>
              </div>
              {groups.review.map(renderRun)}
            </div>
          ) : null}
          {groups.done.length > 0 ? (
            <details
              className="run-done"
              open={doneOpen}
              onToggle={(event) => setDoneOpen(event.currentTarget.open)}
            >
              <summary className="lbl">{`Done (${groups.done.length})`}</summary>
              {groups.done.map(renderRun)}
            </details>
          ) : null}
        </>
      )}
    </div>
  );
}
