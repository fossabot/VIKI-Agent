/**
 * usePresence: reports this window's presence to POST /api/v2/decision/presence (the exact
 * `PresenceBody`). It posts on a
 * change to any of its inputs (debounced 1 s) and again every 15 s while the tab is visible;
 * nothing is ever posted while hidden, and becoming visible posts once right away. `typing`
 * tracks a document-wide `input` event: true the instant one fires, held for 3 s after the last
 * one, then false — the event's target and value are never read, only that one occurred.
 *
 * The caller passes the facts it already tracks (`window`, `focused`, `chat`, `chatTitle`,
 * `note`, `launcher`, `page` — typically from `pageFacts()`); this hook builds the exact wire
 * body: `chat`/`note` fall back to null unless their ids are canonical UUIDs, `chat_title` is
 * clipped to 120 characters with control characters (and lone surrogates) stripped, and `note`
 * falls back to null as a whole unless its `ref` is 1 to 512 clean characters — so a malformed
 * value never gets the whole report refused by the server's strict parser (the server's
 * `parse_presence` requires exactly these keys and those exact bounds).
 *
 * The body stays within the server's 4,096-byte cap: `fitDrafts` drops the `sources` part, then
 * the `context` part, before `fitSecondLook` trims the second look's refs.
 *
 * At most one request is in flight at a time: a slow answer delays the next post rather than
 * racing it, and only the latest body stays queued behind it (it never stacks). Presence is best
 * effort — a failed post is swallowed quietly, with nothing retried beyond the normal cadence and
 * nothing logged.
 *
 * Returns `lastInputMs` (the `Date.now()` of the most recent `input` event, or null before the
 * first one) so a caller such as the launcher can hold its own nudge cue while typing without
 * installing a second listener.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { DecisionLoopApi, PageFacts, PresenceBody } from "../../clients/decisionLoop/types";

const DEBOUNCE_MS = 1_000;
const HEARTBEAT_MS = 15_000;
const TYPING_HOLD_MS = 3_000;
const MAX_CHAT_TITLE = 120;
const MAX_NOTE_REF = 512;
/** The server's cap on a presence body (`api.MAX_DECISION_BODY_BYTES`); above it the whole report
 * is refused (413). */
const MAX_BODY_BYTES = 4_096;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/** A note reference in the app's own camelCase, as the rest of the app already names one (e.g.
 * `App.tsx`'s open-file state); `buildBody` below renders it onto the wire's `{workspace_id,
 * ref}`. */
export interface PresenceNote {
  workspaceId: string;
  ref: string;
}

export interface UsePresenceOptions {
  /** This window's id, stable for the whole mount; forwarded as-is. */
  window: string;
  focused: boolean;
  /** The open chat's session id, or null; sent only when it is a canonical UUID. */
  chat: string | null;
  /** The open chat's title, or null; clipped to 120 characters with control characters removed. */
  chatTitle: string | null;
  /** The open note, or null; sent only when `workspaceId` is a canonical UUID and `ref` is 1 to
   * 512 characters without control characters. */
  note: PresenceNote | null;
  /** Whether the VIKI launcher panel is open. */
  launcher: boolean;
  /** This window's page facts (`pageFacts()`), or null while unsettled. */
  page: PageFacts | null;
}

export interface UsePresenceResult {
  /** `Date.now()` at the most recent document `input` event, or null before the first one. */
  lastInputMs: number | null;
}

/** A control character (Cc: U+0000-U+001F, U+007F-U+009F) or a lone surrogate (Cs: unpaired, so
 * `codePointAt` on it answers its own 0xD800-0xDFFF value rather than a combined code point) —
 * the two classes `_text` refuses server-side. `ch` is one element of `Array.from(text)`, so a
 * valid surrogate pair already reads as a single (non-surrogate-range) code point here. */
function isUnsafeCodePoint(ch: string): boolean {
  const code = ch.codePointAt(0) ?? 0;
  return code <= 0x1f || (code >= 0x7f && code <= 0x9f) || (code >= 0xd800 && code <= 0xdfff);
}

/** `raw` with every unsafe code point removed, then clipped to `MAX_CHAT_TITLE` code points
 * (counted the way the server counts them, not UTF-16 code units). */
function sanitizeTitle(raw: string): string {
  return Array.from(raw)
    .filter((ch) => !isUnsafeCodePoint(ch))
    .slice(0, MAX_CHAT_TITLE)
    .join("");
}

/** Whether `ref` alone would pass the server's bounds: 1 to `MAX_NOTE_REF` code points, none
 * unsafe. A ref is never trimmed to fit (unlike a title, cutting it could point at the wrong
 * file) — it is sent whole or not at all. */
function isCleanRef(ref: string): boolean {
  const chars = Array.from(ref);
  return chars.length >= 1 && chars.length <= MAX_NOTE_REF && !chars.some(isUnsafeCodePoint);
}

function isCanonicalUuid(value: string): boolean {
  return UUID.test(value);
}

function bodyBytes(body: PresenceBody): number {
  return new TextEncoder().encode(JSON.stringify(body)).length;
}

/** `body` with its second-look part kept only while the whole body fits `MAX_BODY_BYTES`: refs go
 * from the end first, then the part itself (the server refuses a part with no ref). A body over the
 * cap without the part is left as it was built, and so is a part whose chat or turn id is not a
 * canonical UUID, which the server would refuse whole. */
function fitSecondLook(body: PresenceBody): PresenceBody {
  const page = body.page;
  const look = page?.second_look;
  if (!page || !look) return body;
  const withRefs = (refs: string[]): PresenceBody => ({
    ...body,
    page: { ...page, second_look: refs.length > 0 ? { ...look, refs } : null },
  });
  let refs = isCanonicalUuid(look.session) && isCanonicalUuid(look.turn) ? look.refs : [];
  while (refs.length > 0 && bodyBytes(withRefs(refs)) > MAX_BODY_BYTES) refs = refs.slice(0, -1);
  return withRefs(refs);
}

/** `body` with its context and sources parts kept only while they can be accepted: a part whose
 * chat or turn id is not a canonical UUID goes (the server would refuse the whole report), then,
 * while the body is over `MAX_BODY_BYTES`, `sources` and then `context`. Runs before
 * `fitSecondLook`, so these two small parts yield first and the second look keeps its refs. */
function fitDrafts(body: PresenceBody): PresenceBody {
  const page = body.page;
  if (!page) return body;
  const { context, sources } = page;
  const fitted: PageFacts = {
    ...page,
    context: context && isCanonicalUuid(context.session) ? context : null,
    sources:
      sources && isCanonicalUuid(sources.session) && isCanonicalUuid(sources.turn) ? sources : null,
  };
  const over = (candidate: PageFacts) => bodyBytes({ ...body, page: candidate }) > MAX_BODY_BYTES;
  if (over(fitted)) fitted.sources = null;
  if (over(fitted)) fitted.context = null;
  return { ...body, page: fitted };
}

/** The exact `PresenceBody` for `options` and the currently computed `typing`. */
function buildBody(options: UsePresenceOptions, typing: boolean): PresenceBody {
  const chat = options.chat !== null && isCanonicalUuid(options.chat) ? options.chat : null;
  const note =
    options.note !== null &&
    isCanonicalUuid(options.note.workspaceId) &&
    isCleanRef(options.note.ref)
      ? { workspace_id: options.note.workspaceId, ref: options.note.ref }
      : null;
  return fitSecondLook(
    fitDrafts({
      window: options.window,
      focused: options.focused,
      typing,
      chat,
      chat_title: options.chatTitle === null ? null : sanitizeTitle(options.chatTitle),
      note,
      launcher: options.launcher,
      page: options.page,
    }),
  );
}

export function usePresence(
  api: Pick<DecisionLoopApi, "presence">,
  options: UsePresenceOptions,
): UsePresenceResult {
  const [lastInputMs, setLastInputMs] = useState<number | null>(null);
  const [typing, setTyping] = useState(false);

  // Mirrors the latest api/options/typing for the timers below, which fire after this render
  // (and any number of later ones) have come and gone — the same pattern useLauncherPosition
  // uses for its debounced save.
  const apiRef = useRef(api);
  apiRef.current = api;
  const optionsRef = useRef(options);
  optionsRef.current = options;
  const typingRef = useRef(typing);
  typingRef.current = typing;

  const typingTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const debounceTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // The most recently *detected* body, as JSON: compared against on every options/typing change
  // so an unrelated re-render (a fresh `options` object with the same values) never restarts the
  // debounce. Updated the moment a change is detected, not when the request finally lands, so a
  // change that reverts mid-debounce is judged against what was last decided, not what shipped.
  const lastSeenRef = useRef<string | null>(null);
  const inFlightRef = useRef(false);
  const pendingRef = useRef<PresenceBody | null>(null);

  // One request in flight at a time: a call that arrives while the last is still pending only
  // replaces whatever was already queued behind it, so a slow answer delays the next post
  // instead of racing it, and nothing stacks. Wrapped in `useCallback` with no deps (it reads
  // only refs) so the effects below can list it honestly without re-running on every render.
  const send = useCallback((body: PresenceBody): void => {
    if (inFlightRef.current) {
      pendingRef.current = body;
      return;
    }
    inFlightRef.current = true;
    apiRef.current
      .presence(body)
      .catch(() => {
        // Presence is best effort: nothing retried beyond the normal cadence, nothing logged.
      })
      .finally(() => {
        inFlightRef.current = false;
        const queued = pendingRef.current;
        if (queued !== null) {
          pendingRef.current = null;
          send(queued);
        }
      });
  }, []);

  /** The single gate for "no posts while hidden": every scheduled send (debounce, heartbeat)
   * routes through this, so only the dedicated visibility handler below ever posts directly. */
  const postIfVisible = useCallback(
    (body: PresenceBody): void => {
      if (document.visibilityState !== "visible") return;
      send(body);
    },
    [send],
  );

  const scheduleDebounced = useCallback(() => {
    if (debounceTimer.current !== null) clearTimeout(debounceTimer.current);
    debounceTimer.current = setTimeout(() => {
      debounceTimer.current = null;
      postIfVisible(buildBody(optionsRef.current, typingRef.current));
    }, DEBOUNCE_MS);
  }, [postIfVisible]);

  // An `input` event anywhere in the document starts (or restarts) a 3 s "typing" window. The
  // event itself is never inspected beyond its existence.
  useEffect(() => {
    function onInput(): void {
      setLastInputMs(Date.now());
      setTyping(true);
      if (typingTimer.current !== null) clearTimeout(typingTimer.current);
      typingTimer.current = setTimeout(() => {
        typingTimer.current = null;
        setTyping(false);
      }, TYPING_HOLD_MS);
    }
    document.addEventListener("input", onInput, { passive: true });
    return () => {
      document.removeEventListener("input", onInput);
      if (typingTimer.current !== null) clearTimeout(typingTimer.current);
    };
  }, []);

  // Debounced on change: any of window/focused/chat/chatTitle/note/launcher/page, or typing
  // flipping. `options` is a fresh object every render, so the comparison is by the body's own
  // JSON, not by reference.
  useEffect(() => {
    const serialized = JSON.stringify(buildBody(options, typing));
    if (serialized === lastSeenRef.current) return;
    lastSeenRef.current = serialized;
    scheduleDebounced();
  }, [options, typing, scheduleDebounced]);

  // The 15 s heartbeat while visible, plus one immediate post the moment the tab becomes visible.
  useEffect(() => {
    const heartbeat = setInterval(() => {
      postIfVisible(buildBody(optionsRef.current, typingRef.current));
    }, HEARTBEAT_MS);
    function onVisibilityChange(): void {
      if (document.visibilityState === "visible") {
        send(buildBody(optionsRef.current, typingRef.current));
      }
    }
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      clearInterval(heartbeat);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [postIfVisible, send]);

  // Unmount: drop any pending debounce so it never fires after this hook is gone.
  useEffect(() => {
    return () => {
      if (debounceTimer.current !== null) clearTimeout(debounceTimer.current);
    };
  }, []);

  return { lastInputMs };
}
