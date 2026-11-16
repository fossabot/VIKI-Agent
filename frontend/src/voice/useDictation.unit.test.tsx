import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { readCss } from "../testing/css";
import type {
  CaptureOptions,
  DictationApi,
  VoiceServerMessage,
  VoiceSocketHandlers,
  VoiceStatus,
} from "./dictationTypes";
import { dictationNotice } from "./dictationWords";
import { stopReadAloud } from "./readAloud";
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
const SETUP: VoiceStatus = { ...READY, state: "setup_needed", model: null, models: [] };

function fakeClock(): DictationClock & { advance(ms: number): void } {
  let now = 0;
  let next = 1;
  const timers = new Map<number, { at: number; fn: () => void }>();
  return {
    now: () => now,
    setTimeout(fn, ms) {
      timers.set(next, { at: now + ms, fn });
      return next++;
    },
    clearTimeout(id) {
      timers.delete(id);
    },
    advance(ms) {
      const end = now + ms;
      for (;;) {
        let due: [number, { at: number; fn: () => void }] | undefined;
        for (const entry of timers)
          if (entry[1].at <= end && (!due || entry[1].at < due[1].at)) due = entry;
        if (!due) break;
        timers.delete(due[0]);
        now = due[1].at;
        due[1].fn();
      }
      now = end;
    },
  };
}

function setup({ status = READY }: { status?: VoiceStatus } = {}) {
  const log: string[] = [];
  const sockets: VoiceSocketHandlers[] = [];
  const captures: CaptureOptions[] = [];
  const statuses = [status];
  const api = {
    status: vi.fn(async () => statuses.shift() ?? READY),
    clearFallback: vi.fn(async () => READY),
    socket: vi.fn((handlers: VoiceSocketHandlers) => {
      sockets.push(handlers);
      return {
        begin: vi.fn((id: string) => log.push(`begin ${id}`)),
        audio: vi.fn((chunk: ArrayBuffer) => log.push(`audio ${chunk.byteLength}`)),
        end: vi.fn((id: string) => log.push(`end ${id}`)),
        cancel: vi.fn((id: string) => log.push(`cancel ${id}`)),
        close: vi.fn(() => log.push("close")),
      };
    }),
    startCapture: vi.fn(async (options: CaptureOptions) => {
      captures.push(options);
      return {
        deviceId: options.deviceId,
        stop: vi.fn(async () => {
          log.push("flush");
          options.onAudio(new ArrayBuffer(1_600));
          log.push("stop");
        }),
      };
    }),
    microphones: vi.fn(async () => []),
    workletUrl: "/worklet.js",
  } satisfies DictationApi;
  const clock = fakeClock();
  const setText = vi.fn();
  const focusBox = vi.fn();
  const io = { caret: 0, composing: false };
  const hook = renderHook(
    ({ text }: { text: string }) =>
      useDictation({
        api,
        text,
        setText,
        caret: () => io.caret,
        composing: () => io.composing,
        focusBox,
        clock,
      }),
    { initialProps: { text: "" } },
  );
  const view = () => hook.result.current[0];
  const controls = () => hook.result.current[1];
  const socket = () => api.socket.mock.results.at(-1)?.value as ReturnType<typeof api.socket>;
  const id = () => socket().begin.mock.calls[0]?.[0] ?? "";
  const send = (m: VoiceServerMessage) => act(() => sockets.at(-1)?.message(m));
  const capturing = () =>
    send({
      type: "capturing",
      capture_id: id(),
      model: "small-q8_0",
      backend: "cpu",
      device: null,
      language: "auto",
      language_unavailable: false,
      fallback: null,
    });
  const audio = (bytes: number) => act(() => captures.at(-1)?.onAudio(new ArrayBuffer(bytes)));
  const advance = (ms: number) => act(() => clock.advance(ms));
  return {
    api,
    log,
    sockets,
    captures,
    statuses,
    setText,
    focusBox,
    io,
    hook,
    view,
    controls,
    socket,
    id,
    send,
    capturing,
    audio,
    advance,
  };
}

const settled = () =>
  act(async () => {
    for (let i = 0; i < 10; i++) await Promise.resolve();
  });

test.each(["ready", "failed"] as const)(
  "a cancelled %s readiness retry cannot start capture or change a newer explicit retry",
  async (outcome) => {
    const t = setup({ status: SETUP });
    await settled();
    let finishOld: (status: VoiceStatus) => void = () => {};
    let failOld: (error: Error) => void = () => {};
    let finishNew: (status: VoiceStatus) => void = () => {};
    t.api.status
      .mockImplementationOnce(
        () =>
          new Promise<VoiceStatus>((resolve, reject) => {
            finishOld = resolve;
            failOld = reject;
          }),
      )
      .mockImplementationOnce(
        () =>
          new Promise<VoiceStatus>((resolve) => {
            finishNew = resolve;
          }),
      );
    act(() => t.controls().press());
    act(() => t.controls().cancel());
    act(() => t.controls().press());
    expect(t.api.status).toHaveBeenCalledTimes(3);
    act(() => {
      if (outcome === "ready") finishOld(READY);
      else failOld(new Error("old retry failed"));
    });
    await settled();
    expect(t.view().phase).toBe("unavailable");
    expect(t.view().notice).toBeNull();
    expect(t.api.startCapture).not.toHaveBeenCalled();
    act(() => t.controls().press());
    expect(t.api.status).toHaveBeenCalledTimes(3);
    act(() => t.controls().release());
    act(() => finishNew(READY));
    await settled();
    expect(t.api.startCapture).toHaveBeenCalledTimes(1);
    expect(t.view().toggled).toBe(true);
  },
);

async function recording(t: ReturnType<typeof setup>, how: "press" | "toggle" = "toggle") {
  await settled();
  act(() => t.controls()[how]());
  await settled();
  t.capturing();
}

test("press starts, capturing records", async () => {
  const t = setup();
  await settled();
  expect(t.view().phase).toBe("idle");
  act(() => t.controls().press());
  expect(t.view().phase).toBe("starting");
  await settled();
  expect(t.api.startCapture).toHaveBeenCalledWith(
    expect.objectContaining({ deviceId: null, workletUrl: "/worklet.js" }),
  );
  expect(t.log[0]).toMatch(/^begin [0-9a-f-]{36}$/);
  t.capturing();
  expect(t.view().phase).toBe("recording");
});

test("the saved microphone is used", async () => {
  localStorage.setItem("viki.voice.microphone", "mic-2");
  const t = setup();
  await recording(t);
  expect(t.captures[0]?.deviceId).toBe("mic-2");
});

test("a 200 ms hold switches to toggle and the next press finishes", async () => {
  const t = setup();
  await recording(t, "press");
  await t.advance(200);
  act(() => t.controls().release());
  expect(t.view().toggled).toBe(true);
  expect(t.view().phase).toBe("recording");
  act(() => t.controls().press());
  expect(t.view().phase).toBe("finalizing");
  await settled();
  expect(t.log.at(-1)).toBe(`end ${t.id()}`);
});

test("a 400 ms hold finishes on release, the flush before end", async () => {
  const t = setup();
  await recording(t, "press");
  await t.audio(3_200);
  await t.advance(400);
  act(() => t.controls().release());
  expect(t.view().phase).toBe("finalizing");
  await settled();
  expect(t.log.slice(1)).toEqual(["audio 3200", "flush", "audio 1600", "stop", `end ${t.id()}`]);
});

test("a press while recording in hold mode (a lost pointerup) finishes", async () => {
  const t = setup();
  await recording(t, "press");
  await t.advance(1_000);
  act(() => t.controls().press());
  expect(t.view().phase).toBe("finalizing");
  await settled();
  expect(t.log.at(-1)).toBe(`end ${t.id()}`);
});

test("toggle starts toggled and finishes", async () => {
  const t = setup();
  await recording(t, "toggle");
  expect(t.view().toggled).toBe(true);
  act(() => t.controls().release());
  expect(t.view().phase).toBe("recording");
  act(() => t.controls().toggle());
  expect(t.view().phase).toBe("finalizing");
  act(() => t.controls().toggle());
  await settled();
  expect(t.log.filter((line) => line.startsWith("end"))).toHaveLength(1);
});

test("cancel ignores a late final", async () => {
  const t = setup();
  await recording(t);
  await t.send({ type: "partial", capture_id: t.id(), segment: 0, revision: 1, text: "hello" });
  expect(t.view().provisional).toBe("hello");
  act(() => t.controls().cancel());
  expect(t.log.at(-1)).toBe(`cancel ${t.id()}`);
  expect(t.view().phase).toBe("cancelling");
  expect(t.view().provisional).toBe("");
  // The microphone closes on cancel itself, not when the socket later closes.
  await settled();
  expect(t.log).toContain("stop");
  expect(t.log).not.toContain("close");
  act(() => t.controls().press());
  expect(t.api.socket).toHaveBeenCalledTimes(1);
  await t.send({ type: "final", capture_id: t.id(), segment: 0, text: "hello" });
  expect(t.setText).not.toHaveBeenCalled();
  await t.send({ type: "cancelled", capture_id: t.id() });
  expect(t.view().phase).toBe("cancelling");
  act(() => t.sockets[0]?.closed(1000));
  expect(t.view().phase).toBe("idle");
  expect(t.view().notice).toBeNull();
});

test("cancelling ends after its backstop when the socket never closes", async () => {
  const t = setup();
  await recording(t);
  act(() => t.controls().cancel());
  await t.advance(5_000);
  expect(t.view().phase).toBe("idle");
  expect(t.socket().close).toHaveBeenCalled();
});

test("cancel while starting stops the capture once it resolves and drops a later final", async () => {
  const t = setup();
  await settled();
  type Handle = Awaited<ReturnType<typeof t.api.startCapture>>;
  let resolveCapture: (handle: Handle) => void = () => undefined;
  const lateHandle: Handle = { deviceId: null, stop: vi.fn(async (): Promise<void> => undefined) };
  t.api.startCapture.mockImplementationOnce(
    () => new Promise<Handle>((resolve) => (resolveCapture = resolve)),
  );
  act(() => t.controls().press());
  await settled();
  expect(t.view().phase).toBe("starting");
  act(() => t.controls().cancel());
  expect(t.view().phase).toBe("cancelling");
  expect(t.socket().cancel).toHaveBeenCalledWith(t.id());
  expect(lateHandle.stop).not.toHaveBeenCalled();
  resolveCapture(lateHandle);
  await settled();
  expect(lateHandle.stop).toHaveBeenCalledTimes(1);
  expect(t.socket().close).not.toHaveBeenCalled();
  await t.send({ type: "final", capture_id: t.id(), segment: 0, text: "hello" });
  expect(t.setText).not.toHaveBeenCalled();
});

test("finalizing ends after 30 s with connection_lost", async () => {
  const t = setup();
  await recording(t);
  act(() => t.controls().toggle());
  await settled();
  expect(t.socket().end).toHaveBeenCalledWith(t.id());
  await t.advance(29_999);
  expect(t.view().phase).toBe("finalizing");
  expect(t.socket().close).not.toHaveBeenCalled();
  await t.advance(1);
  expect(t.view().phase).toBe("idle");
  expect(t.view().notice).toBe(dictationNotice("connection_lost"));
  expect(t.socket().close).toHaveBeenCalled();
});

test("a partial shows as provisional, a final lands at the caret", async () => {
  const t = setup();
  t.io.caret = 5;
  t.hook.rerender({ text: "Note:" });
  await recording(t);
  await t.send({ type: "partial", capture_id: t.id(), segment: 0, revision: 1, text: "hel" });
  await t.send({ type: "partial", capture_id: t.id(), segment: 0, revision: 2, text: "hello" });
  expect(t.view().provisional).toBe("hello");
  await t.send({ type: "final", capture_id: t.id(), segment: 0, text: "hello" });
  expect(t.setText).toHaveBeenCalledWith("Note: hello", 11, 11);
  expect(t.view().provisional).toBe("");
});

test("a rerender with typed text moves the next insertion", async () => {
  const t = setup();
  t.io.caret = 5;
  t.hook.rerender({ text: "hello" });
  await recording(t);
  t.hook.rerender({ text: "Xhello" });
  await t.send({ type: "final", capture_id: t.id(), segment: 0, text: "world" });
  expect(t.setText).toHaveBeenLastCalledWith("Xhello world", 12, 12);
  t.hook.rerender({ text: "Xhello world" });
  await t.send({ type: "final", capture_id: t.id(), segment: 1, text: "again" });
  expect(t.setText).toHaveBeenLastCalledWith("Xhello world again", 18, 18);
});

test("a final during a composition applies at compositionEnd", async () => {
  const t = setup();
  await recording(t);
  t.io.composing = true;
  await t.send({ type: "final", capture_id: t.id(), segment: 0, text: "hello" });
  await t.send({ type: "done", capture_id: t.id() });
  expect(t.setText).not.toHaveBeenCalled();
  expect(t.focusBox).not.toHaveBeenCalled();
  t.io.composing = false;
  t.hook.rerender({ text: "あ" });
  act(() => t.controls().compositionEnd());
  expect(t.setText).toHaveBeenCalledWith("あhello", 6, 6);
});

test("done goes idle and focuses the box after the inserted text", async () => {
  const t = setup();
  await recording(t);
  await t.send({ type: "final", capture_id: t.id(), segment: 0, text: "hello" });
  await t.send({ type: "done", capture_id: t.id() });
  expect(t.view().phase).toBe("idle");
  expect(t.focusBox).toHaveBeenCalledWith(5);
  expect(t.socket().close).toHaveBeenCalled();
});

test("a hidden window finishes", async () => {
  const t = setup();
  await recording(t);
  const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
  act(() => {
    document.dispatchEvent(new Event("visibilitychange"));
  });
  visibility.mockRestore();
  expect(t.view().phase).toBe("finalizing");
  await settled();
  expect(t.log.at(-1)).toBe(`end ${t.id()}`);
});

test("pagehide cancels", async () => {
  const t = setup();
  await recording(t);
  act(() => {
    window.dispatchEvent(new Event("pagehide"));
  });
  expect(t.view().phase).toBe("cancelling");
  expect(t.log).toContain(`cancel ${t.id()}`);
  await settled();
  expect(t.log).toContain("stop");
  expect(t.log).not.toContain("close");
});

test("unmount cancels the capture", async () => {
  const t = setup();
  await recording(t);
  const handle = await t.api.startCapture.mock.results[0]?.value;
  t.hook.unmount();
  await settled();
  expect(t.socket().cancel).toHaveBeenCalledWith(t.id());
  expect(t.socket().close).toHaveBeenCalled();
  expect(handle.stop).toHaveBeenCalled();
});

test("an ended track finishes with mic_lost", async () => {
  const t = setup();
  await recording(t);
  act(() => t.captures[0]?.onEnded());
  expect(t.view().phase).toBe("finalizing");
  expect(t.view().notice).toBe(dictationNotice("mic_lost"));
});

test("the chosen microphone gone at devicechange finishes with mic_lost", async () => {
  const devices = new EventTarget();
  Object.defineProperty(navigator, "mediaDevices", { value: devices, configurable: true });
  try {
    localStorage.setItem("viki.voice.microphone", "mic-2");
    const t = setup();
    await recording(t);
    act(() => {
      devices.dispatchEvent(new Event("devicechange"));
    });
    await settled();
    expect(t.view().phase).toBe("finalizing");
    expect(t.view().notice).toBe(dictationNotice("mic_lost"));
  } finally {
    Reflect.deleteProperty(navigator, "mediaDevices");
  }
});

test("2 s without audio finishes with mic_stalled", async () => {
  const t = setup();
  await recording(t);
  await t.advance(1_000);
  await t.audio(3_200);
  await t.advance(1_750);
  expect(t.view().phase).toBe("recording");
  expect(t.view().elapsedMs).toBe(2_750);
  await t.advance(250);
  expect(t.view().phase).toBe("finalizing");
  expect(t.view().notice).toBe(dictationNotice("mic_stalled"));
});

test("a close without done is connection_lost; 1013 is busy", async () => {
  const t = setup();
  await recording(t);
  act(() => t.sockets[0]?.closed(1006));
  expect(t.view().phase).toBe("idle");
  expect(t.view().notice).toBe(dictationNotice("connection_lost"));
  const handle = await t.api.startCapture.mock.results[0]?.value;
  await settled();
  expect(handle.stop).toHaveBeenCalled();
  act(() => t.controls().toggle());
  act(() => t.sockets[1]?.closed(1013));
  expect(t.view().notice).toBe(dictationNotice("busy"));
});

test("more than 320,000 bytes before capturing cancels with still_starting", async () => {
  const t = setup();
  await settled();
  act(() => t.controls().toggle());
  await settled();
  for (let i = 0; i < 100; i++) await t.audio(3_200);
  expect(t.view().phase).toBe("starting");
  await t.audio(2);
  expect(t.view().phase).toBe("cancelling");
  expect(t.view().notice).toBe(dictationNotice("still_starting"));
  expect(t.socket().audio).toHaveBeenCalledTimes(100);
});

test("a batch that would pass 3,840,000 bytes is not sent and finishes with capture_cap", async () => {
  const t = setup();
  await recording(t);
  for (let i = 0; i < 1_200; i++) await t.audio(3_200);
  expect(t.view().phase).toBe("recording");
  await t.audio(2);
  expect(t.socket().audio).toHaveBeenCalledTimes(1_200);
  expect(t.view().phase).toBe("finalizing");
  expect(t.view().notice).toBe(dictationNotice("capture_cap"));
  await settled();
  expect(t.socket().audio).toHaveBeenCalledTimes(1_200);
  expect(t.socket().end).toHaveBeenCalledWith(t.id());
});

test("stopping finalizes without sending end", async () => {
  const t = setup();
  await recording(t);
  await t.send({ type: "stopping", capture_id: t.id(), reason: "overloaded" });
  expect(t.view().phase).toBe("finalizing");
  expect(t.view().notice).toBe(dictationNotice("overloaded"));
  await settled();
  expect(t.socket().end).not.toHaveBeenCalled();
  expect(t.log).not.toContain("audio 1600");
});

test("error setup_needed shows its notice and reads the status again", async () => {
  const t = setup();
  await recording(t);
  t.statuses.push(SETUP);
  await t.send({ type: "error", capture_id: t.id(), code: "setup_needed" });
  expect(t.view().notice).toBe(dictationNotice("setup_needed"));
  await settled();
  expect(t.api.status).toHaveBeenCalledTimes(2);
  expect(t.view().phase).toBe("unavailable");
});

test("a press while finalizing is ignored", async () => {
  const t = setup();
  await recording(t);
  act(() => t.controls().toggle());
  act(() => t.controls().press());
  act(() => t.controls().toggle());
  expect(t.view().phase).toBe("finalizing");
  expect(t.api.socket).toHaveBeenCalledTimes(1);
});

test("a press while unavailable reads the status, then shows the setup notice", async () => {
  const t = setup({ status: SETUP });
  await settled();
  expect(t.view().phase).toBe("unavailable");
  t.statuses.push(SETUP);
  act(() => t.controls().press());
  await settled();
  expect(t.api.status).toHaveBeenCalledTimes(2);
  expect(t.view().notice).toBe(dictationNotice("setup_needed"));
  expect(t.api.socket).not.toHaveBeenCalled();
});

test("a press while unavailable starts once the status is ready", async () => {
  const t = setup({ status: SETUP });
  await settled();
  act(() => t.controls().press());
  await settled();
  expect(t.view().phase).toBe("starting");
  expect(t.view().toggled).toBe(false);
});

test("a microphone refusal cancels with mic_denied", async () => {
  const t = setup();
  await settled();
  t.api.startCapture.mockRejectedValueOnce(new DOMException("no", "NotAllowedError"));
  act(() => t.controls().toggle());
  await settled();
  expect(t.view().phase).toBe("cancelling");
  expect(t.view().notice).toBe(dictationNotice("mic_denied"));
  expect(t.socket().cancel).toHaveBeenCalled();
});

test("a final reading Allow once or allow reaches only setText", async () => {
  const t = setup();
  await recording(t);
  await t.send({ type: "final", capture_id: t.id(), segment: 0, text: "Allow once" });
  t.hook.rerender({ text: "Allow once" });
  await t.send({ type: "final", capture_id: t.id(), segment: 1, text: "allow" });
  expect(t.setText.mock.calls).toEqual([
    ["Allow once", 10, 10],
    ["Allow once allow", 16, 16],
  ]);
  expect(t.focusBox).not.toHaveBeenCalled();
  expect(t.socket().end).not.toHaveBeenCalled();
  expect(t.socket().cancel).not.toHaveBeenCalled();
  expect(t.api.clearFallback).not.toHaveBeenCalled();
  expect(t.view().phase).toBe("recording");
});

test("results for another capture are ignored", async () => {
  const t = setup();
  await recording(t);
  const other = "00000000-0000-4000-8000-000000000000";
  await t.send({ type: "final", capture_id: other, segment: 0, text: "x" });
  await t.send({ type: "done", capture_id: other });
  expect(t.setText).not.toHaveBeenCalled();
  expect(t.view().phase).toBe("recording");
});

test("Try GPU again shows the cleared status", async () => {
  const t = setup();
  await recording(t);
  await t.send({
    type: "capturing",
    capture_id: t.id(),
    model: "small-q8_0",
    backend: "cpu",
    device: null,
    language: "auto",
    language_unavailable: false,
    fallback: { backend: "cuda", code: "worker_crashed", detail: null },
  });
  expect(t.view().fallback?.code).toBe("worker_crashed");
  act(() => t.controls().retryGpu());
  await settled();
  expect(t.view().fallback).toBeNull();
});

test("each capture start stops read-aloud once, before the capture", async () => {
  const t = setup();
  await settled();
  vi.mocked(stopReadAloud).mockClear();
  vi.mocked(stopReadAloud).mockImplementation(() => {
    t.log.push("stopReadAloud");
  });
  act(() => t.controls().press());
  expect(stopReadAloud).toHaveBeenCalledTimes(1);
  expect(t.log).toEqual([expect.stringMatching(/^begin /), "stopReadAloud"]);
  await settled();
  expect(t.api.startCapture).toHaveBeenCalledTimes(1);
  t.capturing();
  act(() => t.controls().press());
  await settled();
  expect(stopReadAloud).toHaveBeenCalledTimes(1);
  await t.send({ type: "done", capture_id: t.id() });
  act(() => t.controls().toggle());
  expect(stopReadAloud).toHaveBeenCalledTimes(2);
});

test("release, cancel and a refused press leave read-aloud alone", async () => {
  const t = setup({ status: SETUP });
  await settled();
  vi.mocked(stopReadAloud).mockClear();
  t.statuses.push(SETUP);
  act(() => t.controls().press());
  await settled();
  expect(t.api.status).toHaveBeenCalledTimes(2);
  expect(stopReadAloud).not.toHaveBeenCalled();
  const u = setup();
  await recording(u, "press");
  vi.mocked(stopReadAloud).mockClear();
  act(() => u.controls().press());
  act(() => u.controls().release());
  act(() => u.controls().cancel());
  expect(stopReadAloud).not.toHaveBeenCalled();
});

test("the hook imports only react, ./dictation* and ./readAloud", () => {
  const source = readCss(import.meta.url, "useDictation.ts");
  const specifiers = [...source.matchAll(/(?:from|import)\s+"([^"]+)"/g)].map((m) => m[1]);
  expect(specifiers.length).toBeGreaterThan(0);
  for (const specifier of specifiers)
    expect(specifier).toMatch(/^(react|\.\/dictation\w*|\.\/readAloud)$/);
});

test.each(["hidden", "visible again", "pagehide", "cancel", "unmount"])(
  "%s invalidates a pending ready start; returning needs a fresh mic action",
  async (way) => {
    const t = setup({ status: SETUP });
    await settled();
    let resolve!: (status: VoiceStatus) => void;
    t.api.status.mockImplementationOnce(
      () => new Promise<VoiceStatus>((ready) => (resolve = ready)),
    );
    act(() => t.controls().toggle());
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
    try {
      if (way === "hidden" || way === "visible again") {
        visibility.mockReturnValue("hidden");
        act(() => document.dispatchEvent(new Event("visibilitychange")));
        if (way === "visible again") {
          visibility.mockReturnValue("visible");
          act(() => document.dispatchEvent(new Event("visibilitychange")));
        }
      } else if (way === "pagehide") {
        act(() => window.dispatchEvent(new Event("pagehide")));
      } else if (way === "cancel") {
        act(() => t.controls().cancel());
      } else {
        t.hook.unmount();
      }
      await act(async () => resolve(READY));
      await settled();
      expect(t.api.socket).not.toHaveBeenCalled();
      expect(t.api.startCapture).not.toHaveBeenCalled();
      if (way !== "unmount") {
        visibility.mockReturnValue("visible");
        t.api.status.mockResolvedValueOnce(READY);
        act(() => t.controls().toggle());
        await settled();
        expect(t.api.startCapture).toHaveBeenCalledTimes(1);
      }
    } finally {
      visibility.mockRestore();
    }
  },
);

test.each(["press", "toggle"] as const)(
  "a ready %s cannot open capture while hidden",
  async (how) => {
    const t = setup();
    await settled();
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    try {
      act(() => t.controls()[how]());
      expect(t.api.socket).not.toHaveBeenCalled();
      expect(t.api.startCapture).not.toHaveBeenCalled();
      expect(t.view().phase).toBe("idle");
    } finally {
      visibility.mockRestore();
    }
  },
);
