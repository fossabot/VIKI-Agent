/**
 * Frozen acceptance tests for `useDictation`'s `endOnFinal` option: in hands-free
 * mode the first non-empty final ends the capture, an empty one does not, and a hidden window
 * cancels the capture instead of finishing it, so half a sentence is never sent.
 */
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type {
  CaptureOptions,
  DictationApi,
  VoiceServerMessage,
  VoiceSocketHandlers,
  VoiceStatus,
} from "./dictationTypes";
import { type DictationClock, useDictation } from "./useDictation";

vi.mock("./readAloud");

beforeEach(() => {
  localStorage.clear();
});
afterEach(() => {
  cleanup();
  localStorage.clear();
});

const READY: VoiceStatus = {
  state: "ready",
  running: false,
  model: "small-q8_0",
  backend: "cpu",
  device: null,
  languages: ["auto", "en", "ja"],
  language: "auto",
  language_unavailable: false,
  models: ["small-q8_0"],
  backends: ["cpu"],
  fallback: null,
  cpu_model: "small-q8_0",
  slow_on_cpu: false,
};

/** Timers that fire only when a test advances them; none of these tests needs to. */
const CLOCK: DictationClock = {
  now: () => 0,
  setTimeout: () => 0,
  clearTimeout: () => undefined,
};

function setup(endOnFinal: boolean) {
  const log: string[] = [];
  const sockets: VoiceSocketHandlers[] = [];
  const api = {
    status: vi.fn(async () => READY),
    clearFallback: vi.fn(async () => READY),
    socket: vi.fn((handlers: VoiceSocketHandlers) => {
      sockets.push(handlers);
      return {
        begin: vi.fn((id: string) => log.push(`begin ${id}`)),
        audio: vi.fn(),
        end: vi.fn((id: string) => log.push(`end ${id}`)),
        cancel: vi.fn((id: string) => log.push(`cancel ${id}`)),
        close: vi.fn(() => log.push("close")),
      };
    }),
    startCapture: vi.fn(async (options: CaptureOptions) => ({
      deviceId: options.deviceId,
      stop: vi.fn(async () => {
        log.push("stop");
      }),
    })),
    microphones: vi.fn(async () => []),
    workletUrl: "/worklet.js",
  } satisfies DictationApi;
  const setText = vi.fn();
  const hook = renderHook(
    ({ text }: { text: string }) =>
      useDictation({
        api,
        text,
        setText,
        caret: () => text.length,
        composing: () => false,
        focusBox: vi.fn(),
        clock: CLOCK,
        endOnFinal,
      }),
    { initialProps: { text: "" } },
  );
  const id = () => log.find((line) => line.startsWith("begin "))?.slice(6) ?? "";
  const send = (m: VoiceServerMessage) => act(() => sockets.at(-1)?.message(m));
  const final = (text: string, segment = 0) =>
    send({ type: "final", capture_id: id(), segment, text });
  return { api, log, setText, hook, id, send, final, view: () => hook.result.current[0] };
}

const settled = () =>
  act(async () => {
    for (let i = 0; i < 10; i++) await Promise.resolve();
  });

async function recording(t: ReturnType<typeof setup>) {
  await settled();
  act(() => t.hook.result.current[1].toggle());
  await settled();
  await t.send({
    type: "capturing",
    capture_id: t.id(),
    model: "small-q8_0",
    backend: "cpu",
    device: null,
    language: "auto",
    language_unavailable: false,
    fallback: null,
  });
  expect(t.view().phase).toBe("recording");
}

const ends = (t: ReturnType<typeof setup>) => t.log.filter((line) => line.startsWith("end "));

test("a non-empty final lands in the draft and ends the capture", async () => {
  const t = setup(true);
  await recording(t);
  await t.final("buy milk");
  expect(t.setText).toHaveBeenCalledWith("buy milk", 8, 8);
  expect(t.view().phase).toBe("finalizing");
  await settled();
  expect(ends(t)).toEqual([`end ${t.id()}`]);
});

test("an empty or blank final leaves the capture recording", async () => {
  const t = setup(true);
  await recording(t);
  await t.final("", 0);
  await t.final("   ", 1);
  await settled();
  expect(t.view().phase).toBe("recording");
  expect(ends(t)).toEqual([]);
  expect(t.setText).not.toHaveBeenCalled();
  await t.final("hello", 2);
  expect(t.view().phase).toBe("finalizing");
});

test("without endOnFinal a final keeps the capture recording", async () => {
  const t = setup(false);
  await recording(t);
  await t.final("buy milk");
  await settled();
  expect(t.view().phase).toBe("recording");
  expect(ends(t)).toEqual([]);
});

test("a hidden window cancels the capture instead of finishing it", async () => {
  const t = setup(true);
  await recording(t);
  await t.send({ type: "partial", capture_id: t.id(), segment: 0, revision: 1, text: "half a" });
  const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
  act(() => {
    document.dispatchEvent(new Event("visibilitychange"));
  });
  visibility.mockRestore();
  expect(t.view().phase).toBe("cancelling");
  await settled();
  expect(t.log).toContain(`cancel ${t.id()}`);
  expect(ends(t)).toEqual([]);
  // The half sentence the server still finalizes does not reach the draft.
  await t.final("half a sentence");
  expect(t.setText).not.toHaveBeenCalled();
});

test("without endOnFinal a hidden window still finishes the capture", async () => {
  const t = setup(false);
  await recording(t);
  const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
  act(() => {
    document.dispatchEvent(new Event("visibilitychange"));
  });
  visibility.mockRestore();
  expect(t.view().phase).toBe("finalizing");
});
