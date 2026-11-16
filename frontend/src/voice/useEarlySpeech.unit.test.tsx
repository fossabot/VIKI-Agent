/**
 * `useEarlySpeech`, hands-free's speech while VIKI still writes, driven against the real
 * read-aloud controller over a fake `AudioOut` and a fake Voicebox api whose answers each test
 * hands out by hand. Each answer's bytes carry its sentence, so `played` and `cut` name what
 * sounded and what a stop cut short.
 */
import { act, renderHook } from "@testing-library/react";
import { expect, test } from "vitest";
import type { Block, Turn } from "../state/view";
import { type AudioOut, createReadAloud } from "./readAloud";
import { type EarlySpeechFinish, useEarlySpeech } from "./useEarlySpeech";
import { type VoiceboxApi, VoiceboxError } from "./voiceboxApi";

const A = "The first sentence is ready now.";
const B = "The second one follows right after.";
const C = "The third closes the spoken answer.";
const D = "A newer stretch speaks after the tools.";
const THAI = "วันนี้อากาศดีมากและท้องฟ้าก็แจ่มใส";
const OWNER = "handsfree:t2";

type Options = Parameters<typeof useEarlySpeech>[0];

const settled = () =>
  act(async () => {
    for (let i = 0; i < 20; i++) await Promise.resolve();
  });

let blockId = 0;
const textBlock = (value: string): Block => ({
  kind: "text",
  id: `b${blockId++}`,
  text: value,
  done: false,
});
const TOOLS: Block = { kind: "tools", id: "tools", rows: [] };

function turn(id: string, status: Turn["status"], ...blocks: Block[]): Turn {
  return { id, user: null, blocks, status, usage: null, durationMs: 0, history: false };
}

interface SpeechRequest {
  text: string;
  language: string;
  signal: AbortSignal;
  answer(): void;
  fail(error: unknown): void;
}

function setup(speakWhileWriting = true, languages?: readonly string[]) {
  const played: string[] = [];
  const cut: string[] = [];
  const ends: (() => void)[] = [];
  const sentenceOf = new Map<ArrayBuffer, string>();
  const out: AudioOut = {
    decode: async (bytes) => ({ sentence: sentenceOf.get(bytes) }) as unknown as AudioBuffer,
    start(buffer, onEnded) {
      const { sentence } = buffer as unknown as { sentence: string };
      played.push(sentence);
      let done = false;
      const end = () => {
        if (done) return;
        done = true;
        onEnded();
      };
      ends.push(end);
      return () => {
        if (done) return;
        cut.push(sentence);
        end();
      };
    },
    resume: async () => {},
    suspend: async () => {},
  };
  const controller = createReadAloud({
    synth: {
      paused: false,
      cancel: () => {},
      pause: () => {},
      resume: () => {},
    } as unknown as SpeechSynthesis,
    makeUtterance: () => {
      throw new Error("no system voice in these tests");
    },
    audio: out,
    target: new EventTarget(),
    devices: null,
    setTimeout: () => 0,
    clearTimeout: () => {},
  });
  const requests: SpeechRequest[] = [];
  // Like the real client, a request rejects with an AbortError once its signal aborts.
  const api: VoiceboxApi = {
    status: async () => ({ state: "ready", gpu: null, profiles: [] }),
    speech: (text, language, signal) =>
      new Promise<ArrayBuffer>((resolve, reject) => {
        const bytes = new ArrayBuffer(1);
        sentenceOf.set(bytes, text);
        requests.push({ text, language, signal, answer: () => resolve(bytes), fail: reject });
        signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), {
          once: true,
        });
      }),
  };
  let props: Options = {
    phase: "thinking",
    turn: undefined,
    controller,
    voicebox: {
      api,
      url: "http://voicebox.invalid",
      speakWhileWriting,
      ...(languages !== undefined ? { languages } : {}),
    },
  };
  const hook = renderHook((options: Options) => useEarlySpeech(options), { initialProps: props });
  return {
    controller,
    played,
    cut,
    requests,
    hook,
    asked: () => requests.map((request) => request.text),
    async update(next: Partial<Options>) {
      props = { ...props, ...next };
      hook.rerender(props);
      await settled();
    },
    /** Answers request `index` with its sentence's bytes. */
    async answer(index: number) {
      requests[index]?.answer();
      await settled();
    },
    /** Ends the sentence now sounding. */
    async end() {
      ends.at(-1)?.();
      await settled();
    },
    async finish(turnId: string, final: Turn) {
      let result: EarlySpeechFinish | undefined;
      await act(async () => {
        result = await hook.result.current.finish(turnId, final);
      });
      return result as EarlySpeechFinish;
    },
  };
}

test("requests the first sentence while the turn runs", async () => {
  const t = setup();
  await t.update({ turn: turn("t2", "running", textBlock(`${A} The sec`)) });
  expect(t.requests.map(({ text, language }) => [text, language])).toEqual([[A, "en"]]);
  await t.answer(0);
  expect(t.played).toEqual([A]);
  expect(t.controller.statusOf(OWNER)).toBe("speaking");
});

test("the rest follow in order; finish hands over a last sentence never seen running and owns the speech", async () => {
  const t = setup();
  await t.update({ turn: turn("t2", "running", textBlock(`${A} ${B} The th`)) });
  expect(t.asked()).toEqual([A]);
  await t.answer(0);
  expect(t.asked()).toEqual([A, B]);
  const final = turn("t2", "success", textBlock(`${A} ${B} ${C}\n\nDetails follow.`));
  await t.update({ turn: final });
  expect(await t.finish("t2", final)).toEqual({ played: true, failure: null });
  await t.update({ phase: "speaking" });
  await t.answer(1);
  await t.end();
  expect(t.asked()).toEqual([A, B, C]);
  await t.answer(2);
  await t.end();
  await t.end();
  expect(t.played).toEqual([A, B, C]);
  expect(t.cut).toEqual([]);
  expect(t.controller.statusOf(OWNER)).toBe("idle");
});

test.each([
  { when: "the setting is off", on: false, phase: "thinking", status: "running" },
  { when: "the loop is not sending or thinking", on: true, phase: "speaking", status: "running" },
  { when: "the turn has already finished", on: true, phase: "sending", status: "success" },
] as const)(
  "nothing is requested when $when, and finish leaves the reply to the loop",
  async ({ on, phase, status }) => {
    const t = setup(on);
    const shown = turn("t2", status, textBlock(`${A} ${B} The th`));
    await t.update({ phase, turn: shown });
    expect(t.requests).toEqual([]);
    expect(await t.finish("t2", shown)).toEqual({ played: false, failure: null });
  },
);

test("a reply in a language Voicebox does not speak is declined for good: Thai", async () => {
  const t = setup();
  const thai = textBlock(`${THAI}\n\n${THAI}`);
  await t.update({ turn: turn("t2", "running", thai) });
  await t.update({ turn: turn("t2", "running", thai, TOOLS, textBlock(`${A} The s`)) });
  expect(t.requests).toEqual([]);
  expect(await t.finish("t2", turn("t2", "success", thai))).toEqual({
    played: false,
    failure: null,
  });
});

test("a language outside the engine's own narrower set is declined, even one the default set speaks", async () => {
  const t = setup(true, ["ar"]);
  await t.update({ turn: turn("t2", "running", textBlock(`${A} The s`)) });
  expect(t.requests).toEqual([]);
  expect(await t.finish("t2", turn("t2", "success", textBlock(A)))).toEqual({
    played: false,
    failure: null,
  });
});

test("a failed first request releases the controller, requests nothing more and makes finish report its code", async () => {
  const t = setup();
  await t.update({ turn: turn("t2", "running", textBlock(`${A} ${B} The th`)) });
  t.requests[0]?.fail(new VoiceboxError("voicebox_unreachable"));
  await settled();
  expect(t.controller.owner()).toBeNull();
  const later = turn("t2", "running", textBlock(`${A} ${B} ${C}\n\nMore.`));
  await t.update({ turn: later });
  expect(t.asked()).toEqual([A]);
  expect(await t.finish("t2", later)).toEqual({ played: false, failure: "voicebox_unreachable" });
});

test.each(["waiting", "off"] as const)(
  "%s stops the playback, and later text requests nothing",
  async (phase) => {
    const t = setup();
    await t.update({ turn: turn("t2", "running", textBlock(`${A} ${B} The th`)) });
    await t.answer(0);
    expect(t.asked()).toEqual([A, B]);
    await t.update({ phase });
    expect(t.cut).toEqual([A]);
    expect(t.requests[1]?.signal.aborted).toBe(true);
    expect(t.controller.statusOf(OWNER)).toBe("idle");
    const later = turn(
      "t2",
      "running",
      textBlock(`${A} ${B} ${C}\n\nMore.`),
      TOOLS,
      textBlock(`${D} The n`),
    );
    await t.update({ phase: "thinking", turn: later });
    expect(t.asked()).toEqual([A, B]);
    expect(await t.finish("t2", later)).toEqual({ played: false, failure: null });
  },
);

test("a card while the first sentence is still requested aborts it, and its late answer plays nothing", async () => {
  const t = setup();
  await t.update({ turn: turn("t2", "running", textBlock(`${A} The s`)) });
  await t.update({ phase: "waiting" });
  expect(t.requests[0]?.signal.aborted).toBe(true);
  await t.answer(0);
  expect(t.played).toEqual([]);
  expect(t.controller.owner()).toBeNull();
  expect(await t.finish("t2", turn("t2", "success", textBlock(`${A} ${B}`)))).toEqual({
    played: false,
    failure: null,
  });
});

test("a newer stretch drops an older queued sentence", async () => {
  const t = setup();
  const first = textBlock(`${A} ${B}\n\nMore detail.`);
  await t.update({ turn: turn("t2", "running", first) });
  expect(t.asked()).toEqual([A]);
  await t.update({ turn: turn("t2", "running", first, TOOLS, textBlock(`${D} The n`)) });
  await t.answer(0);
  expect(t.asked()).toEqual([A, D]);
});

test("a new turn id stops the old turn's speech, even before the new one has a sentence", async () => {
  const t = setup();
  await t.update({ turn: turn("t2", "running", textBlock(`${A} The s`)) });
  await t.answer(0);
  await t.update({ turn: turn("t3", "running", textBlock("A newer turn has only")) });
  expect(t.cut).toEqual([A]);
  expect(t.controller.statusOf(OWNER)).toBe("idle");
  await t.update({ turn: turn("t3", "running", textBlock(`${D} The n`)) });
  expect(t.asked()).toEqual([A, D]);
  await t.answer(1);
  expect(t.controller.statusOf("handsfree:t3")).toBe("speaking");
  expect(await t.finish("t2", turn("t2", "success", textBlock(A)))).toEqual({
    played: false,
    failure: null,
  });
});

test("an unmount stops the playback", async () => {
  const t = setup();
  await t.update({ turn: turn("t2", "running", textBlock(`${A} The s`)) });
  await t.answer(0);
  t.hook.unmount();
  expect(t.cut).toEqual([A]);
  expect(t.controller.statusOf(OWNER)).toBe("idle");
});
