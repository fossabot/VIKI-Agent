/**
 * `useHandsFree`'s recovery of hands-free speech, driven against the real read-aloud controller over
 * a fake speechSynthesis, a fake Voicebox api and `AudioOut`, with the server report spied on: a
 * device change mid-answer, audio failing after playback started, a first audio that never comes,
 * a reply with nothing to read, the pack warmed on each send, and a mic press while the answer's
 * voice is fetched.
 */
import { act, cleanup, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, expect, test, vi } from "vitest";
import type { ReadAloudSettings } from "../components/Conversation/ReadAloud";
import { emptyView, type SessionView, type Turn } from "../state/view";
import type { DictationApi } from "./dictationTypes";
import { HANDS_FREE_OFF, REARM_DELAY_MS } from "./handsFree";
import { type AudioOut, createReadAloud } from "./readAloud";
import { FIRST_AUDIO_DEADLINE_MS, type UseHandsFreeOptions, useHandsFree } from "./useHandsFree";
import { type VoiceboxApi, VoiceboxError, type VoiceboxSettings } from "./voiceboxApi";
import {
  type PackState,
  type VoicePackApi,
  VoicePackContext,
  type VoicePackServices,
  type VoicePackStatus,
} from "./voicePackApi";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const ZIRA = { name: "Zira", voiceURI: "Zira", lang: "en-US", localService: true, default: false };
const TEXT = "The weather is fine today and you are here.";
const A = "The first sentence is ready now.";
const B = "The second one follows right after.";
/** The fake `AudioOut` cannot decode this one's audio. */
const BROKEN = "Broken audio comes back from the engine.";
const JAPANESE = "今日は天気がとても良いので、散歩に行きます。";

const settled = () =>
  act(async () => {
    for (let i = 0; i < 20; i++) await Promise.resolve();
  });

/** Fake time moves on by `ms`, then what it set off settles. */
const advance = (ms: number) =>
  act(async () => {
    vi.advanceTimersByTime(ms);
    for (let i = 0; i < 20; i++) await Promise.resolve();
  });

function turn(id: string, status: Turn["status"], text = TEXT): Turn {
  return {
    id,
    user: null,
    blocks: [{ kind: "text", id: `${id}-b`, text, done: status !== "running" }],
    status,
    usage: null,
    durationMs: 0,
    history: false,
  };
}

const T1 = turn("t1", "success");
const chat = (turns: Turn[]): SessionView => ({ ...emptyView("s1"), turns, cards: [] });

/** A `VoicePackContext` whose status each test sets up front; `acted` records its actions. */
function fakePack(state: PackState) {
  const acted: string[] = [];
  const status: VoicePackStatus = {
    state,
    reason: null,
    error: null,
    progress: null,
    downloadBytes: 0,
    diskBytes: 0,
    needBytes: 0,
    freeBytes: null,
    gpu: null,
    kokoro: false,
    speed: { kokoro: null, qwen: null },
    fallbacks: [],
  };
  const api: VoicePackApi = {
    status: async () => ({ ...status }),
    act: async (action) => {
      acted.push(action);
      return { ...status };
    },
    openSpeechSettings: async () => {},
  };
  const services: VoicePackServices = { api, openUrl: () => {} };
  return { services, acted };
}

/**
 * The loop over `voices` as the installed system voices (a test may empty the array later) and,
 * unless `voicebox` is false, a Voicebox voice whose answers each test hands out by hand. Each
 * answer's bytes carry its text, so `played` names what sounded; `devices` is where the controller
 * hears a device change; `reports` lists every hands-free report sent. `throwOnSpeak` makes the
 * system voice's own `speak` throw.
 */
function setup({
  voices = [ZIRA] as (typeof ZIRA)[],
  voicebox = true,
  speakWhileWriting = false,
  throwOnSpeak = false,
  engine = undefined as VoiceboxSettings["engine"],
  pack = null as VoicePackServices | null,
} = {}) {
  const played: string[] = [];
  const ends: (() => void)[] = [];
  const textOf = new Map<ArrayBuffer, string>();
  const audio: AudioOut = {
    decode: async (bytes) => {
      const text = textOf.get(bytes);
      if (text === BROKEN) throw new DOMException("bad audio", "EncodingError");
      return { text } as unknown as AudioBuffer;
    },
    start(buffer, onEnded) {
      played.push((buffer as unknown as { text: string }).text);
      let done = false;
      const end = () => {
        if (done) return;
        done = true;
        onEnded();
      };
      ends.push(end);
      return end;
    },
    resume: async () => {},
    suspend: async () => {},
  };
  const requests: { text: string; signal: AbortSignal; answer(): void; fail(e: unknown): void }[] =
    [];
  // Like the real client, a request rejects with an AbortError once its signal aborts.
  const api: VoiceboxApi = {
    status: async () => ({ state: "ready", gpu: null, profiles: [] }),
    speech: (text, _language, signal) =>
      new Promise<ArrayBuffer>((resolve, reject) => {
        const bytes = new ArrayBuffer(1);
        textOf.set(bytes, text);
        requests.push({ text, signal, answer: () => resolve(bytes), fail: reject });
        signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), {
          once: true,
        });
      }),
  };
  const spoken: string[] = [];
  const utterances: { onend: (() => void) | null; onerror: (() => void) | null }[] = [];
  const synth = {
    paused: false,
    getVoices: () => voices,
    addEventListener: () => {},
    removeEventListener: () => {},
    speak: (u: { text: string; onend: (() => void) | null; onerror: (() => void) | null }) => {
      if (throwOnSpeak) throw new Error("speak refused");
      spoken.push(u.text);
      utterances.push(u);
    },
    cancel: () => {},
    pause: () => {},
    resume: () => {},
  };
  const devices = new EventTarget();
  const controller = createReadAloud({
    synth: synth as unknown as SpeechSynthesis,
    makeUtterance: (text) =>
      ({ text, voice: null, lang: "", rate: 1, onend: null, onerror: null }) as never,
    audio,
    target: new EventTarget(),
    devices,
    // A microtask: `loadVoices` settles on an empty list without waiting on real time.
    setTimeout: (cb) => {
      Promise.resolve().then(cb);
      return 0;
    },
    clearTimeout: () => {},
  });
  const report = vi.fn();
  const dictationApi = {
    status: async () => ({ state: "ready" }),
    report,
  } as unknown as DictationApi;
  const settings: VoiceboxSettings = {
    api,
    url: "http://voicebox.invalid",
    speakWhileWriting,
    ...(engine !== undefined ? { engine } : {}),
  };
  const readAloud: ReadAloudSettings = {
    controller,
    rate: 1,
    voices: {},
    noticeSeen: true,
    voicebox: voicebox ? settings : null,
    acknowledge: () => {},
  };
  let props: UseHandsFreeOptions = {
    dictationApi,
    view: chat([T1]),
    readAloud,
    sessionKey: "s1",
    parser: null,
    chatShown: true,
  };
  const hook = renderHook((options: UseHandsFreeOptions) => useHandsFree(options), {
    initialProps: props,
    wrapper: ({ children }: { children: ReactNode }) => (
      <VoicePackContext.Provider value={pack}>{children}</VoicePackContext.Provider>
    ),
  });
  const now = () => hook.result.current;
  const update = async (turns: Turn[]) => {
    props = { ...props, view: chat(turns) };
    hook.rerender(props);
    await settled();
  };
  const send = (type: "captureDone" | "sendAccepted" | "micPressed") =>
    act(() => now().bridge.dispatch({ type }));
  return {
    controller,
    devices,
    voices,
    played,
    spoken,
    requests,
    now,
    update,
    send,
    phase: () => now().state.phase,
    reports: () => report.mock.calls,
    asked: () => requests.map((request) => request.text),
    /** Enter, then listening → sending → thinking, as the Composer reports a heard, sent message. */
    async think() {
      act(() => now().toggle());
      await settled();
      send("captureDone");
      send("sendAccepted");
    },
    /** t2 runs, then finishes with `text`, while the loop waits on it. */
    async finish(text = TEXT) {
      await update([T1, turn("t2", "running", text)]);
      await update([T1, turn("t2", "success", text)]);
    },
    async answer(index: number) {
      requests[index]?.answer();
      await settled();
    },
    async fail(index: number, code: string) {
      requests[index]?.fail(new VoiceboxError(code));
      await settled();
    },
    /** Ends the Voicebox chunk now sounding. */
    async end() {
      ends.at(-1)?.();
      await settled();
    },
    /** Ends, or fails, the system voice's chunk now speaking. */
    async endSpeech(how: "onend" | "onerror" = "onend") {
      act(() => utterances.at(-1)?.[how]?.());
      await settled();
    },
  };
}

test("a device change does not pause hands-free speech; the loop re-arms", async () => {
  const t = setup();
  await t.think();
  await t.finish(`${A} ${B}`);
  await t.answer(0);
  expect(t.phase()).toBe("speaking");
  act(() => {
    t.devices.dispatchEvent(new Event("devicechange"));
  });
  expect(t.controller.statusOf("handsfree:t2")).toBe("speaking");
  await t.answer(1);
  await t.end();
  await t.end();
  expect(t.played).toEqual([A, B]);
  expect(t.phase()).toBe("rearming");
  expect(t.now().note).toBeNull();
  expect(t.reports()).toEqual([]);
});

test("an audio failure after playback started is read by the system voice", async () => {
  const t = setup();
  await t.think();
  await t.finish(`${A} ${B}`);
  await t.answer(0);
  await t.fail(1, "voicebox_timeout");
  expect(t.spoken).toEqual([]);
  // The failure surfaces once the chunk sounding ends; the whole answer is read, A again too.
  await t.end();
  expect(t.played).toEqual([A]);
  expect(t.spoken).toEqual([`${A} ${B}`]);
  expect(t.phase()).toBe("speaking");
  expect(t.controller.statusOf("handsfree:t2")).toBe("speaking");
  expect(t.now().note).toBe("voice_fallback");
  expect(t.reports()).toEqual([["fallback", "voicebox_timeout"]]);
  // Once: the system voice failing as well ends the mode.
  await t.endSpeech("onerror");
  expect(t.now().state).toEqual({ ...HANDS_FREE_OFF, notice: "speech_failed" });
  expect(t.now().note).toBeNull();
  expect(t.spoken).toEqual([`${A} ${B}`]);
  expect(t.reports()).toEqual([
    ["fallback", "voicebox_timeout"],
    ["failed", "playback_failed"],
  ]);
});

test("with no system voice an audio failure ends speech_failed", async () => {
  const t = setup({ voices: [] });
  await t.think();
  await t.finish(`${A} ${BROKEN}`);
  await t.answer(0);
  await t.answer(1);
  await t.end();
  expect(t.played).toEqual([A]);
  expect(t.now().state).toEqual({ ...HANDS_FREE_OFF, notice: "speech_failed" });
  expect(t.reports()).toEqual([["failed", "audio_failed"]]);
  expect(t.controller.owner()).toBeNull();
});

test("a first chunk failing before the speaking render recovers", async () => {
  const t = setup();
  await t.think();
  await t.finish(BROKEN);
  await t.answer(0);
  expect(t.played).toEqual([]);
  expect(t.spoken).toEqual([BROKEN]);
  expect(t.phase()).toBe("speaking");
  expect(t.now().note).toBe("voice_fallback");
  expect(t.reports()).toEqual([["fallback", "audio_failed"]]);
  await t.endSpeech();
  expect(t.phase()).toBe("rearming");
});

test("early speech whose audio ended before its turn ended re-arms", async () => {
  const t = setup({ speakWhileWriting: true });
  await t.think();
  await t.update([T1, turn("t2", "running", `${A}\n\nMore detail fol`)]);
  expect(t.asked()).toEqual([A]);
  await t.answer(0);
  await t.end();
  await t.update([T1, turn("t2", "success", `${A}\n\nMore detail follows.`)]);
  expect(t.phase()).toBe("rearming");
  expect(t.reports()).toEqual([]);
});

test("an engine-down code after playback started sends the next answer to the system voice", async () => {
  const t = setup();
  await t.think();
  await t.finish(`${A} ${B}`);
  await t.answer(0);
  await t.fail(1, "voicebox_unreachable");
  await t.end();
  expect(t.reports()).toEqual([["fallback", "voicebox_unreachable"]]);
  act(() => t.now().interrupt());
  t.send("captureDone");
  t.send("sendAccepted");
  const t2 = turn("t2", "success", `${A} ${B}`);
  await t.update([T1, t2, turn("t3", "running")]);
  await t.update([T1, t2, turn("t3", "success")]);
  expect(t.asked()).toEqual([A, B]);
  expect(t.spoken.at(-1)).toBe(TEXT);
  expect(t.phase()).toBe("speaking");
});

test.each([
  ["with a system voice it falls back", [ZIRA], "fallback", "speaking", "voice_fallback"],
  ["with none the mode ends", [], "failed", "off", null],
] as const)(
  "a Voicebox failure before its first audio is reported: %s",
  async (_name, voices, event, phase, note) => {
    const t = setup({ voices: [...voices] });
    await t.think();
    await t.finish();
    await t.fail(0, "voicebox_unreachable");
    expect(t.phase()).toBe(phase);
    expect(t.now().note).toBe(note);
    expect(t.reports()).toEqual([[event, "voicebox_unreachable"]]);
  },
);

test.each(["before", "after"] as const)(
  "a system voice gone by the time Voicebox fails %s its first audio ends speech_failed",
  async (when) => {
    const t = setup();
    await t.think();
    await t.finish(`${A} ${B}`);
    if (when === "after") await t.answer(0);
    t.voices.length = 0;
    await t.fail(when === "before" ? 0 : 1, "voicebox_unreachable");
    await t.end();
    expect(t.now().state).toEqual({ ...HANDS_FREE_OFF, notice: "speech_failed" });
    expect(t.reports()).toEqual([["failed", "synth_failed"]]);
    expect(t.spoken).toEqual([]);
    expect(t.controller.owner()).toBeNull();
  },
);

test("a system voice that fails as the restart starts it ends speech_failed", async () => {
  const t = setup({ throwOnSpeak: true });
  await t.think();
  await t.finish(`${A} ${B}`);
  await t.answer(0);
  await t.fail(1, "voicebox_timeout");
  await t.end();
  expect(t.now().state).toEqual({ ...HANDS_FREE_OFF, notice: "speech_failed" });
  expect(t.reports()).toEqual([["failed", "synth_failed"]]);
  expect(t.now().note).toBeNull();
  expect(t.controller.owner()).toBeNull();
});

test("early speech's first request still out at the turn's end falls back at the deadline", async () => {
  vi.useFakeTimers();
  const t = setup({ speakWhileWriting: true });
  await t.think();
  await t.update([T1, turn("t2", "running", `${A} The sec`)]);
  expect(t.asked()).toEqual([A]);
  await t.update([T1, turn("t2", "success", `${A} ${B}`)]);
  expect(t.phase()).toBe("thinking");
  expect(t.now().preparing).toBe(true);
  await advance(FIRST_AUDIO_DEADLINE_MS);
  await settled();
  expect(t.requests[0]?.signal.aborted).toBe(true);
  expect(t.asked()).toEqual([A]);
  expect(t.spoken).toEqual([`${A} ${B}`]);
  expect(t.phase()).toBe("speaking");
  expect(t.now().note).toBe("voice_slow");
  expect(t.now().preparing).toBe(false);
  expect(t.reports()).toEqual([["fallback", "voicebox_timeout"]]);
});

test("the deadline counts from the turn's end: after early speech fails, the answer's own request gets only the time left", async () => {
  vi.useFakeTimers();
  const t = setup({ speakWhileWriting: true });
  await t.think();
  await t.update([T1, turn("t2", "running", `${A} The sec`)]);
  await t.update([T1, turn("t2", "success", `${A} ${B}`)]);
  await advance(10_000);
  await t.fail(0, "voicebox_busy");
  expect(t.asked()).toEqual([A, A]);
  expect(t.now().preparing).toBe(true);
  await advance(FIRST_AUDIO_DEADLINE_MS - 10_000);
  await settled();
  expect(t.requests[1]?.signal.aborted).toBe(true);
  expect(t.spoken).toEqual([`${A} ${B}`]);
  expect(t.now().note).toBe("voice_slow");
  expect(t.reports()).toEqual([["fallback", "voicebox_timeout"]]);
});

test("a mic press while early speech's first request is out re-arms", async () => {
  const t = setup({ speakWhileWriting: true });
  await t.think();
  await t.update([T1, turn("t2", "running", `${A} The sec`)]);
  await t.update([T1, turn("t2", "success", `${A} ${B}`)]);
  expect(t.now().preparing).toBe(true);
  act(() => t.now().interrupt());
  await settled();
  expect(t.phase()).toBe("rearming");
  expect(t.requests[0]?.signal.aborted).toBe(true);
  expect(t.now().preparing).toBe(false);
  expect(t.reports()).toEqual([["dropped", "mic"]]);
  expect(t.asked()).toEqual([A]);
  expect(t.controller.owner()).toBeNull();
});

test("no first audio in 15 s reads the system voice", async () => {
  vi.useFakeTimers();
  const t = setup();
  await t.think();
  await t.finish();
  await advance(FIRST_AUDIO_DEADLINE_MS - 1);
  expect(t.requests[0]?.signal.aborted).toBe(false);
  expect(t.spoken).toEqual([]);
  await advance(1);
  expect(t.requests[0]?.signal.aborted).toBe(true);
  expect(t.spoken).toEqual([TEXT]);
  expect(t.phase()).toBe("speaking");
  expect(t.now().note).toBe("voice_slow");
  expect(t.now().preparing).toBe(false);
  expect(t.reports()).toEqual([["fallback", "voicebox_timeout"]]);
});

test("no deadline without a fallback voice", async () => {
  vi.useFakeTimers();
  const t = setup({ voices: [] });
  await t.think();
  await t.finish();
  await advance(FIRST_AUDIO_DEADLINE_MS * 2);
  expect(t.requests[0]?.signal.aborted).toBe(false);
  expect(t.phase()).toBe("thinking");
  expect(t.now().preparing).toBe(true);
  await t.answer(0);
  expect(t.played).toEqual([TEXT]);
  expect(t.phase()).toBe("speaking");
  expect(t.reports()).toEqual([]);
});

test("preparing is true while the voice is fetched", async () => {
  const t = setup();
  await t.think();
  await t.update([T1, turn("t2", "running")]);
  expect(t.now().preparing).toBe(false);
  await t.update([T1, turn("t2", "success")]);
  expect(t.phase()).toBe("thinking");
  expect(t.now().preparing).toBe(true);
  await t.answer(0);
  expect(t.now().preparing).toBe(false);
  expect(t.phase()).toBe("speaking");
});

test.each([
  ["", "nothing", "nothing_read"],
  [JAPANESE, "no_voice", "no_voice_for"],
] as const)("a reply with nothing to read re-arms with a note: %j", async (text, kind, note) => {
  vi.useFakeTimers();
  const t = setup({ voicebox: false });
  await t.think();
  await t.finish(text);
  expect(t.phase()).toBe("rearming");
  expect(t.spoken).toEqual([]);
  expect(t.now().note).toBe(note);
  expect(t.reports()).toEqual([["silent", kind]]);
  await advance(REARM_DELAY_MS);
  expect(t.phase()).toBe("listening");
  expect(t.now().note).toBe(note);
  // The next words heard clear it.
  t.send("captureDone");
  expect(t.now().note).toBeNull();
});

test("each hands-free send warms the pack, engine pack only", async () => {
  const pack = fakePack("ready");
  await setup({ engine: "pack", pack: pack.services }).think();
  // Enter's own warm, then the send's.
  expect(pack.acted).toEqual(["warm", "warm"]);
  const own = fakePack("ready");
  await setup({ engine: "voicebox", pack: own.services }).think();
  const down = fakePack("not_installed");
  await setup({ engine: "pack", pack: down.services }).think();
  expect([own.acted, down.acted]).toEqual([[], []]);
});

test.each(["the strip's interrupt", "the Composer's mic"] as const)(
  "a mic press while preparing re-arms: %s",
  async (way) => {
    const t = setup();
    await t.think();
    await t.update([T1, turn("t2", "running")]);
    // Before the answer's voice is asked for, a press changes nothing.
    t.send("micPressed");
    expect(t.phase()).toBe("thinking");
    await t.update([T1, turn("t2", "success")]);
    expect(t.now().preparing).toBe(true);
    if (way === "the strip's interrupt") act(() => t.now().interrupt());
    else t.send("micPressed");
    await settled();
    expect(t.phase()).toBe("rearming");
    expect(t.requests[0]?.signal.aborted).toBe(true);
    expect(t.now().preparing).toBe(false);
    expect(t.reports()).toEqual([["dropped", "mic"]]);
    await t.answer(0);
    expect(t.played).toEqual([]);
    expect(t.controller.owner()).toBeNull();
  },
);
