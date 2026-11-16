/**
 * Ctrl+K session search (src/accept/m3b/palette.accept.test.tsx pins the contract). The
 * visual follows the design's grammar (a centred panel on the shell's tokens).
 *
 * Rules — the IME contract first, same as the composer (see Composer.tsx's header), because
 * everyone who types through an IME depends on it:
 * - Every key handled below belongs to the IME, not us, whenever `event.nativeEvent.isComposing
 *   || event.keyCode === 229`: the Enter that only commits a Japanese conversion never opens a
 *   hit, the Escape that only cancels a candidate window never closes the palette, and the
 *   arrows that drive the candidate list never move our highlight.
 * - Renders nothing while `open` is false. When open: a `role="dialog"` panel (`.palette`,
 *   aria-label "Search sessions") with an `input` (focused on open, placeholder "Search
 *   sessions") and a `.palette-hits` list.
 * - Typing schedules search(q, requestId) after `debounceMs` (default 150) of quiet; the timer
 *   restarts on every keystroke. A blank q searches nothing and clears the hits. Every request
 *   carries a fresh `crypto.randomUUID()`, and a response is painted only when it answers that
 *   newest request and the input's current text still matches it — so a slow answer the user
 *   has typed past is dropped even if the same text has come back on screen since. The input
 *   carries `maxLength={256}`, the query cap the server refuses with 400.
 * - The server answers with an observation, not a bare list: a `superseded` result
 *   never scanned and is never painted; a typed `error` and a rejected request both clear the
 *   previous query's hits (so a stale row can't be walked with the arrows or opened with
 *   Enter) and show "search failed"; a `partial` result says how many sessions were searched
 *   rather than letting a budget read as "no matches". Every note's wording is in
 *   state/catalog.ts, which also mirrors catalog.py's short-query floor: a refusal and a
 *   genuine zero-result search are the same empty `hits` on the wire.
 * - Each hit is a `.hit` button (`data-session-id`): the title (`.hit-title.jp`), a `.chip.dim`
 *   role — "you" for "user", the assistant name for "assistant", none for "title" — the snippet
 *   (`.hit-snippet`), and timeLabel(at, nowMs) (`.hit-at`, mono) when `at` is non-empty.
 * - ArrowDown / ArrowUp move the highlight (`.hit-active`, wrapping at the ends); Enter opens
 *   the highlighted hit, or the first when none is highlighted: onOpen(session_id) then
 *   onClose(). Clicking a hit does the same. These three keys are scoped to the input alone
 *   (a hit is a real, focusable `<button>` reachable by Tab inside the dialog; if Enter on a
 *   focused hit also ran this handler, it would race the button's own default activation and
 *   open the wrong session).
 * - The whole modal dismisses two ways: Escape, on a window capture listener that lives only while the
 *   palette is open, and a mousedown directly on the dim `.palette-backdrop` — a click on the
 *   panel or its contents does not close it. Escape has to be the window's and not the panel's:
 *   a mousedown on the panel's own chrome (its padding, the note line, the list's gutter) lands
 *   on nothing focusable, so the browser blurs to document.body and a panel-scoped listener
 *   never sees the key again. That would leave a modal that swallows clicks and ignores Escape.
 * - Timers are the window's (tests fake them); nothing sleeps.
 */
import {
  type ChangeEvent,
  type KeyboardEvent,
  type MouseEvent,
  type ReactElement,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import type { SearchHit, SearchResult } from "../../protocol";
import { observeSearch, SEARCH_FAILED, SEARCHING, searchNote } from "../../state/catalog";
import { timeLabel } from "../../state/rows";
import { useIdentity } from "../Avatar/identity";
import { useModalFocus } from "../Dialog/useModalFocus";
import "./SearchPalette.css";

export interface SearchPaletteProps {
  open: boolean;
  search: (q: string, requestId: string) => Promise<SearchResult>;
  onOpen: (sessionId: string) => void;
  onClose: () => void;
  nowMs: number;
  debounceMs?: number;
  archived?: (sessionId: string) => boolean;
}

function roleChip(role: SearchHit["role"], assistantName: string): string | null {
  if (role === "user") return "you";
  if (role === "assistant") return assistantName;
  return null;
}

export function SearchPalette({
  open,
  search,
  onOpen,
  onClose,
  nowMs,
  debounceMs = 150,
  archived,
}: SearchPaletteProps): ReactElement | null {
  const { assistantName } = useIdentity();
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<readonly SearchHit[]>([]);
  const [note, setNote] = useState<string | null>(null);
  const [activeIndex, setActiveIndex] = useState<number | null>(null);
  const resultsId = useId();
  const statusId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  // The latest text the user typed: a response for anything else is stale and gets dropped.
  const latestQueryRef = useRef("");
  // The newest request's id. The text alone cannot tell a fresh answer from a slow one the
  // user typed past and back to (milk → mil → milk): both carry the current text.
  const latestRequestRef = useRef("");

  useModalFocus({
    active: open,
    dialogRef,
    initialFocusRef: inputRef,
    onDismiss: onClose,
  });

  useEffect(() => {
    return () => window.clearTimeout(timerRef.current);
  }, []);

  useEffect(() => {
    if (!open || activeIndex === null) return;
    dialogRef.current?.querySelector<HTMLElement>(".hit-active")?.scrollIntoView?.({
      block: "nearest",
      inline: "nearest",
      behavior: "instant",
    });
  }, [activeIndex, open]);

  if (!open) return null;

  const selectedHit = activeIndex === null ? undefined : hits[activeIndex];
  const selection = selectedHit
    ? `Result ${(activeIndex ?? 0) + 1} of ${hits.length}: ${selectedHit.title}`
    : "";
  const status = [
    note,
    selection ||
      (hits.length > 0 ? `${hits.length} results. Use Up and Down to select, Enter to open.` : ""),
  ]
    .filter(Boolean)
    .join(". ");

  /** True while `id` is still the request this palette is waiting for and its text is still
   * the text on screen. Both are needed: clearing the input issues no new request, so the id
   * alone would let the cleared query's answer repopulate the list. */
  function isCurrentRequest(q: string, id: string): boolean {
    return latestQueryRef.current === q && latestRequestRef.current === id;
  }

  function runSearch(q: string): void {
    const id = crypto.randomUUID();
    latestRequestRef.current = id;
    setNote(SEARCHING);
    search(q, id).then(
      (body) => {
        const result = observeSearch(body);
        if (!isCurrentRequest(q, id)) return;
        // The server echoes the id it answered, and "" when it attributed the request to none
        // An answer stamped with another request's id is not this one's.
        if (result.request_id !== "" && result.request_id !== id) return;
        // A superseded query never scanned. Painting its empty hits would wipe the list the
        // user is still waiting on and read as "no matches" for a search nobody ran — but the
        // pending claim has to go with it. This answer is the only one that will ever come
        // (another window's longer query took the gate, or the two GETs landed out of order),
        // so leaving "searching…" up would assert a request that no longer exists.
        if (result.state === "superseded") {
          setNote(null);
          return;
        }
        // A typed error has nothing to show, and the previous query's rows must not survive
        // it: otherwise they stay mounted under a note that answers a different query,
        // walkable with the arrows and openable with Enter.
        setActiveIndex(null);
        setHits(result.state === "error" ? [] : result.hits);
        setNote(searchNote(result, q));
      },
      () => {
        // A rejected request came back with no result at all, so there is nothing to observe.
        if (!isCurrentRequest(q, id)) return;
        setHits([]);
        setActiveIndex(null);
        setNote(SEARCH_FAILED);
      },
    );
  }

  function handleChange(event: ChangeEvent<HTMLInputElement>): void {
    const value = event.target.value;
    setQuery(value);
    latestQueryRef.current = value;
    setActiveIndex(null);
    window.clearTimeout(timerRef.current);
    if (value.trim() === "") {
      setHits([]);
      setNote(null);
      return;
    }
    timerRef.current = window.setTimeout(() => runSearch(value), debounceMs);
  }

  function openHit(sessionId: string): void {
    onOpen(sessionId);
    onClose();
  }

  // A mousedown directly on the dim backdrop (not on the panel or anything inside it) dismisses
  // the palette, matching the universal click-outside-a-modal gesture.
  function handleBackdropMouseDown(event: MouseEvent<HTMLDivElement>): void {
    if (event.target === event.currentTarget) onClose();
  }

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    // IME contract (same as the composer): a key event that is composing, or whose keyCode is
    // 229, belongs to the IME, not us — never treat it as Escape/Enter/arrows here.
    if (event.nativeEvent.isComposing || event.keyCode === 229) return;
    // Escape is handled at the window, not here: a mousedown on the panel's own chrome — its
    // padding, the note line, the list's gutter — hits no focusable element, so the browser
    // blurs to document.body and a keydown after that never reaches this subtree at all. The
    // user would be left with a modal that swallows clicks and ignores Escape.
    // ArrowUp/ArrowDown/Enter stay scoped to the input:
    // a hit is a real, focusable <button> (Tab reaches it inside the dialog), and if
    // Enter here also fired for a focused hit button, this handler would race the button's own
    // default activation — opening `hits[activeIndex ?? 0]` (the highlighted or first hit)
    // instead of the one the user actually has focused.
    if (event.target !== inputRef.current) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      if (hits.length === 0) return;
      setActiveIndex((i) => (i === null ? 0 : (i + 1) % hits.length));
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      if (hits.length === 0) return;
      setActiveIndex((i) => (i === null ? hits.length - 1 : (i - 1 + hits.length) % hits.length));
      return;
    }
    if (event.key === "Enter") {
      const hit = hits[activeIndex ?? 0];
      if (hit) openHit(hit.session_id);
    }
  }

  return (
    // The backdrop is a dismiss-on-click-outside overlay, not a control — the actual dialog is
    // the "palette" panel below, which already carries role="dialog". It stays a static div.
    // biome-ignore lint/a11y/noStaticElementInteractions: see above
    <div className="palette-backdrop" onMouseDown={handleBackdropMouseDown}>
      <div
        ref={dialogRef}
        className="palette"
        role="dialog"
        aria-modal="true"
        aria-label="Search sessions"
        tabIndex={-1}
        onKeyDown={handleKeyDown}
      >
        <input
          ref={inputRef}
          className="palette-input"
          value={query}
          placeholder="Search sessions"
          aria-label="Search sessions"
          aria-controls={resultsId}
          aria-describedby={statusId}
          // catalog.py refuses a longer query with 400 before reading anything;
          // stopping it at the input means the user never sends one and never sees the error.
          // It counts UTF-16 units where QUERY_MAX_CHARS counts code points, so astral text
          // stops a little early: stricter than the server, never laxer, so nothing 400s.
          maxLength={256}
          onChange={handleChange}
        />
        {note ? <div className="palette-note">{note}</div> : null}
        <div id={statusId} className="sr-only" role="status" aria-atomic="true">
          {status}
        </div>
        <ul id={resultsId} className="palette-hits" aria-label="Session search results">
          {hits.map((hit, index) => (
            // The list is wholly replaced on every query and rows carry no per-row state, so
            // the index is a fine key; `hit.session_id` is not unique (catalog.py returns up to
            // HITS_PER_SESSION hits per session).
            // biome-ignore lint/suspicious/noArrayIndexKey: see above
            <li key={index}>
              <button
                type="button"
                className={index === activeIndex ? "hit hit-active" : "hit"}
                data-session-id={hit.session_id}
                aria-current={index === activeIndex ? "true" : undefined}
                onClick={() => openHit(hit.session_id)}
              >
                <span className="hit-heading">
                  <span className="hit-title jp">{hit.title}</span>
                  {archived?.(hit.session_id) ? <span className="hit-badge">Archived</span> : null}
                  {roleChip(hit.role, assistantName) ? (
                    <span className="chip dim">{roleChip(hit.role, assistantName)}</span>
                  ) : null}
                  {hit.at ? <span className="hit-at mono">{timeLabel(hit.at, nowMs)}</span> : null}
                </span>
                <span className="hit-snippet">{hit.snippet}</span>
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
