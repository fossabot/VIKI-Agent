import { useEffect, useRef, useState } from "react";
import type {
  ConversationSearchApi,
  ConversationSearchResult,
  HistoryAnchorPage,
  MatchSnapshot,
  NativeSearchHit,
} from "../../clients/conversations/conversationSearchApi";
import {
  type AppSearchHit,
  appHitSource,
  currentAppHit,
  searchAppTurns,
} from "../../state/conversationSearch";
import type { SessionView } from "../../state/view";
import { Loading } from "../Loading/Loading";
import "./ConversationTools.css";

type SelectedHit = NativeSearchHit | AppSearchHit;
export interface RevisionSearchProps {
  api: ConversationSearchApi;
  view: SessionView;
  onNativePage: (page: HistoryAnchorPage, generation: string | null) => boolean;
  onJump: (turnId: string) => void;
}
function Snapshot({ value }: { value: MatchSnapshot }) {
  return (
    <span>
      {value.before}
      <mark>{value.match}</mark>
      {value.after}
    </span>
  );
}

/** Search results retain their own text snapshot; asynchronous selection never trusts a UUID alone. */
export function RevisionSearch({ api, view, onNativePage, onJump }: RevisionSearchProps) {
  const [query, setQuery] = useState("");
  const [composing, setComposing] = useState(false);
  const [native, setNative] = useState<ConversationSearchResult | null>(null);
  const [local, setLocal] = useState<AppSearchHit[]>([]);
  const [appHitSettled, setAppHitSettled] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const [selected, setSelected] = useState<SelectedHit | null>(null);
  const [selectionReason, setSelectionReason] = useState("");
  const [opening, setOpening] = useState(false);
  const latest = useRef(view);
  latest.current = view;
  const operation = useRef<AbortController | null>(null);
  const composition = useRef(false);
  const appHitTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const appHitGuard = useRef<{
    query: string;
    sessionId: string;
    generation: string | null;
    composing: boolean;
  } | null>(null);
  // Bumped to invalidate an in-flight app-hit search. Effect cleanup alone cannot do this: it
  // would also fire on every `seq`-only re-invocation, which must leave a pending timer alone.
  const appHitToken = useRef(0);
  const sessionId = view.sessionId,
    generation = view.generation,
    seq = view.seq;
  // biome-ignore lint/correctness/useExhaustiveDependencies: a different query or session invalidates the selected snapshot.
  useEffect(() => {
    operation.current?.abort();
    operation.current = null;
    setOpening(false);
    setSelected(null);
    setSelectionReason("");
  }, [query, sessionId, generation]);
  // The native search asks the server, which cannot see a live delta: a streaming turn must
  // never abort or restart it, so it keys only on what could change a server-side match.
  // biome-ignore lint/correctness/useExhaustiveDependencies: generation invalidates a result carried over from a previous conversation even with an unchanged query.
  useEffect(() => {
    const controller = new AbortController();
    if (!query.trim() || composing) {
      setNative(null);
      setBusy(false);
      return;
    }
    setBusy(true);
    setError(false);
    const id = crypto.randomUUID();
    const timer = setTimeout(() => {
      void api.search(sessionId, query, id, controller.signal).then(
        (result) => {
          if (controller.signal.aborted) return;
          setNative(result);
          setError(false);
          setBusy(false);
        },
        () => {
          if (controller.signal.aborted) return;
          setNative(null);
          setError(true);
          setBusy(false);
        },
      );
    }, 250);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [api, query, sessionId, generation, composing]);
  // The app-hit search reads live turns from `latest`. A query, session, generation or
  // composing change always (re)schedules its timer; a `seq` change (a live envelope) schedules
  // it only when no timer is already pending, so a steady stream still re-runs the search on a
  // throttle instead of starving behind a debounce that a fast stream never lets settle.
  // biome-ignore lint/correctness/useExhaustiveDependencies: seq is the live-envelope signal that re-arms the throttle; it is never read in the effect body itself.
  useEffect(() => {
    if (!query.trim() || composing) {
      appHitGuard.current = { query, sessionId, generation, composing };
      appHitToken.current += 1;
      if (appHitTimer.current !== null) {
        clearTimeout(appHitTimer.current);
        appHitTimer.current = null;
      }
      setLocal([]);
      setAppHitSettled(true);
      return;
    }
    const previous = appHitGuard.current;
    const reset =
      !previous ||
      previous.query !== query ||
      previous.sessionId !== sessionId ||
      previous.generation !== generation ||
      previous.composing !== composing;
    appHitGuard.current = { query, sessionId, generation, composing };
    if (reset) {
      appHitToken.current += 1;
      setLocal([]);
      setAppHitSettled(false);
      if (appHitTimer.current !== null) {
        clearTimeout(appHitTimer.current);
        appHitTimer.current = null;
      }
    }
    if (appHitTimer.current !== null) return;
    const token = appHitToken.current;
    appHitTimer.current = setTimeout(() => {
      appHitTimer.current = null;
      void searchAppTurns(latest.current, query).then(
        (hits) => {
          if (appHitToken.current === token) {
            setLocal(hits);
            setAppHitSettled(true);
          }
        },
        () => {
          if (appHitToken.current === token) {
            setLocal([]);
            setAppHitSettled(true);
          }
        },
      );
    }, 250);
  }, [query, sessionId, generation, seq, composing]);
  useEffect(
    () => () => {
      appHitToken.current += 1;
      if (appHitTimer.current !== null) {
        clearTimeout(appHitTimer.current);
        appHitTimer.current = null;
      }
    },
    [],
  );
  useEffect(() => () => operation.current?.abort(), []);

  async function open(hit: SelectedHit) {
    if (composition.current) return;
    operation.current?.abort();
    const controller = new AbortController();
    operation.current = controller;
    setSelected(hit);
    setSelectionReason("");
    setOpening(true);
    const captured = latest.current;
    try {
      if ("kind" in hit) {
        const source = appHitSource(captured, hit);
        const same = await currentAppHit(captured, hit);
        if (controller.signal.aborted || composition.current) return;
        // A live envelope may commit while the hash is pending; validate its exact input again
        // synchronously so a matching old digest cannot jump to changed content or ownership.
        if (!same || appHitSource(latest.current, hit) !== source)
          setSelectionReason("source_changed");
        else onJump(hit.turnId);
      } else {
        const page = await api.anchor(
          sessionId,
          hit.anchor,
          crypto.randomUUID(),
          controller.signal,
        );
        if (controller.signal.aborted || composition.current) return;
        if (page.state !== "ready") setSelectionReason(page.reason);
        else if (
          latest.current.generation !== captured.generation ||
          !onNativePage(page, captured.generation)
        )
          setSelectionReason("source_changed");
      }
    } catch {
      if (!controller.signal.aborted) setSelectionReason("read_failed");
    } finally {
      if (operation.current === controller) {
        operation.current = null;
        setOpening(false);
      }
    }
  }
  const explanation = (reason: string): string => {
    if (reason === "source_changed")
      return "The source changed. The search snapshot is kept below.";
    if (reason === "entry_missing" || reason === "transcript_missing")
      return "The source is no longer available. The search snapshot is kept below.";
    if (reason === "outside_window" || reason === "tail_budget")
      return "Older history is outside the bounded search window.";
    if (reason === "time_budget") return "This could not be loaded in time.";
    if (reason === "too_large")
      return "This complete turn is too large to display here. Open the native transcript to inspect it.";
    return "This part of the conversation could not be loaded. Try again.";
  };
  return (
    <section className="conversation-search" aria-label="Search conversation">
      <label>
        Search conversation
        <input
          value={query}
          maxLength={256}
          onChange={(event) => setQuery(event.target.value)}
          onCompositionStart={() => {
            composition.current = true;
            setComposing(true);
          }}
          onCompositionEnd={() => {
            composition.current = false;
            setComposing(false);
          }}
        />
      </label>
      {busy ? <Loading>Searching…</Loading> : null}
      {error || native?.state === "error" ? (
        <p role="status">Some history could not be searched.</p>
      ) : null}
      {native?.state === "partial" ? (
        <p role="status">
          {native.reason === "time_budget"
            ? "Search stopped after 2 seconds; showing what it found."
            : native.reason === "response_budget"
              ? "More matches exist than this search can display."
              : explanation("tail_budget")}
          {native.coverage === "tail" &&
          (native.reason === "response_budget" || native.reason === "time_budget")
            ? ` ${explanation("tail_budget")}`
            : ""}
        </p>
      ) : null}
      {query.trim() &&
      !busy &&
      !error &&
      appHitSettled &&
      native?.state === "ready" &&
      !native.hits.length &&
      !local.length ? (
        <p role="status">No matches.</p>
      ) : null}
      <ul>
        {native?.hits.map((hit) => (
          <li key={`${hit.anchor.entry_uuid}-${hit.anchor.mapped_index}`}>
            <button type="button" onClick={() => void open(hit)}>
              <Snapshot value={hit.snapshot} />
            </button>
          </li>
        ))}
        {local.map((hit) => (
          <li key={`${hit.turnId}-${hit.role}`}>
            <button type="button" onClick={() => void open(hit)}>
              <Snapshot value={hit.snapshot} />
            </button>
          </li>
        ))}
      </ul>
      {opening ? <Loading>Opening match…</Loading> : null}
      {selected && selectionReason ? (
        <div role="status">
          <p>{explanation(selectionReason)}</p>
          <Snapshot value={selected.snapshot} />
          <button type="button" onClick={() => void open(selected)}>
            Try again
          </button>
        </div>
      ) : null}
    </section>
  );
}
