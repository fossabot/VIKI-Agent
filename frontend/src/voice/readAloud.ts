/**
 * Read-aloud controller over the browser's `speechSynthesis`, installed voices only.
 *
 * One choke point, `localVoices`, feeds every list, choice and `speak`: a voice whose
 * `localService` is not exactly `true` (Edge's "Online (Natural)" voices, Chrome's Google voices)
 * is never listed, picked or spoken with, so reply text reaches only the operating system's own
 * engine. The controller keeps one playback in memory and nothing else.
 *
 * `playAudio` plays audio already synthesized elsewhere (Voicebox's chunks) through `AudioOut`,
 * one Web Audio context, under the same tickets, owners, statuses and stops as `play`.
 */

export const VOICES_WAIT_MS = 3000;

export type ReadAloudStatus = "idle" | "speaking" | "paused" | "failed";

type Voice = SpeechSynthesisVoice;

export function primaryLanguage(tag: string): string {
  const primary = (tag.toLowerCase().split(/[-_]/)[0] ?? "").trim();
  if (primary === "in") return "id";
  if (primary === "cmn") return "zh";
  return primary;
}

export function localVoices(list: readonly Voice[]): Voice[] {
  return list.filter((voice) => voice.localService === true);
}

export function voicesFor(lang: string, list: readonly Voice[]): Voice[] {
  return localVoices(list).filter((voice) => primaryLanguage(voice.lang) === lang);
}

export function pickVoice(
  lang: string,
  list: readonly Voice[],
  chosen: Readonly<Record<string, string>>,
): Voice | null {
  const local = localVoices(list);
  const wanted = Object.hasOwn(chosen, lang) ? chosen[lang] : undefined;
  const stored = wanted === undefined ? undefined : local.find((v) => v.voiceURI === wanted);
  return stored ?? voicesFor(lang, local)[0] ?? null;
}

export function parseVoiceMap(value: unknown): Record<string, string> {
  if (typeof value !== "string") return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    return {};
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return {};
  const map: Record<string, string> = {};
  for (const [key, uri] of Object.entries(parsed)) {
    if (!/^[a-z]{2,3}$/.test(key)) return {};
    if (typeof uri !== "string" || uri.length < 1 || uri.length > 256) return {};
    map[key] = uri;
  }
  return map;
}

export function serializeVoiceMap(map: Readonly<Record<string, string>>): string | null {
  const keys = Object.keys(map).sort();
  if (keys.length === 0) return null;
  return JSON.stringify(Object.fromEntries(keys.map((key) => [key, map[key]])));
}

/** Where `playAudio` sounds. `start` returns that sound's stop; `onEnded` runs once, at its end or
 * stop. */
export interface AudioOut {
  decode(bytes: ArrayBuffer): Promise<AudioBuffer>;
  start(buffer: AudioBuffer, onEnded: () => void): () => void;
  resume(): Promise<void>;
  suspend(): Promise<void>;
}

/** The chunks after the first: the next chunk's bytes, or null when no more come. One call at a
 * time. */
export interface AudioFeed {
  next(signal: AbortSignal): Promise<ArrayBuffer | null>;
}

export interface ReadAloud {
  readonly supported: boolean;
  voices(): readonly Voice[];
  loadVoices(): Promise<readonly Voice[]>;
  begin(owner: string): number;
  isCurrent(ticket: number): boolean;
  play(
    ticket: number,
    owner: string,
    chunks: readonly string[],
    voice: Voice,
    rate: number,
  ): boolean;
  /** `play` for audio: `first`, then each chunk `feed` answers until null. */
  playAudio(ticket: number, owner: string, first: ArrayBuffer, feed: AudioFeed): boolean;
  pause(): void;
  resume(): void;
  stop(): void;
  stopIf(owner: string): void;
  statusOf(owner: string): ReadAloudStatus;
  /** Whose playback this is, from its `begin` (while it prepares) until it stops; null when idle. */
  owner(): string | null;
  subscribe(listener: () => void): () => void;
}

export interface ReadAloudDeps {
  synth: SpeechSynthesis | null;
  makeUtterance(text: string): SpeechSynthesisUtterance;
  audio?: AudioOut | null;
  target: EventTarget;
  devices: EventTarget | null;
  setTimeout(cb: () => void, ms: number): number;
  clearTimeout(id: number): void;
}

interface SpeechPlayback {
  kind: "speech";
  owner: string;
  generation: number;
  chunks: readonly string[];
  voiceURI: string;
  rate: number;
  index: number;
  status: Exclude<ReadAloudStatus, "idle">;
  // Held until the chunk ends: Chromium drops the end event of an utterance nothing references.
  utterance: SpeechSynthesisUtterance | null;
}

interface AudioPlayback {
  kind: "audio";
  owner: string;
  generation: number;
  status: Exclude<ReadAloudStatus, "idle">;
  // Aborts the feed's request in flight.
  abort: AbortController;
  // The stop of the chunk started last.
  stopSource: () => void;
}

type Playback = SpeechPlayback | AudioPlayback;

/** Hands-free's own playback (`speechOwner`'s `handsfree:<turn>`, and its card line) plays on
 * through a device change: no one in a hands-free conversation would press resume, and a headset
 * switching profile as the mic closes fires one. */
function handsFreeOwned(owner: string): boolean {
  return owner.startsWith("handsfree:");
}

export function createReadAloud(deps: ReadAloudDeps): ReadAloud {
  const { synth, audio = null } = deps;
  const listeners = new Set<() => void>();
  const notify = () => {
    for (const listener of [...listeners]) listener();
  };

  let generation = 0;
  let pending: string | null = null;
  let current: Playback | null = null;

  let listeningToVoices = false;
  let cachedVoices: readonly Voice[] = [];
  let cachedKey = "";

  function voices(): readonly Voice[] {
    if (synth === null) return cachedVoices;
    if (!listeningToVoices) {
      listeningToVoices = true;
      synth.addEventListener("voiceschanged", () => {
        const before = cachedVoices;
        voices();
        if (cachedVoices !== before) notify();
      });
    }
    const local = localVoices(synth.getVoices());
    const key = local.map((voice) => voice.voiceURI).join("\u0000");
    if (key !== cachedKey) {
      cachedKey = key;
      cachedVoices = local;
    }
    return cachedVoices;
  }

  function loadVoices(): Promise<readonly Voice[]> {
    const now = voices();
    if (synth === null || now.length > 0) return Promise.resolve(now);
    return new Promise((resolve) => {
      const done = () => {
        synth.removeEventListener("voiceschanged", onChange);
        deps.clearTimeout(timer);
        resolve(voices());
      };
      const onChange = () => {
        if (voices().length > 0) done();
      };
      const timer = deps.setTimeout(done, VOICES_WAIT_MS);
      synth.addEventListener("voiceschanged", onChange);
    });
  }

  function fail(playback: Playback) {
    // A new generation turns every late event of this playback into a no-op.
    generation += 1;
    playback.status = "failed";
    if (playback.kind === "audio") {
      playback.abort.abort();
      playback.stopSource();
    } else {
      playback.utterance = null;
    }
    notify();
  }

  function speakChunk(playback: SpeechPlayback) {
    if (synth === null) return;
    const chunk = playback.chunks[playback.index];
    const live = localVoices(synth.getVoices()).find((v) => v.voiceURI === playback.voiceURI);
    if (chunk === undefined || live === undefined) {
      fail(playback);
      return;
    }
    const utterance = deps.makeUtterance(chunk);
    utterance.voice = live;
    utterance.lang = live.lang;
    utterance.rate = playback.rate;
    utterance.onend = () => {
      if (playback.generation !== generation) return;
      playback.index += 1;
      if (playback.index < playback.chunks.length) {
        speakChunk(playback);
        return;
      }
      current = null;
      notify();
    };
    utterance.onerror = () => {
      if (playback.generation !== generation) return;
      fail(playback);
      // After fail, so an engine that reports the cancel as an error finds a new generation.
      synth.cancel();
    };
    playback.utterance = utterance;
    try {
      synth.speak(utterance);
    } catch {
      fail(playback);
    }
  }

  let listeningToWindow = false;
  function listenOnce() {
    if (listeningToWindow) return;
    listeningToWindow = true;
    deps.target.addEventListener("pagehide", () => stop());
    deps.devices?.addEventListener("devicechange", () => {
      if (current !== null && !handsFreeOwned(current.owner)) pause();
    });
  }

  function begin(owner: string): number {
    stop();
    generation += 1;
    pending = owner;
    // A listener learns who starts before anything plays (hands-free leaves for a reply's Read).
    notify();
    return generation;
  }

  function play(
    ticket: number,
    owner: string,
    chunks: readonly string[],
    voice: Voice,
    rate: number,
  ): boolean {
    if (ticket !== generation || pending !== owner || chunks.length === 0) return false;
    if (!voices().some((v) => v.voiceURI === voice.voiceURI)) return false;
    pending = null;
    listenOnce();
    const playback: SpeechPlayback = {
      kind: "speech",
      owner,
      generation,
      chunks,
      voiceURI: voice.voiceURI,
      rate,
      index: 0,
      status: "speaking",
      utterance: null,
    };
    current = playback;
    notify();
    speakChunk(playback);
    return true;
  }

  function playAudio(ticket: number, owner: string, first: ArrayBuffer, feed: AudioFeed): boolean {
    if (ticket !== generation || pending !== owner || audio === null) return false;
    pending = null;
    listenOnce();
    const playback: AudioPlayback = {
      kind: "audio",
      owner,
      generation,
      status: "speaking",
      abort: new AbortController(),
      stopSource: () => {},
    };
    current = playback;
    notify();
    void playChunks(playback, audio, first, feed);
    return true;
  }

  // Chunk n+1 is asked for while chunk n decodes and sounds, and only once chunk n's bytes arrived.
  async function playChunks(
    playback: AudioPlayback,
    out: AudioOut,
    first: ArrayBuffer,
    feed: AudioFeed,
  ) {
    const moved = () => playback.generation !== generation;
    try {
      void out.resume();
      let bytes: ArrayBuffer | null = first;
      while (bytes !== null) {
        const ahead = feed.next(playback.abort.signal);
        ahead.catch(() => undefined);
        const buffer = await out.decode(bytes);
        if (moved()) return;
        await new Promise<void>((ended) => {
          playback.stopSource = out.start(buffer, ended);
        });
        if (moved()) return;
        bytes = await ahead;
        if (moved()) return;
      }
    } catch {
      if (!moved()) fail(playback);
      return;
    }
    current = null;
    void out.suspend();
    notify();
  }

  function pause() {
    if (current?.status !== "speaking") return;
    if (current.kind === "audio") void audio?.suspend();
    else synth?.pause();
    current.status = "paused";
    notify();
  }

  function resume() {
    if (current?.status !== "paused") return;
    if (current.kind === "audio") void audio?.resume();
    else synth?.resume();
    current.status = "speaking";
    notify();
  }

  function stop() {
    generation += 1;
    const had = current !== null || pending !== null;
    if (current?.status === "speaking" || current?.status === "paused") {
      if (current.kind === "audio") {
        current.abort.abort();
        current.stopSource();
      } else if (synth !== null) {
        synth.cancel();
        // A pause survives cancel in some engines, and the next Read would then stay silent.
        if (synth.paused) synth.resume();
      }
    }
    current = null;
    pending = null;
    if (had) notify();
  }

  function stopIf(owner: string) {
    if (current?.owner === owner || pending === owner) stop();
  }

  return {
    supported: synth !== null,
    voices,
    loadVoices,
    begin,
    isCurrent: (ticket) => ticket === generation,
    play,
    playAudio,
    pause,
    resume,
    stop,
    stopIf,
    statusOf: (owner) => (current?.owner === owner ? current.status : "idle"),
    owner: () => current?.owner ?? pending,
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}

/** `AudioOut` over one Web Audio context, made on first use. */
export function webAudio(create = () => new AudioContext()): AudioOut {
  let context: AudioContext | null = null;
  const ready = () => {
    context ??= create();
    return context;
  };
  return {
    decode: (bytes) => ready().decodeAudioData(bytes),
    start(buffer, onEnded) {
      const output = ready();
      const source = output.createBufferSource();
      source.buffer = buffer;
      source.connect(output.destination);
      source.onended = () => {
        source.disconnect();
        onEnded();
      };
      source.start();
      return () => {
        try {
          source.stop();
        } catch {
          // The controller's stop must not throw, whatever state the source is in.
        }
      };
    },
    resume: () => ready().resume(),
    suspend: () => context?.suspend() ?? Promise.resolve(),
  };
}

export const readAloud = createReadAloud({
  synth: typeof speechSynthesis === "undefined" ? null : speechSynthesis,
  makeUtterance: (text) => new SpeechSynthesisUtterance(text),
  audio: typeof AudioContext === "undefined" ? null : webAudio(),
  target: window,
  devices: navigator.mediaDevices ?? null,
  setTimeout: (cb, ms) => window.setTimeout(cb, ms),
  clearTimeout: (id) => window.clearTimeout(id),
});

export function stopReadAloud(): void {
  readAloud.stop();
}
