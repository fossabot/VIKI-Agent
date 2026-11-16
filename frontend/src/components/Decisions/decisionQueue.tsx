/**
 * The decision queue: arms a 5 s undo window before a row or key decision is actually
 * sent, so a fast change of mind never races the real send. `DecisionQueueProvider` owns the
 * timers and the in-flight bookkeeping behind the `DecisionQueue` contract; `useDecisionQueue` is
 * the hook a row or key handler calls to enqueue or undo one; `UndoBar` is the one place that
 * renders the pending rows and a failed send. Unmounting the provider (closing the surface that
 * hosts it, e.g. the Approvals dialog) flushes every still-pending send right away.
 * The provider sends one decision at a time, in the order they were enqueued, whether a window
 * ended or the flush sent it: the server admits only 4 decision requests at once and refuses the
 * rest (429 `overloaded`). A send that fails once the provider is gone (a flushed one, or one in
 * flight or waiting then) has no `UndoBar` left to show it, so it goes to `onLateFailure`.
 * A caller can hand the same `DecisionLine` (`decisionLine()`) to two mounts in turn — App.tsx
 * does, across an Approvals close and the reopen after it — so the one line's order and failures
 * still carry over; without the prop a provider keeps its own line, as if it were the only one.
 */
import {
  createContext,
  type ReactElement,
  type ReactNode,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";
import "./decisionQueue.css";

export const UNDO_MS = 5000;

/** The sends one window has started, in order, and the keys still actually in flight to the
 * server. `decisionLine()` makes one; App.tsx keeps it (`useState`) for the window's life and
 * passes it to every `DecisionQueueProvider` mount, so a reopened Approvals waits behind, and
 * knows about, whatever the last close is still sending. */
export interface DecisionLine {
  chain: Promise<void>;
  inFlight: Set<string>;
}
export const decisionLine = (): DecisionLine => ({ chain: Promise.resolve(), inFlight: new Set() });

export interface DecisionQueue {
  pending: readonly { key: string; label: string }[];
  isPending(key: string): boolean;
  enqueue(key: string, label: string, send: () => Promise<void>): void;
  undo(key: string): void;
}

interface QueuedEntry {
  label: string;
  send: () => Promise<void>;
  timer: ReturnType<typeof setTimeout>;
}

interface SendFailure {
  label: string;
  text: string;
}

/** What the provider actually hands down; `UndoBar` reads `failure` too, but it stays off the
 * public `DecisionQueue` shape so a row/key handler never sees another row's error state. */
interface InternalQueue extends DecisionQueue {
  failure: SendFailure | null;
}

function hasOwnerText(error: unknown): error is { ownerText: string } {
  return (
    typeof error === "object" &&
    error !== null &&
    "ownerText" in error &&
    typeof (error as { ownerText: unknown }).ownerText === "string"
  );
}

const failureText = (error: unknown): string =>
  hasOwnerText(error) ? error.ownerText : "Nothing was recorded. Try again.";

const Context = createContext<InternalQueue | null>(null);

function useQueueContext(): InternalQueue {
  const found = useContext(Context);
  if (!found) throw new Error("useDecisionQueue must be used inside a DecisionQueueProvider");
  return found;
}

export function DecisionQueueProvider({
  delayMs = UNDO_MS,
  onLateFailure,
  line: shared,
  children,
}: {
  delayMs?: number;
  /** A send that failed after this provider unmounted: the label it was queued with and the same
   * text `UndoBar` would have shown. */
  onLateFailure?: (label: string, text: string) => void;
  /** The window's one line (`decisionLine()`), shared with whatever provider held Approvals open
   * before this mount. Absent, this provider makes its own and keeps it for its own life. */
  line?: DecisionLine;
  children: ReactNode;
}): ReactElement {
  const entries = useRef(new Map<string, QueuedEntry>());
  // Each link catches its own send's failure, so a failed send does not stop the ones behind it.
  const own = useRef<DecisionLine | null>(null);
  if (!shared && !own.current) own.current = decisionLine();
  const line = (shared ?? own.current) as DecisionLine;
  const mounted = useRef(true);
  const lateFailure = useRef(onLateFailure);
  lateFailure.current = onLateFailure;
  const [, setTick] = useState(0);
  const [failure, setFailure] = useState<SendFailure | null>(null);
  const rerender = (): void => setTick((tick) => tick + 1);

  function startSend(key: string, label: string, send: () => Promise<void>): void {
    line.inFlight.add(key);
    line.chain = line.chain.then(send).then(
      () => {
        line.inFlight.delete(key);
        if (mounted.current) rerender();
      },
      (error: unknown) => {
        line.inFlight.delete(key);
        if (mounted.current) {
          setFailure({ label, text: failureText(error) });
          rerender();
        } else lateFailure.current?.(label, failureText(error));
      },
    );
  }

  const enqueue = (key: string, label: string, send: () => Promise<void>): void => {
    const existing = entries.current.get(key);
    if (existing) clearTimeout(existing.timer);
    entries.current.delete(key); // a replace re-enters at the end: it just happened "now".
    const timer = setTimeout(() => {
      entries.current.delete(key);
      startSend(key, label, send);
      rerender();
    }, delayMs);
    entries.current.set(key, { label, send, timer });
    setFailure(null);
    rerender();
  };

  const undo = (key: string): void => {
    const existing = entries.current.get(key);
    if (!existing) return;
    clearTimeout(existing.timer);
    entries.current.delete(key);
    rerender();
  };

  const isPending = (key: string): boolean => entries.current.has(key) || line.inFlight.has(key);

  // Newest first (UndoBar's display order): a replace deletes-then-sets above, so the map's own
  // insertion order already puts the most recently touched key last; reverse it once, here.
  const pending = [...entries.current.entries()]
    .reverse()
    .map(([key, entry]) => ({ key, label: entry.label }));

  // Set here, not only at creation: in development StrictMode (main.tsx) runs this effect, its
  // cleanup and the effect again, and the live provider must not stay counted as unmounted.
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      const toFlush = [...entries.current.entries()];
      for (const [, entry] of toFlush) clearTimeout(entry.timer);
      entries.current.clear();
      // Each flushed key joins `inFlight` only once it is actually this link's turn on the line,
      // not the moment the panel closes — a key still waiting behind a sibling flushed with it
      // (or behind an older send a reopen is sharing the line with) is not yet "in flight", so a
      // reopened panel is free to act on it; `startSend` marks a window-ended key the moment its
      // timer fires instead, for the open panel's own immediate feedback.
      for (const [key, entry] of toFlush)
        line.chain = line.chain
          .then(() => {
            line.inFlight.add(key);
            return entry.send();
          })
          .then(
            () => {
              line.inFlight.delete(key);
            },
            (error: unknown) => {
              line.inFlight.delete(key);
              lateFailure.current?.(entry.label, failureText(error));
            },
          );
    };
  }, [line]);

  const value: InternalQueue = { pending, isPending, enqueue, undo, failure };
  return <Context.Provider value={value}>{children}</Context.Provider>;
}

export function useDecisionQueue(): DecisionQueue {
  return useQueueContext();
}

export function UndoBar(): ReactElement | null {
  const { pending, undo, failure } = useQueueContext();
  if (pending.length === 0 && !failure) return null;
  return (
    <div className="decision-undo-bar">
      {pending.length > 0 ? (
        <ul role="status" className="decision-undo-rows">
          {pending.map((row) => (
            <li key={row.key} className="decision-undo-row">
              <span className="decision-undo-label">{row.label} · sends in 5 seconds</span>
              <button
                type="button"
                className="btn txt sm decision-undo-action"
                aria-label={`Undo ${row.label}`}
                onClick={() => undo(row.key)}
              >
                Undo
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      {failure ? (
        <p role="alert" className="decision-undo-error">
          {`Could not finish “${failure.label}”. ${failure.text}`}
        </p>
      ) : null}
    </div>
  );
}

/** Moves focus to the next (`step` 1, the default) or previous (-1) `[data-approval-row]` inside
 * `from`'s nearest `[role="dialog"]` ancestor. False, focus untouched, when there is no dialog or
 * no such row to move to — including past either end, where callers let j/k simply stop. */
export function focusNextRow(from: Element, step: 1 | -1 = 1): boolean {
  const dialog = from.closest('[role="dialog"]');
  if (!dialog) return false;
  const rows = [...dialog.querySelectorAll<HTMLElement>("[data-approval-row]")];
  const current = from.closest("[data-approval-row]");
  const index = current ? rows.indexOf(current as HTMLElement) : -1;
  const target = rows[index + step];
  if (!target) return false;
  target.focus();
  return true;
}
