import { describe, expect, it } from "vitest";
import {
  createWorkerParser,
  MAX_CACHED_NODES,
  MAX_SPAWN_FAILURES,
  type ParseRequest,
  type ParseResponse,
  type WorkerLike,
} from "./markdownParser";
import type { ParseResult } from "./markdownTree";

class FakeWorker implements WorkerLike {
  posted: ParseRequest[] = [];
  terminated = false;
  onmessage: ((event: MessageEvent<ParseResponse>) => void) | null = null;
  onerror: ((event: ErrorEvent) => void) | null = null;
  onmessageerror: ((event: MessageEvent) => void) | null = null;

  postMessage(message: ParseRequest): void {
    this.posted.push(message);
  }

  terminate(): void {
    this.terminated = true;
  }

  /** Answers the last posted job unless another id is named. */
  reply(result: ParseResult, id = this.posted.at(-1)?.id ?? 0): void {
    this.onmessage?.({ data: { id, result } } as MessageEvent<ParseResponse>);
  }

  error(): void {
    this.onerror?.({} as ErrorEvent);
  }

  messageError(): void {
    this.onmessageerror?.({} as MessageEvent);
  }
}

function tree(nodes: number): ParseResult {
  return { kind: "tree", tree: { type: "root", children: [] }, nodes };
}

/** A parser over fake workers and a hand-driven timer table; `throws` spawns fail first. */
function setup(throws = 0) {
  const workers: FakeWorker[] = [];
  let spawnCalls = 0;
  const timers = new Map<number, { callback: () => void; ms: number }>();
  let nextHandle = 1;
  const parser = createWorkerParser({
    spawn: () => {
      spawnCalls += 1;
      if (spawnCalls <= throws) throw new Error("no worker");
      const worker = new FakeWorker();
      workers.push(worker);
      return worker;
    },
    setTimer: (callback, ms) => {
      timers.set(nextHandle, { callback, ms });
      return nextHandle++;
    },
    clearTimer: (handle) => {
      timers.delete(handle);
    },
  });
  const fireTimer = () => {
    const [handle, timer] = [...timers][0] ?? [];
    if (handle === undefined || !timer) throw new Error("no timer pending");
    timers.delete(handle);
    timer.callback();
  };
  const seen: [string, ParseResult][] = [];
  const collect = (label: string) => (result: ParseResult) => {
    seen.push([label, result]);
  };
  return { parser, workers, timers, fireTimer, seen, collect, spawns: () => spawnCalls };
}

const texts = (worker: FakeWorker | undefined) => worker?.posted.map((job) => job.text);

describe("createWorkerParser", () => {
  it("runs one job at a time, first in first out across owners", () => {
    const { parser, workers, timers, seen, collect } = setup();
    parser.request({}, "a", 250, collect("a"));
    parser.request({}, "b", 2000, collect("b"));
    parser.request({}, "c", 250, collect("c"));
    expect(texts(workers[0])).toEqual(["a"]);
    expect([...timers.values()].map((timer) => timer.ms)).toEqual([250]);

    workers[0]?.reply(tree(1));
    expect(texts(workers[0])).toEqual(["a", "b"]);
    expect([...timers.values()].map((timer) => timer.ms)).toEqual([2000]);
    workers[0]?.reply(tree(2));
    workers[0]?.reply(tree(3));
    expect(seen.map(([label]) => label)).toEqual(["a", "b", "c"]);
    expect(workers).toHaveLength(1);
    expect(timers.size).toBe(0);
  });

  it("parses only the newest queued text of an owner and never replaces a job in flight", () => {
    const { parser, workers, timers, seen, collect } = setup();
    const streaming = {};
    const other = {};
    parser.request(streaming, "s1", 250, collect("s1"));
    parser.request(other, "o", 2000, collect("o"));
    parser.request(streaming, "s1 more", 250, collect("s2"));
    parser.request(streaming, "s1 more and more", 2000, collect("s3"));
    expect(texts(workers[0])).toEqual(["s1"]);

    workers[0]?.reply(tree(1));
    workers[0]?.reply(tree(1));
    expect(texts(workers[0])).toEqual(["s1", "o", "s1 more and more"]);
    expect([...timers.values()].map((timer) => timer.ms)).toEqual([2000]);
    workers[0]?.reply(tree(1));
    expect(seen.map(([label]) => label)).toEqual(["s1", "o", "s3"]);
  });

  it("terminates a worker past its deadline, answers timeout and respawns for the next job", () => {
    const { parser, workers, fireTimer, seen, collect } = setup();
    parser.request({}, "slow", 250, collect("slow"));
    parser.request({}, "next", 250, collect("next"));
    fireTimer();
    expect(workers[0]?.terminated).toBe(true);
    expect(seen).toEqual([["slow", { kind: "plain", reason: "timeout" }]]);
    expect(texts(workers[1])).toEqual(["next"]);

    // The dropped worker is ignored, even when it names the id now in flight.
    workers[0]?.reply(tree(1), workers[0]?.posted[0]?.id);
    workers[0]?.reply(tree(1), workers[1]?.posted[0]?.id);
    expect(seen).toHaveLength(1);
    expect(parser.cached("slow")).toBeUndefined();
  });

  it("never caches a timeout, so the same text is asked again", () => {
    const { parser, workers, fireTimer, seen, collect } = setup();
    parser.request({}, "slow", 250, collect("first"));
    fireTimer();
    parser.request({}, "slow", 2000, collect("second"));
    expect(seen.map(([label]) => label)).toEqual(["first"]);
    expect(texts(workers[1])).toEqual(["slow"]);
  });

  it("answers error on an error or messageerror event and respawns", () => {
    const { parser, workers, timers, seen, collect } = setup();
    parser.request({}, "a", 250, collect("a"));
    parser.request({}, "b", 250, collect("b"));
    parser.request({}, "c", 250, collect("c"));
    workers[0]?.error();
    expect(workers[0]?.terminated).toBe(true);
    expect(texts(workers[1])).toEqual(["b"]);
    workers[1]?.messageError();
    expect(workers[1]?.terminated).toBe(true);
    expect(texts(workers[2])).toEqual(["c"]);
    expect(seen).toEqual([
      ["a", { kind: "plain", reason: "error" }],
      ["b", { kind: "plain", reason: "error" }],
    ]);
    expect(timers.size).toBe(1);

    // An error from a dropped worker changes nothing.
    workers[0]?.error();
    expect(workers).toHaveLength(3);
    expect(seen).toHaveLength(2);
  });

  it("stops spawning after failures in a row and answers unavailable at once", () => {
    const { parser, workers, seen, collect } = setup();
    parser.request({}, "a", 250, collect("a"));
    parser.request({}, "b", 250, collect("b"));
    parser.request({}, "c", 250, collect("c"));
    parser.request({}, "queued", 250, collect("queued"));
    for (let n = 0; n < MAX_SPAWN_FAILURES; n += 1) workers[n]?.error();
    expect(workers).toHaveLength(MAX_SPAWN_FAILURES);
    expect(seen.at(-1)).toEqual(["queued", { kind: "plain", reason: "unavailable" }]);

    parser.request({}, "later", 250, collect("later"));
    expect(seen.at(-1)).toEqual(["later", { kind: "plain", reason: "unavailable" }]);
    expect(workers).toHaveLength(MAX_SPAWN_FAILURES);
  });

  it("resets the failure count on a success", () => {
    const { parser, workers, collect } = setup();
    for (const text of ["a", "b", "ok", "c", "d", "e"])
      parser.request({}, text, 250, collect(text));
    workers[0]?.error();
    workers[1]?.error();
    workers[2]?.reply(tree(1));
    workers[2]?.error();
    workers[3]?.error();
    expect(texts(workers[4])).toEqual(["e"]);
  });

  it("answers unavailable when spawn throws, and counts it as a failure", () => {
    const { parser, workers, seen, collect, spawns } = setup(2);
    parser.request({}, "a", 250, collect("a"));
    parser.request({}, "b", 250, collect("b"));
    expect(seen).toEqual([
      ["a", { kind: "plain", reason: "unavailable" }],
      ["b", { kind: "plain", reason: "unavailable" }],
    ]);
    parser.request({}, "c", 250, collect("c"));
    expect(texts(workers[0])).toEqual(["c"]);
    workers[0]?.error();
    parser.request({}, "d", 250, collect("d"));
    expect(seen.at(-1)).toEqual(["d", { kind: "plain", reason: "unavailable" }]);
    expect(spawns()).toBe(MAX_SPAWN_FAILURES);
  });

  it("ignores a message whose id is not the job in flight", () => {
    const { parser, workers, timers, seen, collect } = setup();
    parser.request({}, "a", 250, collect("a"));
    const id = workers[0]?.posted[0]?.id ?? 0;
    workers[0]?.reply(tree(1), id + 1);
    expect(seen).toEqual([]);
    expect(timers.size).toBe(1);
    workers[0]?.reply(tree(1), id);
    expect(seen).toHaveLength(1);
  });

  it("cancel drops a queued job and silences one in flight, whose result is still cached", () => {
    const { parser, workers, timers, seen, collect } = setup();
    const owner = {};
    const queuedOwner = {};
    parser.request(owner, "a", 250, collect("a"));
    parser.request(queuedOwner, "q", 250, collect("q"));
    parser.request({}, "b", 250, collect("b"));
    parser.cancel(owner);
    parser.cancel(queuedOwner);
    workers[0]?.reply(tree(4));
    expect(seen).toEqual([]);
    expect(parser.cached("a")).toEqual(tree(4));
    expect(texts(workers[0])).toEqual(["a", "b"]);
    workers[0]?.reply(tree(1));
    expect(seen.map(([label]) => label)).toEqual(["b"]);
    expect(timers.size).toBe(0);
  });

  it("answers a cached text synchronously and posts nothing", () => {
    const { parser, workers, timers, seen, collect } = setup();
    const owner = {};
    parser.request({}, "done", 250, collect("first"));
    workers[0]?.reply(tree(2));
    parser.request({}, "busy", 250, collect("busy"));
    parser.request(owner, "stale", 250, collect("stale"));
    parser.request(owner, "done", 250, collect("hit"));
    expect(seen.map(([label]) => label)).toEqual(["first", "hit"]);
    expect(seen[1]?.[1]).toEqual(tree(2));
    expect(texts(workers[0])).toEqual(["done", "busy"]);

    // The owner's older queued text was dropped with the hit.
    workers[0]?.reply(tree(1));
    expect(texts(workers[0])).toEqual(["done", "busy"]);
    expect(timers.size).toBe(0);
  });

  it("answers a queued duplicate from the cache without parsing it again", () => {
    const { parser, workers, seen, collect } = setup();
    parser.request({}, "same", 250, collect("one"));
    parser.request({}, "same", 250, collect("two"));
    workers[0]?.reply(tree(3));
    expect(seen).toEqual([
      ["one", tree(3)],
      ["two", tree(3)],
    ]);
    expect(texts(workers[0])).toEqual(["same"]);
  });

  it("evicts the least recently used results by node cost", () => {
    const { parser, workers, collect } = setup();
    const parse = (text: string, result: ParseResult) => {
      parser.request({}, text, 250, collect(text));
      workers[0]?.reply(result);
    };
    parse("x", tree(30_000));
    parse("y", tree(MAX_CACHED_NODES - 30_001));
    parse("depth", { kind: "plain", reason: "depth" });
    expect(parser.cached("depth")).toEqual({ kind: "plain", reason: "depth" });
    expect(parser.cached("x")).toEqual(tree(30_000));
    // The cache is full and "y" is now the least recently used.
    parse("z", tree(0));
    expect(parser.cached("y")).toBeUndefined();
    expect(parser.cached("x")).toEqual(tree(30_000));
    expect(parser.cached("depth")).toBeDefined();
    expect(parser.cached("z")).toEqual(tree(0));
  });

  it("charges a node-sparse text by its length", () => {
    const { parser, workers, collect } = setup();
    const parse = (text: string, result: ParseResult) => {
      parser.request({}, text, 250, collect(text));
      workers[0]?.reply(result);
    };
    parse("x", tree(1));
    // One long code block is a handful of nodes but holds the whole text twice over.
    const block = "a".repeat(100 * MAX_CACHED_NODES);
    parse(block, tree(4));
    expect(parser.cached("x")).toBeUndefined();
    expect(parser.cached(block)).toEqual(tree(4));

    const longer = `${block}a`;
    parse(longer, { kind: "plain", reason: "depth" });
    expect(parser.cached(longer)).toBeUndefined();
    expect(parser.cached(block)).toEqual(tree(4));
  });

  it("keeps only the newest of one owner's streamed prefixes", () => {
    const { parser, workers, collect } = setup();
    const streaming = {};
    const other = {};
    for (const text of ["a", "ab", "abc"]) {
      parser.request(streaming, text, 250, collect(text));
      workers[0]?.reply(tree(1));
    }
    expect(parser.cached("a")).toBeUndefined();
    expect(parser.cached("ab")).toBeUndefined();
    expect(parser.cached("abc")).toEqual(tree(1));

    // Another owner's prefix, and a text that does not extend the owner's last one, both stay.
    parser.request(other, "abcd", 250, collect("other"));
    workers[0]?.reply(tree(2));
    parser.request(streaming, "abcde", 250, collect("abcde"));
    workers[0]?.reply(tree(3));
    parser.request(streaming, "zzz", 250, collect("zzz"));
    workers[0]?.reply(tree(4));
    expect(parser.cached("abcd")).toEqual(tree(2));
    expect(parser.cached("abcde")).toEqual(tree(3));
    expect(parser.cached("zzz")).toEqual(tree(4));
    expect(parser.cached("abc")).toBeUndefined();
  });

  it("delivers but does not store a result larger than the whole cache", () => {
    const { parser, workers, seen, collect } = setup();
    parser.request({}, "small", 250, collect("small"));
    workers[0]?.reply(tree(10));
    parser.request({}, "huge", 250, collect("huge"));
    workers[0]?.reply(tree(MAX_CACHED_NODES + 1));
    expect(seen.at(-1)).toEqual(["huge", tree(MAX_CACHED_NODES + 1)]);
    expect(parser.cached("huge")).toBeUndefined();
    expect(parser.cached("small")).toEqual(tree(10));
  });
});
