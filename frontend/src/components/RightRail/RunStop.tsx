/**
 * RunStop: the per-run Stop control, and the state machine behind it (`useRunStops`). A
 * chat's own Stop only stops the reply, so each live background run gets its own Stop here.
 * Idle "Stop" asks for confirmation; Confirm either sends or backs out (Escape also backs out).
 * A sent stop reads "Stopping…" until the run reports its own end — at which point the caller
 * drops its key from `runningKeys` and this hook simply forgets it — or until STOP_CONFIRM_MS
 * passes with no such end, when it reads as unconfirmed and offers Try again. A rejected `stop`
 * reads failed, worded by its code.
 *
 * `useRunStops` holds one `StopPhase` per key, keyed however the caller tracks a row (e.g.
 * AgentRuns' own `row.key`); a key with no entry is idle, the same as one explicitly set to
 * `{ kind: "idle" }` by `cancel` (Keep running or Escape) — the two render the same "Stop"
 * button, but only the latter autofocuses it, since it is always a direct response to the user
 * backing out, where a fresh `undefined` key may be mounting for the first time with focus
 * elsewhere. `send` keeps at most one request in flight per key via `inFlightRef` (checked and
 * set synchronously, so two calls in the same tick cannot both start a request) — a second call
 * while one is already in flight for that key is a no-op. A key that leaves `runningKeys` drops
 * its phase and its in-flight marker; a `stop` that settles afterwards — because its key is gone
 * or this hook has unmounted — changes nothing.
 *
 * Focus never drops to the page across a phase change the user's own click caused: Confirm
 * autofocuses Keep running; backing out of confirm (Keep running or Escape) or dismissing a
 * failed attempt both autofocus the idle Stop button they return to; sending autofocuses its
 * own `tabIndex=-1` "Stopping…" status, carried through to "sent" since it is the same element,
 * and Try again — from a stale "sent" or from failed — returns focus to that same status. A
 * one-shot ref flag, set by RunStop's own handlers and read by a `useLayoutEffect` keyed on the
 * phase kind, gates every one of these moves, so a row that remounts with a phase already under
 * way — e.g. moved to another group after Keep running or Stop run — starts with the
 * flag clear and takes no focus. Confirm's own `<fieldset>` is named by its question
 * (`aria-labelledby`), not left anonymous.
 */
import {
  type ReactElement,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { type RunsApi, RunsApiError } from "../../clients/conversations/runsApi";
import "./RunStop.css";

export const STOP_CONFIRM_MS = 30_000;

export type StopPhase =
  /** Idle because a confirm was explicitly backed out of (Keep running or Escape) — as opposed
   * to a key with no entry at all, `undefined`, which never steals focus on its first mount. */
  | { kind: "idle" }
  | { kind: "confirm" }
  | { kind: "sending" }
  | { kind: "sent"; at: number }
  | { kind: "failed"; code: string };

export interface RunStops {
  phase(key: string): StopPhase | undefined;
  ask(key: string): void;
  cancel(key: string): void;
  send(key: string, sessionId: string, blockId: string): void;
}

/** A rejection the contract never promised is worded the same as a server the client cannot
 * reach: `failureSentence`'s fallback already covers any code outside its table. */
function codeOf(error: unknown): string {
  return error instanceof RunsApiError ? error.code : "unavailable";
}

export function useRunStops(
  api: RunsApi | undefined,
  runningKeys: ReadonlySet<string>,
  now: () => number = Date.now,
): RunStops {
  const [phases, setPhases] = useState<ReadonlyMap<string, StopPhase>>(new Map());
  const inFlightRef = useRef<Set<string>>(new Set());
  const mountedRef = useRef(true);

  useEffect(
    () => () => {
      mountedRef.current = false;
    },
    [],
  );

  // A key that leaves `runningKeys` — its run ended, or its row left the groups — drops its phase
  // and its in-flight marker, so a `stop` that settles afterwards is ignored (see `send` below).
  useEffect(() => {
    for (const key of inFlightRef.current) {
      if (!runningKeys.has(key)) inFlightRef.current.delete(key);
    }
    setPhases((prev) => {
      let next: Map<string, StopPhase> | undefined;
      for (const key of prev.keys()) {
        if (runningKeys.has(key)) continue;
        next ??= new Map(prev);
        next.delete(key);
      }
      return next ?? prev;
    });
  }, [runningKeys]);

  const ask = useCallback((key: string) => {
    setPhases((prev) => {
      const next = new Map(prev);
      next.set(key, { kind: "confirm" });
      return next;
    });
  }, []);

  const cancel = useCallback((key: string) => {
    setPhases((prev) => {
      if (!prev.has(key)) return prev;
      const next = new Map(prev);
      next.set(key, { kind: "idle" });
      return next;
    });
  }, []);

  const send = useCallback(
    (key: string, sessionId: string, blockId: string) => {
      if (!api || inFlightRef.current.has(key)) return;
      inFlightRef.current.add(key);
      setPhases((prev) => {
        const next = new Map(prev);
        next.set(key, { kind: "sending" });
        return next;
      });
      api.stop(sessionId, blockId).then(
        () => {
          if (!inFlightRef.current.has(key)) return; // dropped: key left runningKeys meanwhile
          inFlightRef.current.delete(key);
          if (!mountedRef.current) return;
          setPhases((prev) => {
            const next = new Map(prev);
            next.set(key, { kind: "sent", at: now() });
            return next;
          });
        },
        (error: unknown) => {
          if (!inFlightRef.current.has(key)) return;
          inFlightRef.current.delete(key);
          if (!mountedRef.current) return;
          setPhases((prev) => {
            const next = new Map(prev);
            next.set(key, { kind: "failed", code: codeOf(error) });
            return next;
          });
        },
      );
    },
    [api, now],
  );

  const phase = useCallback((key: string) => phases.get(key), [phases]);

  return { phase, ask, cancel, send };
}

export interface RunStopProps {
  agent: string;
  chat: string;
  phase: StopPhase | undefined;
  nowMs: number;
  onAsk: () => void;
  onCancel: () => void;
  onSend: () => void;
}

const FAILURE_SENTENCES: Readonly<Record<string, string>> = {
  unknown_run: "This run is no longer running in its chat's Claude Code process.",
  not_running: "Claude Code is not running for this chat.",
  not_found: "This chat is not open any more.",
  refused: "Claude Code did not stop this run.",
  timeout: "Claude Code did not answer in time. Try again.",
  overloaded: "Too many stops at once. Try again.",
};

function failureSentence(code: string): string {
  return FAILURE_SENTENCES[code] ?? "Could not stop this run. Try again.";
}

/** One run's Stop control, rendered for whatever phase `useRunStops` currently has its key in
 * (`undefined` = idle). The caller binds `onAsk`/`onCancel`/`onSend` to that key via the hook's
 * `ask`/`cancel`/`send`. */
export function RunStop({
  agent,
  chat,
  phase,
  nowMs,
  onAsk,
  onCancel,
  onSend,
}: RunStopProps): ReactElement {
  const askId = useId();
  // Set by this component's own handlers, just before each calls the prop that causes the next
  // phase; read once by the layout effect below, then cleared. A remount (a row moved to another
  // group while idle, sending or sent) starts this ref at its default `false`, so the
  // phase it first renders with, however it got there, steals no focus.
  const focusPendingRef = useRef(false);
  const stopRef = useRef<HTMLButtonElement | null>(null);
  const keepRunningRef = useRef<HTMLButtonElement | null>(null);
  const statusRef = useRef<HTMLParagraphElement | null>(null);

  useLayoutEffect(() => {
    if (!focusPendingRef.current) return;
    focusPendingRef.current = false;
    if (phase?.kind === "idle") stopRef.current?.focus();
    else if (phase?.kind === "confirm") keepRunningRef.current?.focus();
    else if (phase?.kind === "sending" || phase?.kind === "sent") statusRef.current?.focus();
  }, [phase?.kind]);

  const ownerActed = (): void => {
    focusPendingRef.current = true;
  };

  if (phase === undefined || phase.kind === "idle") {
    return (
      <button
        type="button"
        className="run-stop"
        aria-label={`Stop ${agent} in ${chat}`}
        ref={stopRef}
        onClick={() => {
          ownerActed();
          onAsk();
        }}
      >
        Stop
      </button>
    );
  }

  if (phase.kind === "confirm") {
    return (
      <fieldset
        className="run-stop-confirm"
        aria-labelledby={askId}
        onKeyDown={(event) => {
          if (event.key !== "Escape") return;
          event.preventDefault();
          ownerActed();
          onCancel();
        }}
      >
        <p id={askId} className="run-stop-ask">{`Stop ${agent} now?`}</p>
        <button
          type="button"
          className="run-stop-send"
          onClick={() => {
            ownerActed();
            onSend();
          }}
        >
          Stop run
        </button>
        <button
          type="button"
          className="run-stop-keep"
          ref={keepRunningRef}
          onClick={() => {
            ownerActed();
            onCancel();
          }}
        >
          Keep running
        </button>
      </fieldset>
    );
  }

  if (phase.kind === "sending" || phase.kind === "sent") {
    const stale = phase.kind === "sent" && nowMs - phase.at >= STOP_CONFIRM_MS;
    return (
      <p role="status" className="run-stop-status" tabIndex={-1} ref={statusRef}>
        {stale ? "Stop sent; Claude Code has not confirmed it yet." : "Stopping…"}
        {stale ? (
          <button
            type="button"
            className="run-stop-retry"
            onClick={() => {
              ownerActed();
              onSend();
            }}
          >
            Try again
          </button>
        ) : null}
      </p>
    );
  }

  return (
    <div role="alert" className="run-stop-failed">
      <p className="run-stop-failed-text">{failureSentence(phase.code)}</p>
      <button
        type="button"
        className="run-stop-retry"
        onClick={() => {
          ownerActed();
          onSend();
        }}
      >
        Try again
      </button>
      <button
        type="button"
        className="run-stop-dismiss"
        onClick={() => {
          ownerActed();
          onCancel();
        }}
      >
        Dismiss
      </button>
    </div>
  );
}
