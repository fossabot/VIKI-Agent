/**
 * The main-thread client of the Markdown worker. One job runs in the worker at a time, each
 * under a deadline; a worker that overruns is terminated and the next job gets a fresh one.
 * Results are cached by text, bounded by a cost that counts both nodes and characters.
 *
 * Streaming replies ask again on every delta, so a newer text from the same owner replaces that
 * owner's queued job and only the newest text is parsed. A job already in the worker is never
 * replaced: the worker cannot be interrupted short of terminating it.
 */
import { createContext } from "react";
import type { ParseResult } from "./markdownTree";

/** While a reply streams, a slow parse must not hold newer text back for long. */
export const STREAMING_DEADLINE_MS = 250;
/** Once a reply has settled, a large legitimate table gets time to format. */
export const SETTLED_DEADLINE_MS = 2000;
/** The cache's cost bound. A result costs its node count or one per 100 characters of its text,
 * whichever is more, so neither a node-heavy tree (roughly 411 B per position-stripped node) nor a
 * node-sparse one (a long code block holds its text in the key and again in one text node) can
 * retain much more than about 20 MiB. */
export const MAX_CACHED_NODES = 50_000;
/** Worker failures in a row, with no success between, before the worker is given up. */
export const MAX_SPAWN_FAILURES = 3;

export interface ParseRequest {
  id: number;
  text: string;
}

export interface ParseResponse {
  id: number;
  result: ParseResult;
}

export interface WorkerLike {
  postMessage(message: ParseRequest): void;
  terminate(): void;
  onmessage: ((event: MessageEvent<ParseResponse>) => void) | null;
  onerror: ((event: ErrorEvent) => void) | null;
  onmessageerror: ((event: MessageEvent) => void) | null;
}

export interface MarkdownParser {
  cached(text: string): ParseResult | undefined;
  request(
    owner: object,
    text: string,
    deadlineMs: number,
    onResult: (result: ParseResult) => void,
  ): void;
  cancel(owner: object): void;
}

/** Null means parse synchronously: tests, and any tree rendered without a provider. */
export const MarkdownParserContext = createContext<MarkdownParser | null>(null);

interface Job {
  owner: object;
  text: string;
  deadlineMs: number;
  onResult: (result: ParseResult) => void;
}

interface Running {
  job: Job;
  id: number;
  timer: number;
  silenced: boolean;
}

const TIMEOUT: ParseResult = { kind: "plain", reason: "timeout" };
const ERROR: ParseResult = { kind: "plain", reason: "error" };
const UNAVAILABLE: ParseResult = { kind: "plain", reason: "unavailable" };

function costOf(text: string, result: ParseResult): number {
  const nodes = result.kind === "tree" ? result.nodes : 0;
  return Math.max(1, nodes, Math.ceil(text.length / 100));
}

export function createWorkerParser(deps: {
  spawn: () => WorkerLike;
  setTimer: (callback: () => void, ms: number) => number;
  clearTimer: (handle: number) => void;
}): MarkdownParser {
  // A Map iterates in insertion order, so re-inserting on a hit keeps it in recency order.
  const cache = new Map<string, { result: ParseResult; cost: number }>();
  let cachedCost = 0;
  // Each owner's last stored text: a streamed reply asks for every prefix of itself, and only the
  // newest can ever be shown again.
  const lastStored = new WeakMap<object, string>();
  const queue: Job[] = [];
  let worker: WorkerLike | null = null;
  let running: Running | null = null;
  let failures = 0;
  let nextId = 1;

  function cached(text: string): ParseResult | undefined {
    const entry = cache.get(text);
    if (!entry) return undefined;
    cache.delete(text);
    cache.set(text, entry);
    return entry.result;
  }

  function forget(text: string): void {
    const old = cache.get(text);
    if (!old) return;
    cache.delete(text);
    cachedCost -= old.cost;
  }

  /** A timeout or an unavailable worker says nothing about the text, so neither is kept. */
  function store(owner: object, text: string, result: ParseResult): void {
    if (
      result.kind === "plain" &&
      (result.reason === "timeout" || result.reason === "unavailable")
    ) {
      return;
    }
    const cost = costOf(text, result);
    if (cost > MAX_CACHED_NODES) return;
    const previous = lastStored.get(owner);
    if (previous !== undefined && previous !== text && text.startsWith(previous)) forget(previous);
    lastStored.set(owner, text);
    forget(text);
    cache.set(text, { result, cost });
    cachedCost += cost;
    for (const [key, entry] of cache) {
      if (cachedCost <= MAX_CACHED_NODES) break;
      cache.delete(key);
      cachedCost -= entry.cost;
    }
  }

  function dropWorker(): void {
    worker?.terminate();
    worker = null;
  }

  function finish(result: ParseResult): void {
    const current = running;
    if (!current) return;
    running = null;
    deps.clearTimer(current.timer);
    try {
      if (!current.silenced) current.job.onResult(result);
    } finally {
      pump();
    }
  }

  function attach(w: WorkerLike): void {
    w.onmessage = (event) => {
      // A dropped worker or a stale id is an answer nobody is waiting for any more.
      if (w !== worker || !running || event.data.id !== running.id) return;
      store(running.job.owner, running.job.text, event.data.result);
      failures = 0;
      finish(event.data.result);
    };
    const fail = () => {
      if (w !== worker) return;
      dropWorker();
      failures += 1;
      finish(ERROR);
    };
    w.onerror = fail;
    w.onmessageerror = fail;
  }

  function expire(id: number): void {
    if (running?.id !== id) return;
    dropWorker();
    finish(TIMEOUT);
  }

  function pump(): void {
    while (!running) {
      const job = queue.shift();
      if (!job) return;
      const hit = cached(job.text);
      if (hit) {
        job.onResult(hit);
        continue;
      }
      if (failures >= MAX_SPAWN_FAILURES) {
        job.onResult(UNAVAILABLE);
        continue;
      }
      if (!worker) {
        try {
          worker = deps.spawn();
        } catch {
          failures += 1;
          job.onResult(UNAVAILABLE);
          continue;
        }
        attach(worker);
      }
      const id = nextId++;
      worker.postMessage({ id, text: job.text });
      running = {
        job,
        id,
        timer: deps.setTimer(() => expire(id), job.deadlineMs),
        silenced: false,
      };
    }
  }

  function dropQueued(owner: object): void {
    const index = queue.findIndex((job) => job.owner === owner);
    if (index >= 0) queue.splice(index, 1);
  }

  return {
    cached,
    request(owner, text, deadlineMs, onResult) {
      const hit = cached(text);
      if (hit) {
        // The owner has moved on from whatever it had queued.
        dropQueued(owner);
        onResult(hit);
        return;
      }
      const queued = queue.find((job) => job.owner === owner);
      if (queued) {
        queued.text = text;
        queued.deadlineMs = deadlineMs;
        queued.onResult = onResult;
      } else {
        queue.push({ owner, text, deadlineMs, onResult });
      }
      pump();
    },
    cancel(owner) {
      dropQueued(owner);
      if (running?.job.owner === owner) running.silenced = true;
    },
  };
}
