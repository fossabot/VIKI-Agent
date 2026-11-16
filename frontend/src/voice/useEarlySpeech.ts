/**
 * Hands-free's "Speak while VIKI writes" (`VoiceboxSettings.speakWhileWriting`). While the loop is
 * `sending` or `thinking` and the turn runs, `createSpokenStream` cuts the first paragraph of each
 * text stretch into sentences and a Voicebox feed speaks them under the turn's own hands-free
 * owner (`speechOwner`). The first chunks fix the language; one Voicebox does not speak declines
 * the turn. The speech stops when the loop leaves `sending`, `thinking` and `speaking`, when a new
 * turn id takes over, and on unmount. `finish` hands over the rest at the turn's end and says
 * whether this speech owns the reply.
 */
import { useEffect, useRef } from "react";
import type { Turn } from "../state/view";
import { type HandsFreePhase, speechOwner } from "./handsFree";
import type { ReadAloud } from "./readAloud";
import { detectLanguage } from "./readAloudText";
import { createSpokenStream } from "./sentences";
import { VOICEBOX_LANGUAGES, type VoiceboxSettings } from "./voiceboxApi";
import { createVoiceboxFeed, startAudio, type VoiceboxFeed } from "./voiceboxFeed";

/** One turn's early speech. */
interface Session {
  turnId: string;
  stream: ReturnType<typeof createSpokenStream>;
  feed: VoiceboxFeed | null;
  state: "collecting" | "declined" | "playing" | "failed" | "stopped";
  /** Whether the first chunk played: false once it went stale or failed. */
  started: Promise<boolean> | null;
}

/** Outside these phases, a session still collecting or playing stops. */
const KEPT_PHASES: readonly HandsFreePhase[] = ["sending", "thinking", "speaking"];

/** Only a session that began stops its owner: one still collecting owns nothing, and the turn's
 * end may already be preparing its answer under that same owner. */
function stopSession(session: Session | null, controller: ReadAloud): void {
  if (session?.state !== "collecting" && session?.state !== "playing") return;
  const began = session.state === "playing";
  session.state = "stopped";
  if (began) controller.stopIf(speechOwner(session.turnId));
}

export interface EarlySpeechFinish {
  /** Whether early speech's own audio played and owns the reply: nothing is requested again. */
  played: boolean;
  /** The first request's failure code, when the session failed; null otherwise — including a mere
   * stop or preemption, which is not a failure. */
  failure: string | null;
}

export function useEarlySpeech({
  phase,
  turn,
  controller,
  voicebox,
}: {
  phase: HandsFreePhase;
  turn: Turn | undefined;
  controller: ReadAloud;
  voicebox: VoiceboxSettings | null;
}): { finish(turnId: string, turn: Turn): Promise<EarlySpeechFinish> } {
  const sessionRef = useRef<Session | null>(null);

  useEffect(() => {
    if (!voicebox?.speakWhileWriting || (phase !== "sending" && phase !== "thinking")) return;
    if (turn?.status !== "running") return;
    if (sessionRef.current?.turnId !== turn.id) {
      stopSession(sessionRef.current, controller);
      sessionRef.current = {
        turnId: turn.id,
        stream: createSpokenStream(),
        feed: null,
        state: "collecting",
        started: null,
      };
    }
    const session = sessionRef.current;
    if (session.state !== "collecting" && session.state !== "playing") return;
    const chunks = session.stream.update(turn.blocks, false);
    if (session.feed) {
      for (const { text, group } of chunks) session.feed.push(text, group);
      return;
    }
    if (chunks.length === 0) return;
    const lang = detectLanguage(chunks.map((chunk) => chunk.text).join(" "));
    const languages = voicebox.languages ?? VOICEBOX_LANGUAGES;
    if (lang === null || !languages.includes(lang)) {
      session.state = "declined";
      return;
    }
    const owner = speechOwner(turn.id);
    const ticket = controller.begin(owner);
    const { api } = voicebox;
    const feed = createVoiceboxFeed((text, signal) => api.speech(text, lang, signal));
    for (const { text, group } of chunks) feed.push(text, group);
    session.feed = feed;
    session.state = "playing";
    session.started = startAudio(controller, ticket, owner, feed).then((result) => {
      if (result === "playing") return true;
      if (result === "stale") {
        session.state = "stopped";
      } else {
        session.state = "failed";
        // Its begin is still current: release it, as nothing will play under it.
        controller.stopIf(owner);
      }
      return false;
    });
  }, [turn, phase, voicebox, controller]);

  useEffect(() => {
    if (!KEPT_PHASES.includes(phase)) stopSession(sessionRef.current, controller);
  }, [phase, controller]);

  useEffect(() => () => stopSession(sessionRef.current, controller), [controller]);

  async function finish(turnId: string, final: Turn): Promise<EarlySpeechFinish> {
    const none: EarlySpeechFinish = { played: false, failure: null };
    const session = sessionRef.current;
    if (session?.turnId !== turnId) return none;
    if (session.state === "failed") {
      return { played: false, failure: session.feed?.failure() ?? "unavailable" };
    }
    if (session.state !== "playing") return none;
    const { feed, started } = session;
    if (feed === null || started === null) return none;
    for (const { text, group } of session.stream.update(final.blocks, true)) feed.push(text, group);
    feed.end();
    const played = await started;
    if (played) return { played: true, failure: null };
    return { played: false, failure: feed.failure() ?? "unavailable" };
  }

  return { finish };
}
