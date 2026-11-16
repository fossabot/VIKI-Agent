/**
 * `playAudio`, the controller's audio path: a fake `AudioOut` that logs, and a feed whose answers
 * each test hands out by hand.
 */
import { expect, test } from "vitest";
import { type AudioFeed, type AudioOut, createReadAloud, webAudio } from "./readAloud";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** Settle the microtask chain a playback builds, with no timer and no sleep. */
async function flush(): Promise<void> {
  for (let turn = 0; turn < 10; turn += 1) await Promise.resolve();
}

/** A chunk of audio; its byte length names it in the log. */
const chunk = (name: number) => new ArrayBuffer(name);
const sound = (name: number) => ({ length: name }) as AudioBuffer;

interface Ask {
  signal: AbortSignal;
  resolve(bytes: ArrayBuffer | null): void;
  reject(error: unknown): void;
}

function setup(withAudio = true) {
  const log: string[] = [];
  // Answers for the coming decodes; when none is queued, a chunk decodes at once.
  const decodes: Promise<AudioBuffer>[] = [];
  const ends: (() => void)[] = [];
  const out: AudioOut = {
    decode(bytes) {
      log.push(`decode ${bytes.byteLength}`);
      return decodes.shift() ?? Promise.resolve(sound(bytes.byteLength));
    },
    start(buffer, onEnded) {
      log.push(`start ${buffer.length}`);
      let done = false;
      const end = () => {
        if (done) return;
        done = true;
        log.push(`ended ${buffer.length}`);
        onEnded();
      };
      ends.push(end);
      // Like a real source: stopping one that already ended changes nothing.
      return () => {
        if (done) return;
        log.push(`stop ${buffer.length}`);
        end();
      };
    },
    resume: async () => {
      log.push("resume");
    },
    suspend: async () => {
      log.push("suspend");
    },
  };
  const asks: Ask[] = [];
  const feed: AudioFeed = {
    next(signal) {
      log.push("next");
      const { promise, resolve, reject } = deferred<ArrayBuffer | null>();
      asks.push({ signal, resolve, reject });
      return promise;
    },
  };
  const target = new EventTarget();
  const devices = new EventTarget();
  const controller = createReadAloud({
    synth: {
      paused: false,
      cancel: () => log.push("synth cancel"),
      pause: () => log.push("synth pause"),
      resume: () => log.push("synth resume"),
    } as unknown as SpeechSynthesis,
    makeUtterance: () => {
      throw new Error("no speech in these tests");
    },
    audio: withAudio ? out : null,
    target,
    devices,
    setTimeout: () => 0,
    clearTimeout: () => {},
  });
  let notified = 0;
  controller.subscribe(() => {
    notified += 1;
  });
  return {
    log,
    decodes,
    asks,
    feed,
    target,
    devices,
    controller,
    notified: () => notified,
    /** Begins for `owner` and plays chunk 1 with the rest from the feed. */
    async start(owner = "a") {
      const ok = controller.playAudio(controller.begin(owner), owner, chunk(1), feed);
      await flush();
      return ok;
    },
    /** Answers the feed's open request. */
    async give(bytes: ArrayBuffer | null) {
      asks.at(-1)?.resolve(bytes);
      await flush();
    },
    /** Ends the chunk now sounding. */
    async end() {
      ends.at(-1)?.();
      await flush();
    },
  };
}

type Setup = ReturnType<typeof setup>;

test("plays the chunks in order with one request ahead, and ends idle and suspended", async () => {
  const t = setup();
  expect(await t.start()).toBe(true);
  expect(t.controller.statusOf("a")).toBe("speaking");
  expect(t.log).toEqual(["resume", "next", "decode 1", "start 1"]);
  // Chunk 2 arrives while chunk 1 still sounds: nothing more is asked for or started yet.
  await t.give(chunk(2));
  expect(t.log).toHaveLength(4);
  await t.end();
  expect(t.asks).toHaveLength(2);
  await t.end();
  await t.give(chunk(3));
  await t.end();
  expect(t.controller.statusOf("a")).toBe("speaking");
  await t.give(null);
  expect(t.log).toEqual([
    "resume",
    "next",
    "decode 1",
    "start 1",
    "ended 1",
    "next",
    "decode 2",
    "start 2",
    "ended 2",
    "next",
    "decode 3",
    "start 3",
    "ended 3",
    "suspend",
  ]);
  expect(t.asks).toHaveLength(3);
  expect(t.controller.statusOf("a")).toBe("idle");
  expect(t.controller.owner()).toBeNull();
  expect(t.notified()).toBe(3);
});

test("asks for the next chunk only after its bytes arrive", async () => {
  const t = setup();
  await t.start();
  expect(t.asks).toHaveLength(1);
  // Chunk 1 ends before chunk 2's bytes are here: the playback waits and asks for nothing else.
  await t.end();
  expect(t.asks).toHaveLength(1);
  expect(t.controller.statusOf("a")).toBe("speaking");
  await t.give(chunk(2));
  expect(t.asks).toHaveLength(2);
  expect(t.log.slice(-3)).toEqual(["next", "decode 2", "start 2"]);
});

test("pause and resume suspend and resume the audio, a device change pauses it", async () => {
  const t = setup();
  await t.start();
  t.controller.pause();
  expect(t.controller.statusOf("a")).toBe("paused");
  t.controller.pause();
  t.controller.resume();
  expect(t.controller.statusOf("a")).toBe("speaking");
  t.controller.resume();
  t.devices.dispatchEvent(new Event("devicechange"));
  expect(t.controller.statusOf("a")).toBe("paused");
  expect(t.log.slice(4)).toEqual(["suspend", "resume", "suspend"]);
  expect(t.notified()).toBe(5);
  // A stop while paused leaves the output suspended; the next playback resumes it first.
  t.controller.stop();
  expect(t.controller.statusOf("a")).toBe("idle");
  await t.start("b");
  expect(t.log.slice(7)).toEqual(["stop 1", "ended 1", "resume", "next", "decode 1", "start 1"]);
});

const stops: { name: string; act: (t: Setup) => void }[] = [
  { name: "stop", act: (t) => t.controller.stop() },
  {
    name: "another owner's begin",
    act: (t) => {
      t.controller.begin("b");
    },
  },
  {
    name: "pagehide",
    act: (t) => {
      t.target.dispatchEvent(new Event("pagehide"));
    },
  },
];

test.each(stops)(
  "$name stops the source, aborts the feed and ignores a late answer",
  async ({ act }) => {
    const t = setup();
    await t.start();
    act(t);
    expect(t.log.slice(4)).toEqual(["stop 1", "ended 1"]);
    expect(t.asks[0]?.signal.aborted).toBe(true);
    expect(t.controller.statusOf("a")).toBe("idle");
    const notified = t.notified();
    await t.give(chunk(2));
    expect(t.log).toHaveLength(6);
    expect(t.asks).toHaveLength(1);
    expect(t.notified()).toBe(notified);
    expect(t.controller.statusOf("a")).toBe("idle");
  },
);

test("a stop during a decode starts nothing when the decode ends late", async () => {
  const t = setup();
  const late = deferred<AudioBuffer>();
  t.decodes.push(late.promise);
  await t.start();
  t.controller.stop();
  late.resolve(sound(1));
  t.asks[0]?.reject(new DOMException("aborted", "AbortError"));
  await flush();
  expect(t.log).toEqual(["resume", "next", "decode 1"]);
  expect(t.controller.statusOf("a")).toBe("idle");
});

const lateAnswers: { name: string; answer: (t: Setup) => void }[] = [
  { name: "bytes", answer: (t) => t.asks[0]?.resolve(chunk(2)) },
  {
    name: "an abort error",
    answer: (t) => t.asks[0]?.reject(new DOMException("aborted", "AbortError")),
  },
];

test.each(lateAnswers)(
  "a stop while the next chunk is on its way ignores $name arriving late",
  async ({ answer }) => {
    const t = setup();
    await t.start();
    await t.end();
    t.controller.stop();
    const notified = t.notified();
    answer(t);
    await flush();
    expect(t.log.slice(4)).toEqual(["ended 1"]);
    expect(t.asks).toHaveLength(1);
    expect(t.controller.statusOf("a")).toBe("idle");
    expect(t.notified()).toBe(notified);
  },
);

test("a feed that fails fails the playback after its chunk, and a stop clears it", async () => {
  const t = setup();
  await t.start();
  t.asks[0]?.reject(new Error("voicebox_unreachable"));
  await flush();
  expect(t.controller.statusOf("a")).toBe("speaking");
  await t.end();
  expect(t.controller.statusOf("a")).toBe("failed");
  expect(t.controller.owner()).toBe("a");
  expect(t.notified()).toBe(3);
  t.controller.stop();
  expect(t.controller.statusOf("a")).toBe("idle");
  expect(t.log.slice(4)).toEqual(["ended 1"]);
});

test("a decode that fails fails the playback, aborts the request ahead, and plays no late answer", async () => {
  const t = setup();
  const decoding = deferred<AudioBuffer>();
  t.decodes.push(decoding.promise);
  await t.start();
  decoding.reject(new Error("not audio"));
  await flush();
  expect(t.controller.statusOf("a")).toBe("failed");
  expect(t.asks[0]?.signal.aborted).toBe(true);
  await t.give(chunk(2));
  expect(t.log).toEqual(["resume", "next", "decode 1"]);
  expect(t.controller.statusOf("a")).toBe("failed");
});

test("a stale ticket, a spent ticket, another owner or no audio output plays nothing", async () => {
  const t = setup();
  const stale = t.controller.begin("a");
  t.controller.begin("b");
  expect(t.controller.playAudio(stale, "a", chunk(1), t.feed)).toBe(false);
  const ticket = t.controller.begin("c");
  expect(t.controller.playAudio(ticket, "other", chunk(1), t.feed)).toBe(false);
  expect(t.controller.playAudio(ticket, "c", chunk(1), t.feed)).toBe(true);
  expect(t.controller.playAudio(ticket, "c", chunk(2), t.feed)).toBe(false);
  await flush();
  expect(t.log).toEqual(["resume", "next", "decode 1", "start 1"]);

  const silent = setup(false);
  const mine = silent.controller.begin("a");
  expect(silent.controller.playAudio(mine, "a", chunk(1), silent.feed)).toBe(false);
  await flush();
  expect(silent.log).toEqual([]);
  expect(silent.controller.statusOf("a")).toBe("idle");
});

test("webAudio makes one context on first use and plays a buffer through its destination", async () => {
  const log: string[] = [];
  const destination = {};
  const sources: { buffer: AudioBuffer | null; onended: (() => void) | null }[] = [];
  let made = 0;
  let stopThrows = false;
  const context = {
    destination,
    decodeAudioData: async (bytes: ArrayBuffer) => {
      log.push(`decodeAudioData ${bytes.byteLength}`);
      return sound(bytes.byteLength);
    },
    createBufferSource: () => {
      const source = {
        buffer: null as AudioBuffer | null,
        onended: null as (() => void) | null,
        connect: (node: unknown) => log.push(node === destination ? "connect destination" : "?"),
        disconnect: () => log.push("disconnect"),
        start: () => log.push("start"),
        stop: () => {
          log.push("stop");
          if (stopThrows) throw new DOMException("already stopped", "InvalidStateError");
        },
      };
      sources.push(source);
      return source;
    },
    resume: async () => {
      log.push("resume");
    },
    suspend: async () => {
      log.push("suspend");
    },
  };
  const out = webAudio(() => {
    made += 1;
    return context as unknown as AudioContext;
  });
  await out.suspend();
  expect(made).toBe(0);
  await out.resume();
  const buffer = await out.decode(chunk(4));
  const stop = out.start(buffer, () => log.push("onEnded"));
  expect(sources[0]?.buffer).toBe(buffer);
  sources[0]?.onended?.();
  expect(log).toEqual([
    "resume",
    "decodeAudioData 4",
    "connect destination",
    "start",
    "disconnect",
    "onEnded",
  ]);
  stopThrows = true;
  expect(stop).not.toThrow();
  await out.suspend();
  expect(log.slice(6)).toEqual(["stop", "suspend"]);
  expect(made).toBe(1);
});
