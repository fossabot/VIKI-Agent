import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { PcmBatcher, toInt16 } from "./capturePcm";
import { microphones, startCapture } from "./dictationCapture";
import { MICROPHONE_KEY } from "./dictationTypes";

const samples = (n: number, value = 0.5) => new Float32Array(n).fill(value);
const int16 = (buffer: ArrayBuffer) => Array.from(new Int16Array(buffer));

test("toInt16 clamps to the sample range and rounds", () => {
  expect(toInt16(1)).toBe(32767);
  expect(toInt16(-1)).toBe(-32767);
  expect(toInt16(7)).toBe(32767);
  expect(toInt16(-7)).toBe(-32767);
  expect(toInt16(0)).toBe(0);
  expect(toInt16(0.5)).toBe(16384);
  expect(toInt16(1 / 32767)).toBe(1);
});

test("4,000 samples make two 3,200-byte batches and a 1,600-byte flush", () => {
  const batcher = new PcmBatcher();
  const batches = batcher.push([samples(4000)]);
  expect(batches.map((b) => b.byteLength)).toEqual([3200, 3200]);
  expect(batcher.push([])).toEqual([]);
  expect(batcher.flush()?.byteLength).toBe(1600);
  expect(batcher.flush()).toBeNull();
});

test("a batch completes across pushes", () => {
  const batcher = new PcmBatcher(4);
  expect(batcher.push([samples(3)])).toEqual([]);
  const [batch] = batcher.push([samples(3)]);
  expect(batch?.byteLength).toBe(8);
  expect(batcher.flush()?.byteLength).toBe(4);
});

test("channels are averaged to mono", () => {
  const batcher = new PcmBatcher(2);
  const [batch] = batcher.push([
    Float32Array.of(1, -1),
    Float32Array.of(0, -0.5),
    Float32Array.of(0.5, -0.5),
  ]);
  expect(int16(batch as ArrayBuffer)).toEqual([toInt16(0.5), toInt16(-2 / 3)]);
});

// -- startCapture, with the browser's audio objects faked on globalThis --

type Log = string[];

class FakeTrack {
  private readonly listeners: (() => void)[] = [];
  constructor(private readonly log: Log) {}
  addEventListener(type: string, listener: () => void) {
    if (type === "ended") this.listeners.push(listener);
  }
  end() {
    for (const listener of this.listeners) listener();
  }
  stop() {
    this.log.push("track.stop");
  }
}

interface World {
  log: Log;
  tracks: FakeTrack[];
  constraints: unknown[];
  nodes: FakeNode[];
  modules: string[];
  contexts: { options: unknown }[];
  flushReply: "answer" | "silent";
}

let world: World;

class FakeNode {
  port: {
    onmessage: ((event: { data: unknown }) => void) | null;
    postMessage(message: unknown): void;
  };
  constructor(
    readonly context: unknown,
    readonly name: string,
  ) {
    world.nodes.push(this);
    this.port = {
      onmessage: null,
      postMessage: (message) => {
        if (message !== "flush") return;
        world.log.push("flush");
        if (world.flushReply === "silent") return;
        this.port.onmessage?.({ data: new Int16Array(3).buffer });
        this.port.onmessage?.({ data: { flushed: true } });
      },
    };
  }
  send(data: unknown) {
    this.port.onmessage?.({ data });
  }
  connect() {}
  disconnect() {
    world.log.push("node.disconnect");
  }
}

class FakeContext {
  readonly destination = {};
  audioWorklet = {
    addModule: (url: string) => {
      world.modules.push(url);
      return Promise.resolve();
    },
  };
  constructor(options: unknown) {
    world.contexts.push({ options });
  }
  createMediaStreamSource() {
    return {
      connect() {},
      disconnect() {
        world.log.push("source.disconnect");
      },
    };
  }
  close() {
    world.log.push("ctx.close");
    return Promise.resolve();
  }
}

function domException(name: string) {
  return Object.assign(new Error(name), { name });
}

function install(
  getUserMedia?: (constraints: unknown) => Promise<unknown>,
  devices: unknown[] = [],
) {
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: {
      getUserMedia:
        getUserMedia ??
        ((constraints: unknown) => {
          world.constraints.push(constraints);
          const track = new FakeTrack(world.log);
          world.tracks.push(track);
          return Promise.resolve({ getTracks: () => [track] });
        }),
      enumerateDevices: () => Promise.resolve(devices),
    },
  });
}

beforeEach(() => {
  world = {
    log: [],
    tracks: [],
    constraints: [],
    nodes: [],
    modules: [],
    contexts: [],
    flushReply: "answer",
  };
  vi.stubGlobal("AudioContext", FakeContext);
  vi.stubGlobal("AudioWorkletNode", FakeNode);
  install();
  localStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  Reflect.deleteProperty(navigator, "mediaDevices");
});

const options = (over: Record<string, unknown> = {}) => ({
  deviceId: null,
  workletUrl: "/assets/captureWorklet.js",
  onAudio: vi.fn(),
  onEnded: vi.fn(),
  ...over,
});

test("the stream asks for one unprocessed channel at 16 kHz", async () => {
  const handle = await startCapture(options({ deviceId: "mic-1" }));
  expect(world.constraints).toEqual([
    {
      audio: {
        deviceId: { exact: "mic-1" },
        channelCount: 1,
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: false,
      },
    },
  ]);
  expect(world.contexts[0]?.options).toEqual({ sampleRate: 16000 });
  expect(world.modules).toEqual(["/assets/captureWorklet.js"]);
  expect(world.nodes[0]?.name).toBe("viki-dictation");
  expect(handle.deviceId).toBe("mic-1");
});

test("no device id leaves the constraint out", async () => {
  await startCapture(options());
  expect(world.constraints[0]).toEqual({
    audio: {
      channelCount: 1,
      echoCancellation: false,
      noiseSuppression: false,
      autoGainControl: false,
    },
  });
});

test("worklet batches are forwarded to onAudio", async () => {
  const opts = options();
  await startCapture(opts);
  const batch = new Int16Array(1600).buffer;
  world.nodes[0]?.send(batch);
  expect(opts.onAudio).toHaveBeenCalledExactlyOnceWith(batch);
});

test("onEnded fires once for any track's ended", async () => {
  const opts = options();
  await startCapture(opts);
  world.tracks[0]?.end();
  world.tracks[0]?.end();
  expect(opts.onEnded).toHaveBeenCalledTimes(1);
});

test("stop flushes before the tracks end", async () => {
  const opts = options();
  const handle = await startCapture(opts);
  await handle.stop();
  expect(world.log).toEqual([
    "flush",
    "track.stop",
    "source.disconnect",
    "node.disconnect",
    "ctx.close",
  ]);
  // The flushed batch reached onAudio before the tracks stopped.
  expect(opts.onAudio).toHaveBeenCalledTimes(1);
});

test("stop gives up on a silent worklet after 200 ms", async () => {
  vi.useFakeTimers();
  world.flushReply = "silent";
  const handle = await startCapture(options());
  const stopped = handle.stop();
  await vi.advanceTimersByTimeAsync(199);
  expect(world.log).toEqual(["flush"]);
  await vi.advanceTimersByTimeAsync(1);
  await stopped;
  expect(world.log).toEqual([
    "flush",
    "track.stop",
    "source.disconnect",
    "node.disconnect",
    "ctx.close",
  ]);
});

test("stop is idempotent and a stopped capture reports no ended", async () => {
  const opts = options();
  const handle = await startCapture(opts);
  await Promise.all([handle.stop(), handle.stop()]);
  await handle.stop();
  expect(world.log.filter((entry) => entry === "ctx.close")).toHaveLength(1);
  world.tracks[0]?.end();
  expect(opts.onEnded).not.toHaveBeenCalled();
});

test("a saved id that is gone retries without it and clears the key", async () => {
  localStorage.setItem(MICROPHONE_KEY, "gone");
  let calls = 0;
  install((constraints) => {
    calls += 1;
    if (calls === 1) return Promise.reject(domException("OverconstrainedError"));
    world.constraints.push(constraints);
    const track = new FakeTrack(world.log);
    return Promise.resolve({ getTracks: () => [track] });
  });
  const handle = await startCapture(options({ deviceId: "gone" }));
  expect(calls).toBe(2);
  expect(world.constraints[0]).not.toHaveProperty("audio.deviceId");
  expect(localStorage.getItem(MICROPHONE_KEY)).toBeNull();
  expect(handle.deviceId).toBeNull();
});

test("NotFoundError with a saved id retries too, once", async () => {
  localStorage.setItem(MICROPHONE_KEY, "gone");
  let calls = 0;
  install(() => {
    calls += 1;
    return Promise.reject(domException("NotFoundError"));
  });
  await expect(startCapture(options({ deviceId: "gone" }))).rejects.toMatchObject({
    name: "NotFoundError",
  });
  expect(calls).toBe(2);
});

test("a denial, or a missing device without a saved id, rejects at once", async () => {
  localStorage.setItem(MICROPHONE_KEY, "kept");
  let calls = 0;
  install(() => {
    calls += 1;
    return Promise.reject(domException("NotAllowedError"));
  });
  await expect(startCapture(options({ deviceId: "kept" }))).rejects.toMatchObject({
    name: "NotAllowedError",
  });
  expect(calls).toBe(1);
  expect(localStorage.getItem(MICROPHONE_KEY)).toBe("kept");

  calls = 0;
  install(() => {
    calls += 1;
    return Promise.reject(domException("NotFoundError"));
  });
  await expect(startCapture(options())).rejects.toMatchObject({ name: "NotFoundError" });
  expect(calls).toBe(1);
});

test("a worklet that fails to load releases the microphone", async () => {
  class Failing extends FakeContext {
    override audioWorklet = { addModule: () => Promise.reject(domException("AbortError")) };
  }
  vi.stubGlobal("AudioContext", Failing);
  await expect(startCapture(options())).rejects.toMatchObject({ name: "AbortError" });
  expect(world.log).toEqual(["track.stop", "ctx.close"]);
});

test("microphones lists audio inputs only", async () => {
  install(undefined, [
    { kind: "audioinput", deviceId: "a", label: "Desk", groupId: "g" },
    { kind: "videoinput", deviceId: "v", label: "Camera" },
    { kind: "audiooutput", deviceId: "o", label: "Speakers" },
    { kind: "audioinput", deviceId: "b", label: "" },
  ]);
  expect(await microphones()).toEqual([
    { deviceId: "a", label: "Desk" },
    { deviceId: "b", label: "" },
  ]);
});

// -- the worklet module, with the AudioWorkletGlobalScope faked --

test("the worklet registers viki-dictation, posts batches and answers flush", async () => {
  class Processor {
    port = { onmessage: null as ((event: { data: unknown }) => void) | null, postMessage: vi.fn() };
  }
  const registerProcessor = vi.fn();
  vi.stubGlobal("AudioWorkletProcessor", Processor);
  vi.stubGlobal("registerProcessor", registerProcessor);
  await import("./captureWorklet");
  const [name, ctor] = registerProcessor.mock.calls[0] as [string, new () => Processor];
  expect(name).toBe("viki-dictation");
  const processor = new ctor() as Processor & { process(inputs: Float32Array[][]): boolean };

  expect(processor.process([[]])).toBe(true);
  expect(processor.process([[samples(2000)]])).toBe(true);
  expect(processor.port.postMessage).toHaveBeenCalledTimes(1);
  const [batch, transfer] = processor.port.postMessage.mock.calls[0] as [ArrayBuffer, unknown[]];
  expect(batch.byteLength).toBe(3200);
  expect(transfer).toEqual([batch]);

  processor.port.postMessage.mockClear();
  processor.port.onmessage?.({ data: "flush" });
  const posted = processor.port.postMessage.mock.calls.map(([message]) => message);
  expect((posted[0] as ArrayBuffer).byteLength).toBe(800);
  expect(posted[1]).toEqual({ flushed: true });

  processor.port.postMessage.mockClear();
  processor.port.onmessage?.({ data: "flush" });
  expect(processor.port.postMessage.mock.calls.map(([message]) => message)).toEqual([
    { flushed: true },
  ]);
});
