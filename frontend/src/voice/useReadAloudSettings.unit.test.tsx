/**
 * `useReadAloudSettings`: the read-aloud settings the window hands every reply and
 * hands-free, read off the effective settings, and a confirmed notice the window remembers itself
 * even when saving it fails.
 */
import { act, cleanup, fireEvent, render, renderHook, screen } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import { afterEach, expect, test, vi } from "vitest";
import {
  ReadAloudContext,
  type ReadAloudSettings,
  useReadAloudReply,
} from "../components/Conversation/ReadAloud";
import { t } from "../i18n/t";
import type { Turn } from "../state/view";
import { createReadAloud, type ReadAloud } from "./readAloud";
import { useReadAloudSettings } from "./useReadAloudSettings";
import { type VoiceboxApi, VoiceboxContext } from "./voiceboxApi";
import { PACK_LANGUAGES } from "./voicePackApi";

afterEach(() => {
  cleanup();
});

const ZIRA = { name: "Zira", voiceURI: "Zira", lang: "en-US", localService: true, default: false };

const TURN: Turn = {
  id: "t1",
  user: null,
  blocks: [
    { kind: "text", id: "b1", text: "The weather is fine today and you are here.", done: true },
  ],
  status: "success",
  usage: null,
  durationMs: 0,
  history: false,
};

function speech() {
  const spoken: string[] = [];
  const synth = {
    paused: false,
    getVoices: () => [ZIRA],
    addEventListener: () => {},
    removeEventListener: () => {},
    speak: (u: { text: string }) => spoken.push(u.text),
    cancel: () => {},
    pause: () => {},
    resume: () => {},
  };
  const controller = createReadAloud({
    synth: synth as unknown as SpeechSynthesis,
    makeUtterance: (text) =>
      ({ text, voice: null, lang: "", rate: 1, onend: null, onerror: null }) as never,
    target: new EventTarget(),
    devices: null,
    setTimeout: () => 0,
    clearTimeout: () => {},
  });
  return { controller, spoken };
}

function Reply(): ReactElement {
  const ui = useReadAloudReply(TURN);
  return (
    <div>
      {ui.controls}
      {ui.note}
    </div>
  );
}

function Window({
  store,
  controller,
}: {
  store: { patch(changes: Record<string, unknown>): Promise<unknown> };
  controller: ReadAloud;
}): ReactElement {
  const settings: ReadAloudSettings = { ...useReadAloudSettings(undefined, store), controller };
  return (
    <ReadAloudContext.Provider value={settings}>
      <Reply />
    </ReadAloudContext.Provider>
  );
}

const button = (key: string) =>
  screen.getByRole("button", { name: t(`readaloud.${key}.label` as never) });

async function click(element: HTMLElement) {
  await act(async () => {
    fireEvent.click(element);
  });
}

test("reads the rate and the notice off the effective settings", () => {
  const store = { patch: vi.fn(async () => undefined) };
  const { result } = renderHook(() =>
    useReadAloudSettings(
      { "voice.read_aloud_rate": 1.5, "voice.read_aloud_notice_seen": true },
      store,
    ),
  );
  expect(result.current.rate).toBe(1.5);
  expect(result.current.noticeSeen).toBe(true);
  expect(renderHook(() => useReadAloudSettings(undefined, store)).result.current).toMatchObject({
    rate: 1,
    voices: {},
    noticeSeen: false,
  });
});

test("a confirmed notice is remembered by the window even when saving it fails", async () => {
  const { controller, spoken } = speech();
  const patch = vi.fn(async () => {
    throw new Error("offline");
  });
  render(<Window store={{ patch }} controller={controller} />);
  await click(button("read"));
  expect(screen.getByRole("group")).toBeTruthy();
  await click(screen.getByRole("button", { name: t("readaloud.notice.confirm") }));
  expect(patch).toHaveBeenCalledWith({ "voice.read_aloud_notice_seen": true });
  expect(spoken).toHaveLength(1);
  await click(button("stop"));
  await click(button("read"));
  expect(screen.queryByRole("group")).toBeNull();
  expect(spoken).toHaveLength(2);
});

test("maps a chosen Voicebox profile to its api, url and speak-while-writing, else null", () => {
  const store = { patch: vi.fn(async () => undefined) };
  const api: VoiceboxApi = {
    status: async () => ({ state: "ready", gpu: null, profiles: [] }),
    speech: async () => new ArrayBuffer(0),
  };
  const wrapper = ({ children }: { children: ReactNode }) => (
    <VoiceboxContext.Provider value={api}>{children}</VoiceboxContext.Provider>
  );

  const chosen = renderHook(
    () =>
      useReadAloudSettings(
        {
          "voice.voicebox_profile": "profile-1",
          "voice.voicebox_url": "http://localhost:9999",
          "voice.speak_while_writing": true,
        },
        store,
      ),
    { wrapper },
  );
  expect(chosen.result.current.voicebox).toMatchObject({
    url: "http://localhost:9999",
    speakWhileWriting: true,
    engine: "voicebox",
  });
  expect(chosen.result.current.voicebox?.api.status).toBe(api.status);

  const defaulted = renderHook(
    () => useReadAloudSettings({ "voice.voicebox_profile": "profile-1" }, store),
    { wrapper },
  );
  expect(defaulted.result.current.voicebox).toMatchObject({
    url: "http://127.0.0.1:17493",
    speakWhileWriting: false,
    engine: "voicebox",
  });

  expect(
    renderHook(() => useReadAloudSettings(undefined, store), { wrapper }).result.current.voicebox,
  ).toBeNull();
  expect(
    renderHook(() => useReadAloudSettings({ "voice.voicebox_profile": "profile-1" }, store)).result
      .current.voicebox,
  ).toBeNull();
});

test('maps engine "pack" to the pack\'s languages and no url, over the same Voicebox api', () => {
  const store = { patch: vi.fn(async () => undefined) };
  const api: VoiceboxApi = {
    status: async () => ({ state: "ready", gpu: null, profiles: [] }),
    speech: async () => new ArrayBuffer(0),
  };
  const wrapper = ({ children }: { children: ReactNode }) => (
    <VoiceboxContext.Provider value={api}>{children}</VoiceboxContext.Provider>
  );

  const { result } = renderHook(
    () => useReadAloudSettings({ "voice.speech_engine": "pack" }, store),
    { wrapper },
  );
  expect(result.current.voicebox).toMatchObject({
    url: "",
    speakWhileWriting: false,
    engine: "pack",
    languages: PACK_LANGUAGES,
  });
  expect(result.current.voicebox?.api.status).toBe(api.status);

  expect(
    renderHook(() => useReadAloudSettings({ "voice.speech_engine": "pack" }, store)).result.current
      .voicebox,
  ).toBeNull();
});

test('drops a language set to "system" from the pack\'s languages', () => {
  const store = { patch: vi.fn(async () => undefined) };
  const api: VoiceboxApi = {
    status: async () => ({ state: "ready", gpu: null, profiles: [] }),
    speech: async () => new ArrayBuffer(0),
  };
  const wrapper = ({ children }: { children: ReactNode }) => (
    <VoiceboxContext.Provider value={api}>{children}</VoiceboxContext.Provider>
  );

  const { result } = renderHook(
    () =>
      useReadAloudSettings(
        { "voice.speech_engine": "pack", "voice.pack_voices": '{"ja":"system"}' },
        store,
      ),
    { wrapper },
  );
  expect(result.current.voicebox?.languages).toEqual(
    PACK_LANGUAGES.filter((lang) => lang !== "ja"),
  );
});

test("noteFallback notes a code once, until a Voicebox call succeeds again", async () => {
  const store = { patch: vi.fn(async () => undefined) };
  let fail = true;
  const api: VoiceboxApi = {
    status: async () => ({ state: "ready", gpu: null, profiles: [] }),
    speech: async () => {
      if (fail) throw new Error("down");
      return new ArrayBuffer(0);
    },
  };
  const wrapper = ({ children }: { children: ReactNode }) => (
    <VoiceboxContext.Provider value={api}>{children}</VoiceboxContext.Provider>
  );

  const { result } = renderHook(
    () => useReadAloudSettings({ "voice.voicebox_profile": "profile-1" }, store),
    { wrapper },
  );
  const speech = result.current.voicebox?.api.speech;
  expect(result.current.noteFallback?.("voicebox_unreachable")).toBe(true);
  expect(result.current.noteFallback?.("voicebox_unreachable")).toBe(false);

  await expect(speech?.("hi", "en", new AbortController().signal)).rejects.toThrow("down");
  expect(result.current.noteFallback?.("voicebox_unreachable")).toBe(false);

  fail = false;
  await speech?.("hi", "en", new AbortController().signal);
  expect(result.current.noteFallback?.("voicebox_unreachable")).toBe(true);
});

test("keeps the latest openVoiceSettings callback stable across renders", () => {
  const store = { patch: vi.fn(async () => undefined) };
  const first = vi.fn();
  const second = vi.fn();
  const { result, rerender } = renderHook(
    ({ open }: { open: () => void }) => useReadAloudSettings(undefined, store, open),
    { initialProps: { open: first } },
  );
  const stableFn = result.current.openVoiceSettings;

  rerender({ open: second });

  expect(result.current.openVoiceSettings).toBe(stableFn);
  result.current.openVoiceSettings?.();
  expect(first).not.toHaveBeenCalled();
  expect(second).toHaveBeenCalledTimes(1);
});
