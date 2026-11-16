/**
 * Hands-free conversation, the App-level half: feeds `handsFreeReducer` what the
 * window sees — the visible session's last turn and open cards, a session switch, and the
 * window's own visibility — and drives the read-aloud controller for the two things it speaks:
 * the turn's short spoken answer (owned `handsfree:<turnId>`) and,
 * once, "Approval needed on screen" when a card opens (owned `handsfree:card`, a fixed line with
 * no turn of its own). Its bridge's `message` adds the spoken-answer request to the automatic Send.
 *
 * English only for now: Enter needs a local voice for
 * `HANDS_FREE_LANGUAGE` unless a Voicebox voice is chosen, and the card line is spoken in that local
 * voice when one is installed. A turn's spoken answer is prepared the way its own Read prepares the
 * whole reply (`prepareReadAloud`, with `spokenAnswer` picking what is read): in the voice for the
 * language detected in it, and an answer with no installed voice for that language is silent, so
 * the mic re-arms. With a Voicebox voice, an answer in a language Voicebox speaks goes to Voicebox
 * instead (`playVoicebox`); should Voicebox fail before its first audio, the system voice reads it,
 * and with none for the language the mode ends (`speech_failed`). Early speech (`useEarlySpeech`)
 * whose first audio played owns the reply when its turn ends: nothing is requested again.
 *
 * Recovery: a Voicebox answer with a system voice prepared for it waits at most
 * `FIRST_AUDIO_DEADLINE_MS` after its turn ends for its first audio (early speech's first request
 * still out then stops at that deadline too, and the system voice reads the answer at once), and
 * should its audio fail later that system voice reads the whole answer, once. A device change
 * does not pause hands-free speech (the controller's devicechange listener skips `handsfree:`
 * owners), and a mic press while the loop thinks and the answer's voice is fetched drops that
 * answer, so the mic re-arms. Each hands-free send warms the voice pack (engine "pack", unless
 * down). `note` names what the loop recovered from or skipped until the next capture is heard, and
 * each such step, failure or drop is reported through `DictationApi.report`.
 *
 * Hidden window: off, listening or rearming leave the instant the
 * window is covered or minimized. Mid-turn or mid-answer (sending, thinking, waiting, speaking)
 * the answer keeps playing instead, and only once it is done does re-arming leave `hidden` rather
 * than reopening the mic — checked against the window's own visibility, not a timer, since a
 * hidden tab throttles timers and the mic must never open while hidden. Every way out of the
 * loop, the hook's own (Esc, the toggle, a session switch, pagehide, another reply's Read, leaving
 * the chat) or the Composer's (`bridge.dispatch`'s `exit`), goes through one `leave` function: an
 * answer still pending when it runs reports `dropped <reason>`; any other phase leaves quietly.
 */
import { useContext, useEffect, useReducer, useRef, useState } from "react";
import type { MarkdownParser } from "../components/Conversation/markdownParser";
import type { ReadAloudSettings } from "../components/Conversation/ReadAloud";
import { en } from "../i18n/index";
import type { SessionView, TextBlock, Turn } from "../state/view";
import type { DictationApi, HandsFreeReportEvent } from "./dictationTypes";
import {
  HANDS_FREE_OFF,
  type HandsFreeBridge,
  type HandsFreeEvent,
  type HandsFreeExit,
  type HandsFreePhase,
  type HandsFreeState,
  handsFreeReducer,
  REARM_DELAY_MS,
  speechOwner,
} from "./handsFree";
import { pickVoice, type ReadAloud } from "./readAloud";
import {
  type PreparedSpeech,
  playVoicebox,
  prepareReadAloud,
  readAloudParse,
} from "./readAloudPrepare";
import { spokenAnswer, withSpokenAnswerRequest } from "./spokenAnswer";
import { useEarlySpeech } from "./useEarlySpeech";
import type { VoiceboxSettings } from "./voiceboxApi";
import type { VoiceboxFeed } from "./voiceboxFeed";
import { type PackState, VoicePackContext, type VoicePackServices } from "./voicePackApi";

export const HANDS_FREE_LANGUAGE = "en";

/** What the loop last recovered from or skipped: the system voice read a Voicebox answer after a
 * failure (`voice_fallback`) or after no first audio came in time (`voice_slow`), the reply had
 * nothing to read (`nothing_read`) or no installed voice speaks its language (`no_voice_for`). */
export type HandsFreeNote = "voice_fallback" | "voice_slow" | "nothing_read" | "no_voice_for";

/** How long after its turn ends a Voicebox answer waits for its first audio before the system voice
 * reads it instead; an answer with no system voice for its language waits on. */
export const FIRST_AUDIO_DEADLINE_MS = 15_000;

/** How often hands-free re-warms the pack while it's on with the pack engine, so the pack's own
 * idle stop (`voice_pack_server.IDLE_STOP_S`, 300 s) never catches it between turns. */
export const KEEP_WARM_MS = 240_000;

const CARD_ANNOUNCE_OWNER = "handsfree:card";

/** The phases in which the loop sent a message and waits on its turn: only then is a finished
 * turn read, or a card announced. */
const AWAITING_TURN: readonly HandsFreePhase[] = ["sending", "thinking", "waiting"];

/** Phases with an answer still pending: `leave` reports `dropped <reason>` only when the exit cuts
 * one of these off; every other phase leaves quietly. */
const ANSWER_PENDING: readonly HandsFreePhase[] = ["sending", "thinking", "waiting", "speaking"];

async function dictationReady(api: DictationApi | null): Promise<boolean> {
  if (!api) return false;
  try {
    return (await api.status()).state === "ready";
  } catch {
    return false;
  }
}

async function hasLocalVoice(
  controller: ReadAloud,
  chosen: Readonly<Record<string, string>>,
): Promise<boolean> {
  if (!controller.supported) return false;
  const voices = await controller.loadVoices();
  return pickVoice(HANDS_FREE_LANGUAGE, voices, chosen) !== null;
}

/** States in which the pack's own server is up enough to speak through, even "stopped" (not yet
 * started: Enter warms it from there). Anything else — not reached that point yet, torn down, or
 * "failed" — counts as down. */
const PACK_UP_STATES: readonly PackState[] = ["stopped", "starting", "ready"];

/** Failure codes that mean the engine itself is unreachable this session, not just this one reply:
 * once seen, the rest of the session falls back to the system voice. */
const ENGINE_DOWN_CODES: readonly string[] = [
  "voicebox_unreachable",
  "voicebox_pack_missing",
  "voicebox_pack_failed",
];

/** Enter's preflight: can this session speak through the chosen engine? "pack" asks the installed
 * server's own status and, once up, warms it (fire-and-forget, errors ignored); "voicebox" (today's
 * default) asks the chosen app directly. A throw, no pack context, or any other state counts as
 * down. */
async function voiceboxUp(
  voicebox: VoiceboxSettings,
  pack: VoicePackServices | null,
): Promise<boolean> {
  if (voicebox.engine === "pack") {
    if (!pack) return false;
    try {
      const status = await pack.api.status();
      if (!PACK_UP_STATES.includes(status.state)) return false;
      void pack.api.act("warm").catch(() => {});
      return true;
    } catch {
      return false;
    }
  }
  try {
    return (await voicebox.api.status()).state === "ready";
  } catch {
    return false;
  }
}

export interface UseHandsFreeOptions {
  /** Null outside `DictationContext`'s provider: Enter then always says why (no dictation). */
  dictationApi: DictationApi | null;
  /** The visible session, or undefined with none selected: Enter then has no turn to watch. */
  view: SessionView | undefined;
  readAloud: ReadAloudSettings;
  /** Changes on a session switch (the editor key or selected session id) — anything else about
   * `view` identifies the same chat and must not itself end the mode. */
  sessionKey: string | null;
  /** The window's Markdown worker (`MarkdownParserContext`); null parses on the main thread. */
  parser: MarkdownParser | null;
  /** The chat's message box is on screen. Leaving it (another view, an archive preview) ends the
   * mode, so a returning Composer never opens the mic without a click. */
  chatShown: boolean;
}

export interface UseHandsFree {
  state: HandsFreeState;
  /** What the loop last recovered from or skipped; null again once the next capture is heard or
   * the mode ends. */
  note?: HandsFreeNote | null;
  /** A Voicebox answer is being fetched after its turn ended (early speech's first request still
   * out, or the answer's own): until its first audio plays, the system voice takes over, or the
   * request fails or is dropped. */
  preparing?: boolean;
  /** The Composer's side of the loop. */
  bridge: HandsFreeBridge;
  /** The toggle button's click: leaves from any phase, enters only from `off`. */
  toggle(): void;
  confirmNotice(): void;
  cancelNotice(): void;
  dismissNotice(): void;
  /** The strip's own interrupt button (`micPressed`, as the Composer's mic sends it): speaking
   * moves straight to listening, matching a physical mic press; while the loop thinks and the
   * answer's voice is being fetched, that answer is dropped and the mic re-arms. */
  interrupt(): void;
}

function isText(block: Turn["blocks"][number]): block is TextBlock {
  return block.kind === "text";
}

/** The final reply's text: what follows the turn's last tool call (a "let me check" before the
 * tools is not the answer), or all of its text when nothing follows one. */
function answerBlocks(turn: Turn): TextBlock[] {
  const lastTools = turn.blocks.findLastIndex((block) => block.kind === "tools");
  const answer = turn.blocks.slice(lastTools + 1).filter(isText);
  return answer.length > 0 ? answer : turn.blocks.filter(isText);
}

export function useHandsFree(options: UseHandsFreeOptions): UseHandsFree {
  const optionsRef = useRef(options);
  optionsRef.current = options;
  const entryIntent = useRef(0);
  const entryOwner = useRef({ key: options.sessionKey, shown: options.chatShown });
  // A key round trip must still invalidate an entry waiting on readiness.
  if (
    entryOwner.current.key !== options.sessionKey ||
    entryOwner.current.shown !== options.chatShown
  ) {
    entryOwner.current = { key: options.sessionKey, shown: options.chatShown };
    entryIntent.current += 1;
  }
  const [state, dispatch] = useReducer(handsFreeReducer, HANDS_FREE_OFF);
  // The phase an await resumes into, which may no longer be the one it started in.
  const phaseRef = useRef(state.phase);
  phaseRef.current = state.phase;
  const packServices = useContext(VoicePackContext);
  // Whether the chosen engine is down for this whole session: Enter's own preflight, or a down-set
  // failure code seen later. Read at render by `useEarlySpeech` below (so `useState`); the ref lets
  // the same effect that detects a failure read the fresh value right away, since the state update
  // it schedules only lands on the next render.
  const engineDownRef = useRef(false);
  const [engineDown, setEngineDownState] = useState(false);
  function markEngineDown(down: boolean): void {
    engineDownRef.current = down;
    setEngineDownState(down);
  }
  const [note, setNote] = useState<HandsFreeNote | null>(null);
  // The turn whose Voicebox answer is being fetched: `preparing` for the strip, and the turn a mic
  // press drops meanwhile.
  const preparingRef = useRef<string | null>(null);
  const [preparing, setPreparingState] = useState(false);
  function setPreparing(turnId: string | null): void {
    preparingRef.current = turnId;
    setPreparingState(turnId !== null);
  }
  // The Voicebox answer playing now: its feed, whose failure code says why its audio stopped, and
  // the system voice prepared for it (null with none installed for its language).
  const fallbackRef = useRef<{
    owner: string;
    fallback: PreparedSpeech | null;
    feed: VoiceboxFeed;
  } | null>(null);
  // Set while the speaking check restarts that answer in the system voice, whose own begin and
  // play notify it.
  const restartingRef = useRef(false);
  function report(event: HandsFreeReportEvent, code: string): void {
    optionsRef.current.dictationApi?.report?.(event, code);
  }
  /** Every way out of the loop, hook-side and the Composer's (`bridge.dispatch`'s own `exit`)
   * alike: an answer still pending (`ANSWER_PENDING`) reports `dropped <reason>` before the exit
   * lands; leaving from any other phase — including a hidden window's own re-arm, once the answer
   * already played — stays quiet. The loop reads as off from then on, so a second way out
   * in the same commit (a session switch that also takes the chat off screen) reports nothing. */
  function leave(reason: HandsFreeExit): void {
    entryIntent.current += 1;
    if (ANSWER_PENDING.includes(phaseRef.current)) report("dropped", reason);
    dispatch({ type: "exit", reason });
    phaseRef.current = "off";
  }
  // biome-ignore lint/correctness/useExhaustiveDependencies: markEngineDown and setPreparing only close over refs and state setters, so a fresh identity each render changes nothing.
  useEffect(() => {
    if (state.phase !== "off") return;
    markEngineDown(false);
    setNote(null);
    setPreparing(null);
    fallbackRef.current = null;
  }, [state.phase]);

  // Any state: the window's own visibility, a session switch and a page unload. Off, listening or
  // rearming leave the instant the window hides; mid-turn or mid-answer the answer keeps playing,
  // and the rearming effect below leaves once it is done if the window is still hidden, instead of
  // reopening the mic.
  // biome-ignore lint/correctness/useExhaustiveDependencies: leave only closes over refs, report and dispatch, so a fresh identity each render changes nothing.
  useEffect(() => {
    function onVisibility(): void {
      if (document.visibilityState !== "hidden") return;
      if (["off", "listening", "rearming"].includes(phaseRef.current)) leave("hidden");
    }
    function onPageHide(): void {
      leave("pagehide");
    }
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("pagehide", onPageHide);
    return () => {
      entryIntent.current += 1;
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("pagehide", onPageHide);
    };
  }, []);

  // Esc from anywhere while on. On `document`, so it runs after the focused element's own handler
  // (the Composer's Esc, which marks the key handled) and before every window listener (App's page
  // Back among them, which then finds it handled); a dialog's capture listener, an open menu or an
  // IME keeps the key. The open capture closes on the Composer's falling edge.
  const on = state.phase !== "off";
  // biome-ignore lint/correctness/useExhaustiveDependencies: leave only closes over refs, report and dispatch, so a fresh identity each render changes nothing.
  useEffect(() => {
    if (!on) return;
    function onKeyDown(event: KeyboardEvent): void {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      if (event.isComposing || event.keyCode === 229) return;
      if (document.querySelector('[role="menu"]')) return;
      event.preventDefault();
      leave("esc");
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [on]);

  // Keeps the pack warm while hands-free is on with the pack engine, so the pack's own idle stop
  // never catches it between turns; errors ignored, as Enter's and sendAccepted's own warms
  // already are. Cleared the moment the phase returns to off, and on unmount.
  useEffect(() => {
    if (!on || optionsRef.current.readAloud.voicebox?.engine !== "pack" || packServices === null) {
      return;
    }
    const id = window.setInterval(() => {
      void packServices.api.act("warm").catch(() => {});
    }, KEEP_WARM_MS);
    return () => window.clearInterval(id);
  }, [on, packServices]);

  const sessionKeyRef = useRef(options.sessionKey);
  // biome-ignore lint/correctness/useExhaustiveDependencies: leave only closes over refs, report and dispatch, so a fresh identity each render changes nothing.
  useEffect(() => {
    if (sessionKeyRef.current === options.sessionKey) return;
    sessionKeyRef.current = options.sessionKey;
    leave("session");
  }, [options.sessionKey]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: leave only closes over refs, report and dispatch, so a fresh identity each render changes nothing.
  useEffect(() => {
    if (!options.chatShown) leave("view");
  }, [options.chatShown]);

  // Half-duplex: a playback this loop does not own, such as a reply's own
  // Read, never plays into an open capture. One that begins while listening ends the mode quietly,
  // at its `begin`; the Composer's falling edge then cancels the capture, and the Read plays. One
  // already playing when the loop enters rearming or speaking — its `begin` fired before this
  // subscription existed, so no later notify will ever report it — is caught by this same check,
  // run once up front; entering listening skips that immediate check, since the toggle's own
  // capture start is itself what legitimately cuts a user's Read there.
  // biome-ignore lint/correctness/useExhaustiveDependencies: leave only closes over refs, report and dispatch, so a fresh identity each render changes nothing.
  useEffect(() => {
    if (!["listening", "rearming", "speaking"].includes(state.phase)) return;
    const controller = optionsRef.current.readAloud.controller;
    const check = () => {
      const owner = controller.owner();
      if (owner !== null && !owner.startsWith("handsfree:")) {
        leave("read");
      }
    };
    if (state.phase !== "listening") check();
    return controller.subscribe(check);
  }, [state.phase]);

  const cardsOpenRef = useRef(false);
  const cardsOpen = (options.view?.cards.length ?? 0) > 0;
  useEffect(() => {
    if (cardsOpenRef.current === cardsOpen) return;
    cardsOpenRef.current = cardsOpen;
    dispatch({ type: "card", open: cardsOpen });
  }, [cardsOpen]);

  // A reply's parse requests still out, cancelled with the window's worker when this unmounts.
  const parseOwners = useRef(new Set<object>());
  useEffect(() => {
    const owners = parseOwners.current;
    return () => {
      for (const owner of owners) optionsRef.current.parser?.cancel(owner);
      owners.clear();
    };
  }, []);

  // The visible session's last turn, read once per finish: a running turn is not watched again
  // until its status (or id) actually changes, so a mid-turn re-render never re-fires this. Every
  // finish is recorded, but only one the loop waits on is read: with the mode off (at launch,
  // after a session switch, on a typed send), listening or speaking, nothing is said for it.
  const lastTurnRef = useRef<{ id: string; status: Turn["status"] } | null>(null);
  const lastTurn = options.view?.turns.at(-1);
  // Before the finish effect, so that in one commit early speech's own effects run first: a card's
  // `waiting` has already stopped it when `finish` reads it.
  const early = useEarlySpeech({
    phase: state.phase,
    turn: lastTurn,
    controller: options.readAloud.controller,
    voicebox: engineDown ? null : (options.readAloud.voicebox ?? null),
  });
  // The owner of a turn's answer while its Voicebox request is out.
  const requestOwnerRef = useRef<string | null>(null);
  // lastTurn is read off `options.view` on purpose: re-running on an unrelated view change (not
  // this turn's id or status) would re-prepare speech already decided for it, and the ref guard
  // above makes a repeat of the same id and status a no-op either way.
  // biome-ignore lint/correctness/useExhaustiveDependencies: see above; options are read fresh through optionsRef, and early.finish reads only refs.
  useEffect(() => {
    if (!lastTurn || lastTurn.status === "running") return;
    if (lastTurnRef.current?.id === lastTurn.id && lastTurnRef.current.status === lastTurn.status) {
      return;
    }
    lastTurnRef.current = { id: lastTurn.id, status: lastTurn.status };
    const awaiting = () => AWAITING_TURN.includes(phaseRef.current);
    if (!awaiting()) return;
    const turnId = lastTurn.id;
    if (lastTurn.status === "cancelled") {
      dispatch({ type: "turnFinished", turnId, outcome: "stopped" });
      return;
    }
    if (lastTurn.status === "error") {
      dispatch({ type: "turnFinished", turnId, outcome: "failed" });
      return;
    }
    const { controller, voices, rate, voicebox = null } = optionsRef.current.readAloud;
    const parse = readAloudParse(optionsRef.current.parser, parseOwners.current);
    // Overtaken by a newer turn, or the loop left, while an await was out: say nothing for it.
    const stale = () => lastTurnRef.current?.id !== turnId || !awaiting();
    // The first audio's deadline counts from the turn's end. It bounds early speech's first request
    // still out then, and a Voicebox answer with a system voice to fall back to.
    const deadline = new AbortController();
    const timer = window.setTimeout(() => deadline.abort(), FIRST_AUDIO_DEADLINE_MS);
    const owner = speechOwner(turnId);
    void (async () => {
      // Early speech's first request still out (its begin held, nothing playing) is the answer's
      // voice being fetched. Once the deadline passes it stops, and the answer below falls back at
      // once, as that deadline has already passed.
      if (controller.owner() === owner && controller.statusOf(owner) === "idle") {
        setPreparing(turnId);
      }
      const stopEarly = () => controller.stopIf(owner);
      deadline.signal.addEventListener("abort", stopEarly);
      // Early speech whose first audio played owns this reply: nothing is requested a second time.
      const early1 = await early.finish(turnId, lastTurn);
      deadline.signal.removeEventListener("abort", stopEarly);
      if (early1.failure !== null && ENGINE_DOWN_CODES.includes(early1.failure)) {
        markEngineDown(true);
      }
      if (early1.played) {
        if (!stale()) dispatch({ type: "turnFinished", turnId, outcome: "speakable" });
        return;
      }
      const outcome = await prepareReadAloud(
        answerBlocks(lastTurn),
        parse,
        controller,
        voices,
        spokenAnswer,
        voicebox !== null && !engineDownRef.current,
        voicebox?.languages,
      );
      if (stale()) return;
      if (outcome.kind === "voicebox" && voicebox !== null) {
        const { fallback } = outcome.speech;
        const ticket = controller.begin(owner);
        requestOwnerRef.current = owner;
        setPreparing(turnId);
        const { result, code, feed } = await playVoicebox(
          controller,
          ticket,
          owner,
          outcome.speech,
          voicebox.api,
          rate,
          fallback === null ? undefined : deadline.signal,
        );
        if (preparingRef.current === turnId) setPreparing(null);
        if (requestOwnerRef.current === owner) requestOwnerRef.current = null;
        if (code !== null && ENGINE_DOWN_CODES.includes(code)) markEngineDown(true);
        if (stale()) {
          controller.stopIf(owner);
          return;
        }
        if (result === "playing") fallbackRef.current = { owner, fallback, feed };
        // A fallback whose own play failed still holds its begin, with nothing playing.
        const failure =
          result === "failed"
            ? (code ?? "unavailable")
            : result === "fallback" && controller.statusOf(owner) === "idle"
              ? "synth_failed"
              : null;
        if (result === "fallback" && failure === null) {
          setNote(code === "voicebox_timeout" ? "voice_slow" : "voice_fallback");
          report("fallback", code ?? "unavailable");
        }
        dispatch({ type: "turnFinished", turnId, outcome: "speakable" });
        // Voicebox failed before any audio and no system voice reads the reply: the mode ends.
        if (failure !== null) {
          report("failed", failure);
          controller.stopIf(owner);
          dispatch({ type: "speechFailed", turnId });
        }
        return;
      }
      if (outcome.kind !== "speak") {
        setNote(outcome.kind === "no_voice" ? "no_voice_for" : "nothing_read");
        report("silent", outcome.kind);
        dispatch({ type: "turnFinished", turnId, outcome: "silent" });
        return;
      }
      const ticket = controller.begin(owner);
      dispatch({ type: "turnFinished", turnId, outcome: "speakable" });
      if (!controller.play(ticket, owner, outcome.speech.chunks, outcome.speech.voice, rate)) {
        report("failed", "synth_failed");
        dispatch({ type: "speechFailed", turnId });
      }
    })().finally(() => {
      window.clearTimeout(timer);
      if (preparingRef.current === turnId) setPreparing(null);
    });
  }, [lastTurn?.id, lastTurn?.status]);

  // A Voicebox request still out stops the moment the loop stops waiting on its turn (Esc or any
  // other way out), so its late answer plays nothing.
  useEffect(() => {
    if (requestOwnerRef.current === null || AWAITING_TURN.includes(state.phase)) return;
    optionsRef.current.readAloud.controller.stopIf(requestOwnerRef.current);
    requestOwnerRef.current = null;
  }, [state.phase]);

  // Whatever is speaking under this loop's name stops the moment the loop leaves `speaking`, by
  // any path (a card, Esc, a failed turn, the strip's own interrupt).
  const speakingOwnerRef = useRef<string | null>(null);
  useEffect(() => {
    const owner = state.phase === "speaking" && state.turnId ? speechOwner(state.turnId) : null;
    if (owner) {
      speakingOwnerRef.current = owner;
      return;
    }
    if (speakingOwnerRef.current) {
      optionsRef.current.readAloud.controller.stopIf(speakingOwnerRef.current);
      speakingOwnerRef.current = null;
    }
  }, [state.phase, state.turnId]);

  // The controller notifies on every change; while this loop owns the current playback, its end
  // or failure is read straight off `statusOf`, including one already settled before this ran.
  // A failed Voicebox answer is read again, whole, by the system voice prepared for it, once; with
  // none, or when that fails too, the mode ends.
  // biome-ignore lint/correctness/useExhaustiveDependencies: report and markEngineDown only touch refs and state setters, so a fresh identity each render changes nothing.
  useEffect(() => {
    if (state.phase !== "speaking" || state.turnId === null) return;
    const turnId = state.turnId;
    const owner = speechOwner(turnId);
    const controller = optionsRef.current.readAloud.controller;
    const failed = () => {
      const playing = fallbackRef.current?.owner === owner ? fallbackRef.current : null;
      fallbackRef.current = null;
      const code = playing?.feed.failure() ?? null;
      if (code !== null && ENGINE_DOWN_CODES.includes(code)) markEngineDown(true);
      const fallback = playing?.fallback ?? null;
      if (fallback === null) {
        report("failed", playing === null ? "playback_failed" : (code ?? "audio_failed"));
        dispatch({ type: "speechFailed", turnId });
        return;
      }
      restartingRef.current = true;
      let started = false;
      try {
        const ticket = controller.begin(owner);
        const { rate } = optionsRef.current.readAloud;
        started = controller.play(ticket, owner, fallback.chunks, fallback.voice, rate);
        if (!started) controller.stopIf(owner);
      } finally {
        restartingRef.current = false;
      }
      // A system voice that failed inside `play` notified while the guard was up: read it here.
      if (started && controller.statusOf(owner) === "failed") started = false;
      if (started) {
        setNote("voice_fallback");
        report("fallback", code ?? "audio_failed");
        return;
      }
      report("failed", "synth_failed");
      dispatch({ type: "speechFailed", turnId });
    };
    const check = () => {
      if (restartingRef.current) return;
      const status = controller.statusOf(owner);
      if (status === "failed") {
        failed();
      } else if (status === "idle") {
        dispatch({ type: "speechEnded", turnId });
      }
    };
    check();
    return controller.subscribe(check);
  }, [state.phase, state.turnId]);

  // The pause before the mic re-arms (speech ended, or the turn had nothing to speak). A window
  // still hidden when rearming starts (hidden mid-answer, say) leaves `hidden` at once instead:
  // checked against the window's own visibility here, not left to the timer, since a hidden tab
  // throttles timers and the mic must never reopen while hidden.
  // biome-ignore lint/correctness/useExhaustiveDependencies: leave only closes over refs, report and dispatch, so a fresh identity each render changes nothing.
  useEffect(() => {
    if (state.phase !== "rearming") return;
    if (document.visibilityState === "hidden") {
      leave("hidden");
      return;
    }
    const id = window.setTimeout(() => dispatch({ type: "rearm" }), REARM_DELAY_MS);
    return () => window.clearTimeout(id);
  }, [state.phase]);

  // "Approval needed on screen", once per card (hard rule 6: voice never answers it).
  const announcedRef = useRef(false);
  useEffect(() => {
    if (state.phase !== "waiting") {
      announcedRef.current = false;
      return;
    }
    if (announcedRef.current) return;
    announcedRef.current = true;
    const { controller, voices, rate } = optionsRef.current.readAloud;
    void (async () => {
      if (!controller.supported) return;
      const installed = await controller.loadVoices();
      const voice = pickVoice(HANDS_FREE_LANGUAGE, installed, voices);
      // The card closed, or the mode ended, while the voices loaded: say nothing. Nor while a
      // Voicebox answer is still out: this line's begin would cut it, so the answer plays instead,
      // as a system-voice answer already playing does.
      if (!voice || phaseRef.current !== "waiting" || requestOwnerRef.current !== null) return;
      const ticket = controller.begin(CARD_ANNOUNCE_OWNER);
      // In English: the voice is HANDS_FREE_LANGUAGE's.
      controller.play(ticket, CARD_ANNOUNCE_OWNER, [en["handsfree.announce.card"]], voice, rate);
    })();
  }, [state.phase]);

  function canStartListening(): boolean {
    const now = optionsRef.current;
    return (
      document.visibilityState !== "hidden" &&
      now.chatShown &&
      (now.view?.cards.length ?? 0) === 0 &&
      now.view?.turns.at(-1)?.status !== "running"
    );
  }

  async function toggle(): Promise<void> {
    const intent = ++entryIntent.current;
    if (state.phase !== "off") {
      leave("toggle");
      return;
    }
    const { dictationApi, readAloud, sessionKey } = optionsRef.current;
    const voicebox = readAloud.voicebox ?? null;
    const [ready, up] = await Promise.all([
      dictationReady(dictationApi),
      voicebox ? voiceboxUp(voicebox, packServices) : Promise.resolve(false),
    ]);
    if (intent !== entryIntent.current) return;
    // Up, a Voicebox voice speaks English answers too: Enter then needs no system voice. Down, the
    // session runs on system voices, like one without Voicebox.
    const voice =
      voicebox && up ? true : await hasLocalVoice(readAloud.controller, readAloud.voices);
    // The window hid, the chat left the screen or a second click entered while those were read.
    const now = optionsRef.current;
    if (
      intent !== entryIntent.current ||
      sessionKey !== now.sessionKey ||
      phaseRef.current !== "off" ||
      !canStartListening()
    ) {
      return;
    }
    markEngineDown(voicebox !== null && !up);
    dispatch({
      type: "enter",
      dictationReady: ready,
      localVoice: voice,
      noticeSeen: optionsRef.current.readAloud.noticeSeen,
    });
  }

  function confirmNotice(): void {
    if (!canStartListening()) {
      dispatch({ type: "noticeCancelled" });
      return;
    }
    optionsRef.current.readAloud.acknowledge();
    dispatch({ type: "noticeConfirmed" });
  }

  function cancelNotice(): void {
    entryIntent.current += 1;
    dispatch({ type: "noticeCancelled" });
  }

  function dismissNotice(): void {
    entryIntent.current += 1;
    dispatch({ type: "dismiss" });
  }

  /** The Composer's events and the strip's interrupt on their way to the reducer: the next capture
   * heard clears the note, an accepted send warms the voice pack, and a mic press while the loop
   * thinks and the answer's voice is fetched drops that answer instead. */
  function composerEvent(event: HandsFreeEvent): void {
    const preparingTurn = preparingRef.current;
    if (event.type === "micPressed" && preparingTurn !== null && phaseRef.current === "thinking") {
      report("dropped", "mic");
      // Leaving thinking, the request-owner effect above stops the voice; the mic then re-arms.
      dispatch({ type: "turnFinished", turnId: preparingTurn, outcome: "silent" });
      return;
    }
    if (event.type === "exit") {
      leave(event.reason);
      return;
    }
    if (event.type === "captureDone") setNote(null);
    if (event.type === "sendAccepted" && !engineDownRef.current) {
      const pack = optionsRef.current.readAloud.voicebox?.engine === "pack" ? packServices : null;
      // A pack its idle stop shut down starts while Claude thinks; errors ignored, as Enter's are.
      void pack?.api.act("warm").catch(() => {});
    }
    dispatch(event);
  }

  function interrupt(): void {
    composerEvent({ type: "micPressed" });
  }

  // Retained children see this render before the session-exit effect clears the prior mode.
  const ownsSession = sessionKeyRef.current === options.sessionKey && options.chatShown;
  const bridge: HandsFreeBridge = {
    active: ownsSession && state.phase !== "off",
    listening: ownsSession && state.phase === "listening",
    dispatch: composerEvent,
    message: withSpokenAnswerRequest,
  };

  return {
    state,
    note,
    preparing,
    bridge,
    toggle: () => void toggle(),
    confirmNotice,
    cancelNotice,
    dismissNotice,
    interrupt,
  };
}
