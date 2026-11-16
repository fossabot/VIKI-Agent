/**
 * `useReadAloudReply`'s Voicebox path: a Japanese reply goes to Voicebox and plays; a
 * preparing note and Stop show while Voicebox answers, and Stop aborts the request; an unreachable
 * Voicebox falls back to the system voice when one is installed for the reply, else notes the
 * problem only; a later chunk failing mid-reading notes the Voicebox problem instead of the
 * generic failure; the first-use notice names Voicebox when one is chosen.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, expect, test, vi } from "vitest";
import { t } from "../../i18n/t";
import type { Turn } from "../../state/view";
import { type AudioOut, createReadAloud, type ReadAloud } from "../../voice/readAloud";
import {
  type VoiceboxApi,
  VoiceboxError,
  type VoiceboxSettings,
  voiceboxProblem,
} from "../../voice/voiceboxApi";
import {
  PACK_LANGUAGES,
  type VoicePackApi,
  VoicePackContext,
  type VoicePackStatus,
} from "../../voice/voicePackApi";
import { ReadAloudContext, type ReadAloudSettings, useReadAloudReply } from "./ReadAloud";

function voice(name: string, lang: string): SpeechSynthesisVoice {
  return { name, voiceURI: name, lang, localService: true, default: false } as SpeechSynthesisVoice;
}

const ZIRA = voice("Zira", "en-US");

/** Decodes and starts at once, so a chunk plays and ends within one microtask flush. */
function fakeAudioOut(): AudioOut & { started: number[] } {
  const started: number[] = [];
  return {
    started,
    decode: async (bytes) => ({ length: bytes.byteLength }) as unknown as AudioBuffer,
    start: (buffer, onEnded) => {
      started.push((buffer as unknown as { length: number }).length);
      onEnded();
      return () => {};
    },
    resume: async () => {},
    suspend: async () => {},
  };
}

function makeController(voices: SpeechSynthesisVoice[], audio: AudioOut) {
  const spoken: { text: string; voice: string | null }[] = [];
  const synth = {
    paused: false,
    getVoices: () => voices,
    addEventListener: () => {},
    removeEventListener: () => {},
    speak: (u: { text: string; voice: { voiceURI: string } | null }) => {
      spoken.push({ text: u.text, voice: u.voice?.voiceURI ?? null });
    },
    cancel: () => {},
    pause: () => {},
    resume: () => {},
  };
  const controller: ReadAloud = createReadAloud({
    synth: synth as unknown as SpeechSynthesis,
    makeUtterance: (text) =>
      ({ text, voice: null, lang: "", rate: 1, onend: null, onerror: null }) as never,
    audio,
    target: new EventTarget(),
    devices: null,
    setTimeout: () => 0,
    clearTimeout: () => {},
  });
  return { controller, spoken };
}

function turn(text: string): Turn {
  return {
    id: "t1",
    user: null,
    blocks: [{ kind: "text", id: "b1", text, done: true }],
    status: "success",
    usage: null,
    durationMs: 0,
    history: false,
  };
}

function Harness({ turn: shown }: { turn: Turn }): ReactElement {
  const ui = useReadAloudReply(shown);
  return (
    <div>
      {ui.controls}
      {ui.note}
    </div>
  );
}

function settings(
  controller: ReadAloud,
  voicebox: VoiceboxSettings | null,
  over: Partial<ReadAloudSettings> = {},
): ReadAloudSettings {
  return {
    controller,
    rate: 1,
    voices: {},
    noticeSeen: true,
    voicebox,
    acknowledge: () => {},
    ...over,
  };
}

function view(shown: Turn, value: ReadAloudSettings): ReactElement {
  return (
    <ReadAloudContext.Provider value={value}>
      <Harness turn={shown} />
    </ReadAloudContext.Provider>
  );
}

const button = (key: string) =>
  screen.getByRole("button", { name: t(`readaloud.${key}.label` as never) });
const queryButton = (key: string) =>
  screen.queryByRole("button", { name: t(`readaloud.${key}.label` as never) });

async function click(element: HTMLElement): Promise<void> {
  await act(async () => {
    fireEvent.click(element);
  });
}

/** Settles the microtask chain a Voicebox playback builds, with no timer and no sleep. */
async function flush(): Promise<void> {
  await act(async () => {
    for (let turnCount = 0; turnCount < 20; turnCount += 1) await Promise.resolve();
  });
}

function readyStatus(): Promise<{ state: "ready"; gpu: null; profiles: never[] }> {
  return Promise.resolve({ state: "ready", gpu: null, profiles: [] });
}

function packStatus(
  state: VoicePackStatus["state"],
  reason: VoicePackStatus["reason"] = null,
): VoicePackStatus {
  return {
    state,
    reason,
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
}

function fakePackApi(
  status: VoicePackStatus,
  openSpeechSettings?: () => Promise<void>,
): VoicePackApi {
  return {
    status: async () => status,
    act: async () => status,
    openSpeechSettings: openSpeechSettings ?? (async () => {}),
  };
}

afterEach(() => cleanup());

test("reads a Japanese reply through Voicebox", async () => {
  const audio = fakeAudioOut();
  const { controller } = makeController([ZIRA], audio);
  const calls: { text: string; language: string }[] = [];
  const api: VoiceboxApi = {
    status: readyStatus,
    speech: async (text, language) => {
      calls.push({ text, language });
      return new ArrayBuffer(4);
    },
  };
  const voicebox: VoiceboxSettings = {
    api,
    url: "http://127.0.0.1:17493",
    speakWhileWriting: false,
  };
  render(
    view(turn("今日は天気がとても良いので、散歩に行きます。"), settings(controller, voicebox)),
  );

  await click(button("read"));
  await flush();

  expect(calls.length).toBeGreaterThan(0);
  expect(calls[0]?.language).toBe("ja");
  expect(calls[0]?.text).toContain("散歩");
  expect(audio.started.length).toBeGreaterThan(0);
  expect(screen.queryByRole("status")).toBeNull();
});

test("shows a preparing note and Stop while Voicebox answers, and Stop aborts it", async () => {
  const audio = fakeAudioOut();
  const { controller } = makeController([ZIRA], audio);
  let sentSignal: AbortSignal | null = null;
  const api: VoiceboxApi = {
    status: readyStatus,
    speech: (_text, _language, signal) =>
      new Promise<ArrayBuffer>((_resolve, reject) => {
        sentSignal = signal;
        signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
      }),
  };
  const voicebox: VoiceboxSettings = {
    api,
    url: "http://127.0.0.1:17493",
    speakWhileWriting: false,
  };
  render(
    view(turn("今日は天気がとても良いので、散歩に行きます。"), settings(controller, voicebox)),
  );

  await click(button("read"));
  await flush();

  expect(screen.getByRole("status").textContent).toBe("Preparing speech in Voicebox…");
  expect(queryButton("read")).toBeNull();
  expect(button("stop")).toBeTruthy();

  await click(button("stop"));
  await flush();

  expect(sentSignal).not.toBeNull();
  expect((sentSignal as unknown as AbortSignal).aborted).toBe(true);
  expect(audio.started).toHaveLength(0);
  expect(screen.queryByRole("status")).toBeNull();
  expect(button("read")).toBeTruthy();
});

test("falls back to the system voice when Voicebox is unreachable, if one is installed", async () => {
  const audio = fakeAudioOut();
  const { controller, spoken } = makeController([ZIRA], audio);
  const api: VoiceboxApi = {
    status: readyStatus,
    speech: async () => {
      throw new VoiceboxError("voicebox_unreachable");
    },
  };
  const voicebox: VoiceboxSettings = {
    api,
    url: "http://127.0.0.1:17493",
    speakWhileWriting: false,
  };
  render(view(turn("The weather is fine today and you are here."), settings(controller, voicebox)));

  await click(button("read"));
  await flush();

  expect(spoken).toHaveLength(1);
  expect(spoken[0]?.voice).toBe("Zira");
  expect(screen.getByRole("status").textContent).toContain(
    `${voiceboxProblem("voicebox_unreachable", voicebox.url)} The system voice read this reply instead.`,
  );
});

test("notes the problem only when Voicebox is unreachable and no system voice covers the reply", async () => {
  const audio = fakeAudioOut();
  const { controller, spoken } = makeController([ZIRA], audio);
  const api: VoiceboxApi = {
    status: readyStatus,
    speech: async () => {
      throw new VoiceboxError("voicebox_unreachable");
    },
  };
  const voicebox: VoiceboxSettings = {
    api,
    url: "http://127.0.0.1:17493",
    speakWhileWriting: false,
  };
  render(
    view(turn("今日は天気がとても良いので、散歩に行きます。"), settings(controller, voicebox)),
  );

  await click(button("read"));
  await flush();

  expect(spoken).toHaveLength(0);
  expect(audio.started).toHaveLength(0);
  const text = screen.getByRole("status").textContent ?? "";
  expect(text).toContain(voiceboxProblem("voicebox_unreachable", voicebox.url));
  expect(text).not.toContain("system voice read this reply instead");
  expect(button("read")).toBeTruthy();
});

test("notes the Voicebox problem, not the generic failure, when a later chunk fails mid-reading", async () => {
  const audio = fakeAudioOut();
  const { controller } = makeController([ZIRA], audio);
  const calls: string[] = [];
  const api: VoiceboxApi = {
    status: readyStatus,
    speech: async (text) => {
      calls.push(text);
      if (calls.length === 1) return new ArrayBuffer(4);
      throw new VoiceboxError("voicebox_busy");
    },
  };
  const voicebox: VoiceboxSettings = {
    api,
    url: "http://127.0.0.1:17493",
    speakWhileWriting: false,
  };
  const text = "The weather is fine here today in the valley. Tomorrow it may rain all day long.";
  render(view(turn(text), settings(controller, voicebox)));

  await click(button("read"));
  await flush();

  expect(calls.length).toBeGreaterThanOrEqual(2);
  const note = screen.getByRole("status").textContent ?? "";
  expect(note).toContain(voiceboxProblem("voicebox_busy", voicebox.url));
  expect(note).not.toContain(t("readaloud.failed"));
});

test("the first-use notice names Voicebox when a Voicebox voice is chosen", async () => {
  const audio = fakeAudioOut();
  const { controller } = makeController([ZIRA], audio);
  const api: VoiceboxApi = { status: readyStatus, speech: async () => new ArrayBuffer(4) };
  const voicebox: VoiceboxSettings = {
    api,
    url: "http://127.0.0.1:17493",
    speakWhileWriting: false,
  };
  render(
    view(
      turn("The weather is fine today and you are here."),
      settings(controller, voicebox, { noticeSeen: false }),
    ),
  );

  await click(button("read"));

  expect(screen.getByRole("group").textContent).toContain(
    "With a Voicebox voice chosen, replies in the languages Voicebox speaks go to the Voicebox app on this computer instead.",
  );
});

test("the first-use notice names VIKI's natural voices, not the Voicebox app, for the pack", async () => {
  const audio = fakeAudioOut();
  const { controller } = makeController([ZIRA], audio);
  const api: VoiceboxApi = { status: readyStatus, speech: async () => new ArrayBuffer(4) };
  const voicebox: VoiceboxSettings = {
    api,
    url: "http://127.0.0.1:17493",
    speakWhileWriting: false,
    engine: "pack",
    languages: PACK_LANGUAGES,
  };
  render(
    view(
      turn("The weather is fine today and you are here."),
      settings(controller, voicebox, { noticeSeen: false }),
    ),
  );

  await click(button("read"));

  const text = screen.getByRole("group").textContent;
  expect(text).toContain(
    "With natural voices chosen, replies in the languages they speak go to VIKI's natural-voice server on this computer instead.",
  );
  expect(text).not.toContain("Voicebox app");
});

test("notes a fallback once per code", async () => {
  const audio = fakeAudioOut();
  const { controller, spoken } = makeController([ZIRA], audio);
  const api: VoiceboxApi = {
    status: readyStatus,
    speech: async () => {
      throw new VoiceboxError("voicebox_unreachable");
    },
  };
  const voicebox: VoiceboxSettings = {
    api,
    url: "http://127.0.0.1:17493",
    speakWhileWriting: false,
  };
  const seen = new Set<string>();
  const noteFallback = (code: string) => {
    if (seen.has(code)) return false;
    seen.add(code);
    return true;
  };
  render(
    view(
      turn("The weather is fine today and you are here."),
      settings(controller, voicebox, { noteFallback }),
    ),
  );

  await click(button("read"));
  await flush();
  expect(screen.getByRole("status").textContent).toContain(
    "The system voice read this reply instead.",
  );
  expect(spoken).toHaveLength(1);

  await click(button("stop"));
  await click(button("read"));
  await flush();

  expect(screen.queryByRole("status")).toBeNull();
  expect(spoken).toHaveLength(2);
});

test("the fallback note offers Voice settings…", async () => {
  const audio = fakeAudioOut();
  const { controller } = makeController([ZIRA], audio);
  const openVoiceSettings = vi.fn();
  const api: VoiceboxApi = {
    status: readyStatus,
    speech: async () => {
      throw new VoiceboxError("voicebox_unreachable");
    },
  };
  const voicebox: VoiceboxSettings = {
    api,
    url: "http://127.0.0.1:17493",
    speakWhileWriting: false,
  };
  render(
    view(
      turn("The weather is fine today and you are here."),
      settings(controller, voicebox, { openVoiceSettings }),
    ),
  );

  await click(button("read"));
  await flush();

  await click(screen.getByRole("button", { name: "Voice settings…" }));

  expect(openVoiceSettings).toHaveBeenCalledTimes(1);
});

test("the no-voice note offers Install natural voices", async () => {
  const audio = fakeAudioOut();
  const { controller } = makeController([ZIRA], audio);
  const openVoiceSettings = vi.fn();
  const packApi = fakePackApi(packStatus("not_installed"));
  render(
    <VoicePackContext.Provider value={{ api: packApi, openUrl: () => {} }}>
      {view(
        turn("今日は天気がとても良いので、散歩に行きます。"),
        settings(controller, null, { openVoiceSettings }),
      )}
    </VoicePackContext.Provider>,
  );

  await click(button("read"));
  await flush();

  await click(screen.getByRole("button", { name: "Install natural voices…" }));

  expect(openVoiceSettings).toHaveBeenCalledTimes(1);
});

test("the no-voice note offers to open Windows voice settings, but not an install already done", async () => {
  const audio = fakeAudioOut();
  const { controller } = makeController([ZIRA], audio);
  const openSpeechSettings = vi.fn(async () => {});
  const packApi = fakePackApi(packStatus("ready"), openSpeechSettings);
  render(
    <VoicePackContext.Provider value={{ api: packApi, openUrl: () => {} }}>
      {view(turn("今日は天気がとても良いので、散歩に行きます。"), settings(controller, null))}
    </VoicePackContext.Provider>,
  );

  await click(button("read"));
  await flush();

  expect(screen.queryByRole("button", { name: "Install natural voices…" })).toBeNull();
  await click(screen.getByRole("button", { name: "Open Windows voice settings" }));

  expect(openSpeechSettings).toHaveBeenCalledTimes(1);
});

test("reads a cold-pack note while a pack-engine Read warms up", async () => {
  const audio = fakeAudioOut();
  const { controller } = makeController([ZIRA], audio);
  const api: VoiceboxApi = {
    status: readyStatus,
    speech: () => new Promise<ArrayBuffer>(() => {}),
  };
  const voicebox: VoiceboxSettings = {
    api,
    url: "",
    speakWhileWriting: false,
    engine: "pack",
    languages: PACK_LANGUAGES,
  };
  const packApi = fakePackApi(packStatus("stopped"));
  render(
    <VoicePackContext.Provider value={{ api: packApi, openUrl: () => {} }}>
      {view(turn("今日は天気がとても良いので、散歩に行きます。"), settings(controller, voicebox))}
    </VoicePackContext.Provider>,
  );

  await click(button("read"));
  await flush();

  expect(screen.getByRole("status").textContent).toBe(
    "Starting the natural voice… The first reading takes longer.",
  );
});
