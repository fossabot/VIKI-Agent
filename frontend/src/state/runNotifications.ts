/**
 * Desktop notifications for an open request or a finished run, raised while the user is not
 * looking. Shares its on/off switch and quiet hours with `useRoutineNotifications`
 * (`routines.controls.notify`); only the signal source differs — the run groups and the open
 * sessions' cards, not the routines snapshot.
 *
 * Rules:
 * - A card (every open card of every session outside `exclude`, keyed `${sessionId}:${tool_use_id}`)
 *   gets its body from, in order: a permission card joined to a `groups.needs` row (that row's own
 *   agent, plus its block id); the generic `"subagent"` fallback; any other named agent; a
 *   main-thread (`agent === ""`) tool card; a main-thread plan card; a question. See `cardNotice`.
 * - A run is tracked while it is `live` and not `ended` (a row whose session closed mid-run: its
 *   CLI is gone, it will never report a real end). `stopped` and history rows are never tracked.
 * - `runNotices` fires for a card key that is new in a session `runSignals` already knew (a
 *   session seen for the first time is a first sighting, same as a run's own baseline), and for a
 *   tracked run that was `running` and is now `completed` or `failed`. A session only counts as
 *   known once its view has synced past `openSession`'s placeholder (`generation !== null`) — the
 *   placeholder's own absence of cards must never be read as a baseline a first real snapshot's
 *   cards then notify against.
 * - A card notice's click re-resolves its block id against the freshest groups, by the triggering
 *   card's own `tool_use_id` — `useRunRequests`' read may still be in flight when the notice fires
 *   (two same-type runs read as unattributed until it lands), so the id baked in at that moment
 *   can go stale before the user clicks. A run notice needs no such re-check: its block id comes
 *   straight from the row, never from that async join.
 * - The hook follows `useRoutineNotifications`: the first pass is silent; disabled, no
 *   permission, quiet hours or a focused page drop the notices, and the baseline still moves on.
 */
import { useEffect, useRef, useState } from "react";
import { tf } from "../i18n/t";
import { inQuietHours, type Notifier } from "./routineNotifications";
import type { RunGroups, RunRow } from "./runGroups";
import type { Card, SessionView } from "./view";

export const NOTICE_TITLE = "VIKI Agent";

interface CardSignal {
  readonly body: string;
  readonly sessionId: string;
  readonly blockId: string | null;
  /** The triggering card's own `tool_use_id`, so a click can re-resolve `blockId` against
   * fresher groups than the one this signal was built from. */
  readonly toolUseId: string;
}

interface RunSignal {
  readonly sessionId: string;
  readonly blockId: string;
  readonly agent: string;
  readonly status: RunRow["block"]["status"];
}

export interface RunSignals {
  readonly sessions: ReadonlySet<string>;
  readonly cards: ReadonlyMap<string, CardSignal>;
  readonly runs: ReadonlyMap<string, RunSignal>;
}

export interface RunNotice {
  readonly tag: string;
  readonly body: string;
  readonly sessionId: string;
  readonly blockId: string | null;
  /** The card this notice named, for a click to re-resolve `blockId` by; null for a run notice
   * (finished/failed), whose `blockId` is already reliable — see the module doc. */
  readonly toolUseId: string | null;
}

/** `groups.needs`' own row for an open card attributed to it, by session and `tool_use_id` — the
 * one join `cardNotice` makes at signal time and a click remakes against fresher groups. */
function needsRowFor(groups: RunGroups, sessionId: string, toolUseId: string): RunRow | undefined {
  return groups.needs.find(
    (candidate) =>
      candidate.sessionId === sessionId &&
      candidate.requests.some((request) => request.tool_use_id === toolUseId),
  );
}

/** The body and block id for one open card. See the module doc for the branch order. */
function cardNotice(
  sessionId: string,
  card: Card,
  groups: RunGroups,
  assistantName: string,
): Pick<CardSignal, "body" | "blockId"> {
  if (card.kind === "question") {
    return { body: `${assistantName} has a question for you`, blockId: null };
  }
  const { data } = card;
  if (data.agent === "") {
    return data.kind === "plan"
      ? { body: `${assistantName} has a plan for you to approve`, blockId: null }
      : { body: `${assistantName} needs your approval`, blockId: null };
  }
  const row = needsRowFor(groups, sessionId, data.tool_use_id);
  if (row) return { body: `${row.block.agent} needs your approval`, blockId: row.block.id };
  return {
    body:
      data.agent === "subagent"
        ? "An agent needs your approval"
        : `${data.agent} needs your approval`,
    blockId: null,
  };
}

/** Every open card and every live, not-yet-`ended` run of the sessions outside `exclude` — a
 * snapshot to diff against the next one with `runNotices`. */
export function runSignals(
  groups: RunGroups,
  sessions: Readonly<Record<string, SessionView>>,
  exclude: ReadonlySet<string>,
  assistantName: string,
): RunSignals {
  // A session not yet past its `openSession` placeholder (view.generation === null) is not seen
  // yet: its own absence of cards is not a baseline, or its first real snapshot's cards would
  // read as new and notify.
  const sessionIds = new Set(
    Object.entries(sessions)
      .filter(([sessionId, view]) => !exclude.has(sessionId) && view.generation !== null)
      .map(([sessionId]) => sessionId),
  );

  const cards = new Map<string, CardSignal>();
  for (const [sessionId, view] of Object.entries(sessions)) {
    if (exclude.has(sessionId)) continue;
    for (const card of view.cards) {
      const notice = cardNotice(sessionId, card, groups, assistantName);
      cards.set(`${sessionId}:${card.data.tool_use_id}`, {
        ...notice,
        sessionId,
        toolUseId: card.data.tool_use_id,
      });
    }
  }

  const runs = new Map<string, RunSignal>();
  for (const row of [...groups.needs, ...groups.working, ...groups.review, ...groups.done]) {
    if (exclude.has(row.sessionId) || !row.live || row.ended) continue;
    runs.set(row.key, {
      sessionId: row.sessionId,
      blockId: row.block.id,
      agent: row.block.agent,
      status: row.block.status,
    });
  }

  return { sessions: sessionIds, cards, runs };
}

/** What changed from `prev` to `next`: see the module doc for exactly which cards and runs. */
export function runNotices(prev: RunSignals, next: RunSignals): RunNotice[] {
  const notices: RunNotice[] = [];
  for (const [key, card] of next.cards) {
    if (prev.cards.has(key) || !prev.sessions.has(card.sessionId)) continue;
    notices.push({
      tag: `viki-card-${key}`,
      body: card.body,
      sessionId: card.sessionId,
      blockId: card.blockId,
      toolUseId: card.toolUseId,
    });
  }
  for (const [key, run] of next.runs) {
    if (prev.runs.get(key)?.status !== "running") continue;
    if (run.status !== "completed" && run.status !== "failed") continue;
    notices.push({
      tag: `viki-run-${key}-${run.status}`,
      body: `${run.agent} ${run.status === "completed" ? "finished" : "failed"}`,
      sessionId: run.sessionId,
      blockId: run.blockId,
      toolUseId: null,
    });
  }
  return notices;
}

export interface CardArrivalsOptions {
  groups: RunGroups;
  sessions: Readonly<Record<string, SessionView>>;
  assistantName: string;
  whereOf: (sessionId: string) => string;
}

/** Announces newly open cards as one accessible, localized message per render. */
export function useCardArrivals({
  groups,
  sessions,
  assistantName,
  whereOf,
}: CardArrivalsOptions): { serial: number; text: string } {
  const previous = useRef<RunSignals | null>(null);
  const [arrival, setArrival] = useState({ serial: 0, text: "" });

  useEffect(() => {
    const next = runSignals(groups, sessions, new Set(), assistantName);
    const notices = previous.current ? runNotices(previous.current, next) : [];
    previous.current = next;
    const cards = notices.filter((notice) => notice.toolUseId !== null);
    if (cards.length === 0) return;

    const text =
      cards.length > 1
        ? tf("shell.needs_you.arrival.many", { n: cards.length })
        : (() => {
            const notice = cards[0];
            if (!notice?.toolUseId) return "";
            const card = sessions[notice.sessionId]?.cards.find(
              (candidate) => candidate.data.tool_use_id === notice.toolUseId,
            );
            if (!card) return "";
            let body: string;
            if (card.kind === "question") {
              body = tf("shell.needs_you.arrival.question", { name: assistantName });
            } else if (card.data.agent === "") {
              body = tf(
                card.data.kind === "plan"
                  ? "shell.needs_you.arrival.plan"
                  : "shell.needs_you.arrival.tool",
                { name: assistantName },
              );
            } else {
              const row = needsRowFor(groups, notice.sessionId, notice.toolUseId);
              body = row
                ? tf("shell.needs_you.arrival.tool", { name: row.block.agent })
                : card.data.agent === "subagent"
                  ? tf("shell.needs_you.arrival.agent", {})
                  : tf("shell.needs_you.arrival.tool", { name: card.data.agent });
            }
            return tf("shell.needs_you.arrival.one", {
              body,
              where: whereOf(notice.sessionId),
            });
          })();
    if (text) setArrival((current) => ({ serial: current.serial + 1, text }));
  }, [groups, sessions, assistantName, whereOf]);

  return arrival;
}

export interface RunNotificationOptions {
  groups: RunGroups;
  sessions: Readonly<Record<string, SessionView>>;
  exclude: ReadonlySet<string>;
  assistantName: string;
  enabled: boolean;
  quietStart: string | null;
  quietEnd: string | null;
  notifier: Notifier;
  isFocused?: () => boolean;
  now?: () => Date;
  onOpen: (sessionId: string, blockId: string | null) => void;
}

const pageIsFocused = (): boolean => document.visibilityState === "visible" && document.hasFocus();
const clockNow = (): Date => new Date();

export function useRunNotifications({
  groups,
  sessions,
  exclude,
  assistantName,
  enabled,
  quietStart,
  quietEnd,
  notifier,
  isFocused = pageIsFocused,
  now = clockNow,
  onOpen,
}: RunNotificationOptions): void {
  const previous = useRef<RunSignals | null>(null);
  // The freshest groups, read only when a notice is clicked — long after the render that fired it,
  // and often after useRunRequests' own read has since joined the card to its run (see the module
  // doc). Plain assignment, not an effect: every render keeps this current, click or not.
  const groupsRef = useRef(groups);
  groupsRef.current = groups;
  useEffect(() => {
    const next = runSignals(groups, sessions, exclude, assistantName);
    const notices = previous.current ? runNotices(previous.current, next) : [];
    previous.current = next;
    if (notices.length === 0 || !enabled || notifier.permission() !== "granted") return;
    if (inQuietHours(quietStart, quietEnd, now()) || isFocused()) return;
    for (const notice of notices)
      notifier.show(NOTICE_TITLE, notice.body, notice.tag, () => {
        const blockId =
          notice.toolUseId === null
            ? notice.blockId
            : (needsRowFor(groupsRef.current, notice.sessionId, notice.toolUseId)?.block.id ??
              notice.blockId);
        onOpen(notice.sessionId, blockId);
      });
  }, [
    groups,
    sessions,
    exclude,
    assistantName,
    enabled,
    quietStart,
    quietEnd,
    notifier,
    isFocused,
    now,
    onOpen,
  ]);
}
