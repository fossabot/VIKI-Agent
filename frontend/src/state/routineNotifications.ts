/**
 * Browser notifications for routines that need the user or have finished.
 *
 * Rules:
 * - A notice fires for a new waiting attempt or a new finished attempt of a routine seen in an
 *   earlier ready snapshot. The first ready snapshot and a routine's first sighting are silent.
 * - Disabled, permission not granted, quiet hours and a focused page each suppress a notice. A
 *   suppressed notice is dropped, never replayed: the baseline moves on either way.
 * - The title and body are catalog text. Neither holds a routine name or any run content, and a
 *   click only focuses the window and opens the routine; it never answers a card.
 */
import { useEffect, useRef } from "react";
import { t } from "../i18n/t";
import { type RoutinesSnapshot, routineAttention } from "./routines";

export type NotifyPermission = "default" | "granted" | "denied" | "unsupported";

export interface Notifier {
  permission(): NotifyPermission;
  request(): Promise<NotifyPermission>;
  show(title: string, body: string, tag: string, onClick: () => void): void;
}

/** The slice of `window` the browser notifier uses; a test passes a fake. */
export type NotificationHost = Pick<Window, "focus"> & { Notification?: typeof Notification };

export function browserNotifier(win: NotificationHost = window): Notifier {
  const permission = (): NotifyPermission =>
    win.Notification ? win.Notification.permission : "unsupported";
  return {
    permission,
    async request() {
      if (!win.Notification) return "unsupported";
      return await win.Notification.requestPermission();
    },
    show(title, body, tag, onClick) {
      if (!win.Notification) return;
      const notification = new win.Notification(title, { body, tag });
      notification.onclick = () => {
        win.focus();
        onClick();
        notification.close();
      };
    },
  };
}

/** `HH:MM` as minutes since midnight; anything else is null. */
function minutesOf(clock: string | null): number | null {
  const match = clock === null ? null : /^(\d{2}):(\d{2})$/.exec(clock);
  return match ? Number(match[1]) * 60 + Number(match[2]) : null;
}

/** Local time `now` against the window `start`..`end`, which may cross midnight. Quiet hours are
 * on only when both bounds are set and differ. */
export function inQuietHours(start: string | null, end: string | null, now: Date): boolean {
  const from = minutesOf(start);
  const to = minutesOf(end);
  if (from === null || to === null || from === to) return false;
  const at = now.getHours() * 60 + now.getMinutes();
  return from < to ? from <= at && at < to : at >= from || at < to;
}

export interface RoutineSignal {
  /** The attempt whose session holds an open card, if any. */
  waiting: string | null;
  /** The latest attempt that finished, and whether it succeeded. */
  finished: string | null;
  succeeded: boolean;
}

export type Signals = ReadonlyMap<string, RoutineSignal>;

export interface Notice {
  kind: "waiting" | "done" | "not_done";
  routineId: string;
  tag: string;
}

type SessionCards = Parameters<typeof routineAttention>[1];

export function signalsOf(items: RoutinesSnapshot["items"], sessions: SessionCards): Signals {
  const signals = new Map<string, RoutineSignal>();
  for (const item of items) {
    const attention = routineAttention(item, sessions);
    const attempt = item.last_attempt?.attempt;
    const finished = attempt && attempt.outcome !== null ? attempt : null;
    signals.set(item.routine_id, {
      waiting: attention?.kind === "waiting" ? (item.active_run?.attempt_id ?? null) : null,
      finished: finished?.attempt_id ?? null,
      succeeded: finished?.outcome === "succeeded",
    });
  }
  return signals;
}

/** What changed from `prev` to `next`. A routine absent from `prev` is a first sighting. */
export function noticesFor(prev: Signals, next: Signals): Notice[] {
  const notices: Notice[] = [];
  for (const [routineId, now] of next) {
    const before = prev.get(routineId);
    if (!before) continue;
    if (now.waiting !== null && now.waiting !== before.waiting)
      notices.push({ kind: "waiting", routineId, tag: `viki-routine-${now.waiting}-waiting` });
    if (now.finished !== null && now.finished !== before.finished)
      notices.push({
        kind: now.succeeded ? "done" : "not_done",
        routineId,
        tag: `viki-routine-${now.finished}-done`,
      });
  }
  return notices;
}

export interface RoutineNotificationOptions {
  snapshot: RoutinesSnapshot;
  sessions: SessionCards;
  enabled: boolean;
  quietStart: string | null;
  quietEnd: string | null;
  notifier: Notifier;
  isFocused?: () => boolean;
  now?: () => Date;
  onOpen: (routineId: string) => void;
}

const pageIsFocused = (): boolean => document.visibilityState === "visible" && document.hasFocus();
const clockNow = (): Date => new Date();

export function useRoutineNotifications({
  snapshot,
  sessions,
  enabled,
  quietStart,
  quietEnd,
  notifier,
  isFocused = pageIsFocused,
  now = clockNow,
  onOpen,
}: RoutineNotificationOptions): void {
  const previous = useRef<Signals | null>(null);
  useEffect(() => {
    if (snapshot.status !== "ready") return;
    const next = signalsOf(snapshot.items, sessions);
    const notices = previous.current ? noticesFor(previous.current, next) : [];
    previous.current = next;
    if (notices.length === 0 || !enabled || notifier.permission() !== "granted") return;
    if (inQuietHours(quietStart, quietEnd, now()) || isFocused()) return;
    for (const notice of notices)
      notifier.show(
        t("routines.notify.title"),
        t(`routines.notify.${notice.kind}`),
        notice.tag,
        () => onOpen(notice.routineId),
      );
  }, [snapshot, sessions, enabled, quietStart, quietEnd, notifier, isFocused, now, onOpen]);
}
