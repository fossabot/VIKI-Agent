/**
 * `createVoiceboxFeed`: a `speak` fake that hands out a controllable promise per call, so each
 * test drives ordering, waits and aborts by hand. `startAudio`: a minimal `ReadAloud` fake with
 * just `isCurrent`/`playAudio`/`subscribe`, enough to drive staleness from a `stop()`-like notify.
 */
import { expect, test, vi } from "vitest";
import type { ReadAloud } from "./readAloud";
import { VoiceboxError } from "./voiceboxApi";
import { createVoiceboxFeed, startAudio, type VoiceboxFeed } from "./voiceboxFeed";

async function flush(): Promise<void> {
  for (let turn = 0; turn < 10; turn += 1) await Promise.resolve();
}

const bytes = (n: number) => new ArrayBuffer(n);

interface Call {
  text: string;
  signal: AbortSignal;
  resolve(value: ArrayBuffer): void;
  reject(error: unknown): void;
}

function speakFake() {
  const calls: Call[] = [];
  const speak = (text: string, signal: AbortSignal): Promise<ArrayBuffer> => {
    let resolve!: (value: ArrayBuffer) => void;
    let reject!: (error: unknown) => void;
    const promise = new Promise<ArrayBuffer>((res, rej) => {
      resolve = res;
      reject = rej;
    });
    calls.push({ text, signal, resolve, reject });
    return promise;
  };
  return { speak, calls };
}

test("drains pushed chunks in order", async () => {
  const { speak, calls } = speakFake();
  const feed = createVoiceboxFeed(speak);
  feed.push("a");
  feed.push("b");

  const p1 = feed.next(new AbortController().signal);
  expect(calls[0]?.text).toBe("a");
  const buf1 = bytes(1);
  calls[0]?.resolve(buf1);
  expect(await p1).toBe(buf1);

  const p2 = feed.next(new AbortController().signal);
  expect(calls[1]?.text).toBe("b");
  const buf2 = bytes(2);
  calls[1]?.resolve(buf2);
  expect(await p2).toBe(buf2);
});

test("next waits for a push and wakes once one arrives", async () => {
  const { speak, calls } = speakFake();
  const feed = createVoiceboxFeed(speak);
  let settled = false;
  const p = feed.next(new AbortController().signal).then((value) => {
    settled = true;
    return value;
  });
  await flush();
  expect(settled).toBe(false);
  expect(calls).toHaveLength(0);

  feed.push("late");
  await flush();
  expect(calls[0]?.text).toBe("late");
  const buf = bytes(3);
  calls[0]?.resolve(buf);
  expect(await p).toBe(buf);
  expect(settled).toBe(true);
});

test("end on an empty feed resolves next to null", async () => {
  const feed = createVoiceboxFeed(speakFake().speak);
  feed.end();
  expect(await feed.next(new AbortController().signal)).toBeNull();
});

test("end while next is waiting wakes it to null", async () => {
  const feed = createVoiceboxFeed(speakFake().speak);
  const p = feed.next(new AbortController().signal);
  await flush();
  feed.end();
  expect(await p).toBeNull();
});

test("drops queued chunks of an older group", async () => {
  const { speak, calls } = speakFake();
  const feed = createVoiceboxFeed(speak);
  feed.push("a", 0);
  feed.push("b", 0);
  feed.push("c", 1);

  const p = feed.next(new AbortController().signal);
  expect(calls).toHaveLength(1);
  expect(calls[0]?.text).toBe("c");
  const buf = bytes(4);
  calls[0]?.resolve(buf);
  expect(await p).toBe(buf);

  // "a" and "b" were dropped, not just deferred: nothing is left to hand out.
  feed.end();
  expect(await feed.next(new AbortController().signal)).toBeNull();
});

test("ignores blank text and any push after end", async () => {
  const { speak, calls } = speakFake();
  const feed = createVoiceboxFeed(speak);
  feed.push("   ");
  feed.push("");
  feed.end();
  feed.push("late");
  expect(await feed.next(new AbortController().signal)).toBeNull();
  expect(calls).toHaveLength(0);
});

test("aborts while next is waiting for a push", async () => {
  const feed = createVoiceboxFeed(speakFake().speak);
  const controller = new AbortController();
  const p = feed.next(controller.signal);
  await flush();
  controller.abort();
  await expect(p).rejects.toMatchObject({ name: "AbortError" });
});

test("aborts while speak is in flight, without recording a failure code", async () => {
  const { speak, calls } = speakFake();
  const feed = createVoiceboxFeed(speak);
  feed.push("a");
  const controller = new AbortController();
  const p = feed.next(controller.signal);
  controller.abort();
  calls[0]?.reject(new DOMException("aborted", "AbortError"));
  await expect(p).rejects.toMatchObject({ name: "AbortError" });
  expect(feed.failure()).toBeNull();
});

test("keeps the first failure code across a later, different failure", async () => {
  const { speak, calls } = speakFake();
  const feed = createVoiceboxFeed(speak);
  feed.push("a");
  feed.push("b");

  const p1 = feed.next(new AbortController().signal);
  calls[0]?.reject(new VoiceboxError("voicebox_timeout"));
  await expect(p1).rejects.toBeInstanceOf(VoiceboxError);
  expect(feed.failure()).toBe("voicebox_timeout");

  const p2 = feed.next(new AbortController().signal);
  calls[1]?.reject(new Error("boom"));
  await expect(p2).rejects.toThrow("boom");
  expect(feed.failure()).toBe("voicebox_timeout");
});

test('a non-VoiceboxError failure is recorded as "unavailable"', async () => {
  const { speak, calls } = speakFake();
  const feed = createVoiceboxFeed(speak);
  feed.push("a");
  const p = feed.next(new AbortController().signal);
  calls[0]?.reject(new Error("boom"));
  await expect(p).rejects.toThrow("boom");
  expect(feed.failure()).toBe("unavailable");
});

type Controller = Pick<ReadAloud, "isCurrent" | "playAudio" | "subscribe">;

function fakeController() {
  let current = 0;
  let playAudioResult = true;
  const listeners = new Set<() => void>();
  const playAudioCalls: {
    ticket: number;
    owner: string;
    first: ArrayBuffer;
    feed: VoiceboxFeed;
  }[] = [];
  const controller: Controller = {
    isCurrent: (ticket) => ticket === current,
    playAudio: (ticket, owner, first, feed) => {
      playAudioCalls.push({ ticket, owner, first, feed: feed as VoiceboxFeed });
      return playAudioResult;
    },
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
  return {
    controller,
    playAudioCalls,
    begin: (ticket: number) => {
      current = ticket;
    },
    stop: () => {
      current = -1;
      for (const listener of [...listeners]) listener();
    },
    declinePlayAudio: () => {
      playAudioResult = false;
    },
    listenerCount: () => listeners.size,
  };
}

test("startAudio plays the first chunk and reports playing", async () => {
  const t = fakeController();
  t.begin(1);
  const { speak, calls } = speakFake();
  const feed = createVoiceboxFeed(speak);
  feed.push("hi");

  const resultP = startAudio(t.controller, 1, "owner", feed);
  await flush();
  const buf = bytes(5);
  calls[0]?.resolve(buf);

  expect(await resultP).toBe("playing");
  expect(t.playAudioCalls).toEqual([{ ticket: 1, owner: "owner", first: buf, feed }]);
  expect(t.listenerCount()).toBe(0);
});

test("startAudio is stale immediately when the ticket already isn't current", async () => {
  const t = fakeController();
  t.begin(1);
  const feed = createVoiceboxFeed(speakFake().speak);
  expect(await startAudio(t.controller, 2, "owner", feed)).toBe("stale");
  expect(t.listenerCount()).toBe(0);
});

test("startAudio goes stale when stop() fires mid-wait, aborting the feed's signal", async () => {
  const t = fakeController();
  t.begin(1);
  const { speak, calls } = speakFake();
  const feed = createVoiceboxFeed(speak);
  feed.push("hi");

  const resultP = startAudio(t.controller, 1, "owner", feed);
  await flush();
  expect(calls[0]?.signal.aborted).toBe(false);

  t.stop();
  expect(calls[0]?.signal.aborted).toBe(true);
  calls[0]?.reject(new DOMException("aborted", "AbortError"));

  expect(await resultP).toBe("stale");
  expect(t.listenerCount()).toBe(0);
});

test("startAudio reports the feed's failure code when the first request fails", async () => {
  const t = fakeController();
  t.begin(1);
  const { speak, calls } = speakFake();
  const feed = createVoiceboxFeed(speak);
  feed.push("hi");

  const resultP = startAudio(t.controller, 1, "owner", feed);
  await flush();
  calls[0]?.reject(new VoiceboxError("voicebox_timeout"));

  expect(await resultP).toEqual({ failed: "voicebox_timeout" });
  expect(t.listenerCount()).toBe(0);
});

test("startAudio reports nothing to say when the feed ends with no chunk", async () => {
  const t = fakeController();
  t.begin(1);
  const feed = createVoiceboxFeed(speakFake().speak);
  feed.end();
  expect(await startAudio(t.controller, 1, "owner", feed)).toEqual({ failed: "nothing" });
  expect(t.listenerCount()).toBe(0);
});

test("startAudio is stale when playAudio itself declines", async () => {
  const t = fakeController();
  t.begin(1);
  t.declinePlayAudio();
  const { speak, calls } = speakFake();
  const feed = createVoiceboxFeed(speak);
  feed.push("hi");

  const resultP = startAudio(t.controller, 1, "owner", feed);
  await flush();
  calls[0]?.resolve(bytes(1));

  expect(await resultP).toBe("stale");
  expect(t.listenerCount()).toBe(0);
});

test("a deadline before the first chunk fails voicebox_timeout", async () => {
  const t = fakeController();
  t.begin(1);
  const { speak, calls } = speakFake();
  const feed = createVoiceboxFeed(speak);
  feed.push("hi");
  const deadline = new AbortController();

  const resultP = startAudio(t.controller, 1, "owner", feed, deadline.signal);
  await flush();
  expect(calls[0]?.signal.aborted).toBe(false);

  deadline.abort();
  expect(calls[0]?.signal.aborted).toBe(true);
  calls[0]?.reject(new DOMException("aborted", "AbortError"));

  expect(await resultP).toEqual({ failed: "voicebox_timeout" });
  expect(t.listenerCount()).toBe(0);
});

test("a stale ticket wins over the deadline", async () => {
  const t = fakeController();
  t.begin(1);
  const { speak, calls } = speakFake();
  const feed = createVoiceboxFeed(speak);
  feed.push("hi");
  const deadline = new AbortController();

  const resultP = startAudio(t.controller, 1, "owner", feed, deadline.signal);
  await flush();

  // The deadline fires first, proven wired by the local signal it aborts...
  deadline.abort();
  expect(calls[0]?.signal.aborted).toBe(true);

  // ...but the ticket also goes stale before the wait settles, and that must win.
  t.stop();
  calls[0]?.reject(new DOMException("aborted", "AbortError"));

  expect(await resultP).toBe("stale");
  expect(t.listenerCount()).toBe(0);
});

test("a deadline after the first chunk changes nothing", async () => {
  const t = fakeController();
  t.begin(1);
  const { speak, calls } = speakFake();
  const feed = createVoiceboxFeed(speak);
  feed.push("hi");
  const deadline = new AbortController();

  const resultP = startAudio(t.controller, 1, "owner", feed, deadline.signal);
  await flush();
  calls[0]?.resolve(bytes(6));

  expect(await resultP).toBe("playing");
  expect(t.playAudioCalls).toHaveLength(1);

  deadline.abort();
  expect(t.listenerCount()).toBe(0);
});

test("the deadline's listener is removed once the first chunk arrives", async () => {
  const t = fakeController();
  t.begin(1);
  const { speak, calls } = speakFake();
  const feed = createVoiceboxFeed(speak);
  feed.push("hi");
  const deadline = new AbortController();
  const removed = vi.spyOn(deadline.signal, "removeEventListener");

  const resultP = startAudio(t.controller, 1, "owner", feed, deadline.signal);
  await flush();
  calls[0]?.resolve(bytes(6));

  await resultP;
  expect(removed).toHaveBeenCalledWith("abort", expect.any(Function));
});
