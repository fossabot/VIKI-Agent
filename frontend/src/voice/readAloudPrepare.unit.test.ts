/**
 * `readAloudParse`, the one way Read and hands-free parse a reply before speaking it: refused
 * unparsed over the byte cap, through the window's worker with the settled deadline, or on the spot
 * with no worker. `prepareReadAloud`'s voicebox branch and `playVoicebox`'s own stale/fallback
 * handling are below that.
 */
import { expect, test, vi } from "vitest";
import { MAX_MARKDOWN_BYTES } from "../components/Conversation/Markdown";
import {
  type MarkdownParser,
  SETTLED_DEADLINE_MS,
} from "../components/Conversation/markdownParser";
import type { ParseResult } from "../components/Conversation/markdownTree";
import type { ReadAloud } from "./readAloud";
import {
  type PreparedSpeech,
  playVoicebox,
  prepareReadAloud,
  readAloudParse,
  type VoiceboxSpeech,
} from "./readAloudPrepare";
import { speechChunks } from "./readAloudText";
import { sentenceChunks } from "./sentences";
import { type VoiceboxApi, VoiceboxError } from "./voiceboxApi";

function fakeParser() {
  const requests: { owner: object; deadline: number; onResult: (r: ParseResult) => void }[] = [];
  const parser: MarkdownParser = {
    cached: () => undefined,
    request: (owner, _text, deadline, onResult) => requests.push({ owner, deadline, onResult }),
    cancel: vi.fn(),
  };
  return { parser, requests };
}

test("a block over the byte cap is refused unparsed, without asking the worker", async () => {
  const { parser, requests } = fakeParser();
  const parse = readAloudParse(parser, new Set());
  await expect(parse("a ".repeat(MAX_MARKDOWN_BYTES / 2 + 1))).resolves.toEqual({
    kind: "plain",
    reason: "error",
  });
  expect(requests).toHaveLength(0);
});

test("the worker is asked with the settled deadline, its owner held until it answers", async () => {
  const { parser, requests } = fakeParser();
  const owners = new Set<object>();
  const result = readAloudParse(parser, owners)("Hello there.");
  expect(requests).toHaveLength(1);
  expect(requests[0]?.deadline).toBe(SETTLED_DEADLINE_MS);
  expect(owners.has(requests[0]?.owner as object)).toBe(true);
  const answer: ParseResult = { kind: "plain", reason: "error" };
  requests[0]?.onResult(answer);
  await expect(result).resolves.toBe(answer);
  expect(owners.size).toBe(0);
});

test("with no worker it parses on the spot", async () => {
  const result = await readAloudParse(null, new Set())("Hello **there**.");
  expect(result.kind).toBe("tree");
});

// ---- prepareReadAloud's voicebox branch ----

function voice(name: string, lang: string): SpeechSynthesisVoice {
  return { name, voiceURI: name, lang, localService: true, default: false };
}

const ZIRA = voice("Zira", "en-US");
const HARUKA = voice("Haruka", "ja-JP");
const KASEM = voice("Kasem", "th-TH");

function fakeController(voices: SpeechSynthesisVoice[]): ReadAloud {
  return { loadVoices: async () => voices } as unknown as ReadAloud;
}

/** Ignores the parsed results and hands back fixed text, so a test drives the settled language
 * without building a real Markdown tree. */
function pickText(
  text: string,
): (results: readonly ParseResult[]) => { text: string; prose: string } {
  return () => ({ text, prose: text });
}

const parse = async (): Promise<ParseResult> => ({ kind: "plain", reason: "error" });

test("prepares a voicebox outcome for Japanese", async () => {
  const text = "こんにちは。今日はいい天気ですね。";
  const outcome = await prepareReadAloud(
    [{ text: "irrelevant" }],
    parse,
    fakeController([ZIRA]),
    {},
    pickText(text),
    true,
  );
  expect(outcome).toEqual({
    kind: "voicebox",
    speech: { lang: "ja", chunks: sentenceChunks(text, "ja"), fallback: null },
  });
});

test("keeps an installed system voice as the voicebox fallback", async () => {
  const text = "Hello there. The weather is nice today.";
  const outcome = await prepareReadAloud(
    [{ text: "irrelevant" }],
    parse,
    fakeController([ZIRA]),
    {},
    pickText(text),
    true,
  );
  expect(outcome).toEqual({
    kind: "voicebox",
    speech: {
      lang: "en",
      chunks: sentenceChunks(text, "en"),
      fallback: { lang: "en", chunks: speechChunks(text, "en"), voice: ZIRA, installed: 1 },
    },
  });
});

test("falls through to the system-voice path for a language voicebox doesn't speak", async () => {
  const text = "สวัสดีครับ วันนี้อากาศดี";
  const outcome = await prepareReadAloud(
    [{ text: "irrelevant" }],
    parse,
    fakeController([KASEM]),
    {},
    pickText(text),
    true,
  );
  expect(outcome).toEqual({
    kind: "speak",
    speech: { lang: "th", chunks: speechChunks(text, "th"), voice: KASEM, installed: 1 },
  });
});

test("the languages gate narrows which language gets a voicebox outcome", async () => {
  const text = "こんにちは。今日はいい天気ですね。";
  const outcome = await prepareReadAloud(
    [{ text: "irrelevant" }],
    parse,
    fakeController([HARUKA]),
    {},
    pickText(text),
    true,
    ["en"],
  );
  expect(outcome).toEqual({
    kind: "speak",
    speech: { lang: "ja", chunks: speechChunks(text, "ja"), voice: HARUKA, installed: 1 },
  });
});

test("keeps the system-voice path when voicebox is not requested", async () => {
  const text = "こんにちは。今日はいい天気ですね。";
  const outcome = await prepareReadAloud(
    [{ text: "irrelevant" }],
    parse,
    fakeController([HARUKA]),
    {},
    pickText(text),
  );
  expect(outcome).toEqual({
    kind: "speak",
    speech: { lang: "ja", chunks: speechChunks(text, "ja"), voice: HARUKA, installed: 1 },
  });
});

// ---- playVoicebox ----

async function flush(): Promise<void> {
  for (let turn = 0; turn < 10; turn += 1) await Promise.resolve();
}

type VoiceboxController = Pick<ReadAloud, "isCurrent" | "playAudio" | "subscribe" | "play">;

function fakeVoiceboxController() {
  let current = 0;
  const listeners = new Set<() => void>();
  const playCalls: {
    ticket: number;
    owner: string;
    chunks: readonly string[];
    voice: SpeechSynthesisVoice;
    rate: number;
  }[] = [];
  const controller: VoiceboxController = {
    isCurrent: (ticket) => ticket === current,
    playAudio: (ticket) => ticket === current,
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    play: (ticket, owner, chunks, voiceArg, rate) => {
      playCalls.push({ ticket, owner, chunks, voice: voiceArg, rate });
      return true;
    },
  };
  return {
    controller,
    playCalls,
    begin: (ticket: number) => {
      current = ticket;
    },
    stop: () => {
      current = -1;
      for (const listener of [...listeners]) listener();
    },
  };
}

test("playVoicebox plays the prepared feed and reports playing", async () => {
  const t = fakeVoiceboxController();
  t.begin(1);
  const calls: { text: string; language: string }[] = [];
  const api = {
    speech: async (text: string, language: string) => {
      calls.push({ text, language });
      return new ArrayBuffer(4);
    },
  } as unknown as VoiceboxApi;
  const speech: VoiceboxSpeech = { lang: "ja", chunks: ["a", "b"], fallback: null };

  const result = await playVoicebox(t.controller, 1, "owner", speech, api, 1);

  expect(result.result).toBe("playing");
  expect(result.code).toBeNull();
  expect(typeof result.feed.next).toBe("function");
  expect(calls).toEqual([{ text: "a", language: "ja" }]);
  expect(t.playCalls).toHaveLength(0);
});

test("playVoicebox falls back to the system voice on voicebox_unreachable", async () => {
  const t = fakeVoiceboxController();
  t.begin(1);
  const api = {
    speech: async () => {
      throw new VoiceboxError("voicebox_unreachable");
    },
  } as unknown as VoiceboxApi;
  const fallback: PreparedSpeech = {
    lang: "ja",
    chunks: ["フォールバック"],
    voice: HARUKA,
    installed: 1,
  };
  const speech: VoiceboxSpeech = { lang: "ja", chunks: ["a"], fallback };

  const result = await playVoicebox(t.controller, 1, "owner", speech, api, 1.1);

  expect(result.result).toBe("fallback");
  expect(result.code).toBe("voicebox_unreachable");
  expect(t.playCalls).toEqual([
    { ticket: 1, owner: "owner", chunks: fallback.chunks, voice: HARUKA, rate: 1.1 },
  ]);
});

test("playVoicebox reports failed, with no fallback, when none is installed", async () => {
  const t = fakeVoiceboxController();
  t.begin(1);
  const api = {
    speech: async () => {
      throw new VoiceboxError("voicebox_busy");
    },
  } as unknown as VoiceboxApi;
  const speech: VoiceboxSpeech = { lang: "ja", chunks: ["a"], fallback: null };

  const result = await playVoicebox(t.controller, 1, "owner", speech, api, 1);

  expect(result.result).toBe("failed");
  expect(result.code).toBe("voicebox_busy");
  expect(t.playCalls).toHaveLength(0);
});

test("playVoicebox reports stale, never a fallback, when stop() fires mid-request", async () => {
  const t = fakeVoiceboxController();
  t.begin(1);
  const api = {
    speech: (_text: string, _language: string, signal: AbortSignal) =>
      new Promise<ArrayBuffer>((_resolve, reject) => {
        signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
      }),
  } as unknown as VoiceboxApi;
  const fallback: PreparedSpeech = {
    lang: "ja",
    chunks: ["フォールバック"],
    voice: HARUKA,
    installed: 1,
  };
  const speech: VoiceboxSpeech = { lang: "ja", chunks: ["a"], fallback };

  const resultP = playVoicebox(t.controller, 1, "owner", speech, api, 1);
  await flush();
  t.stop();
  const result = await resultP;

  expect(result.result).toBe("stale");
  expect(result.code).toBeNull();
  expect(t.playCalls).toHaveLength(0);
});

test("playVoicebox falls back on the deadline", async () => {
  const t = fakeVoiceboxController();
  t.begin(1);
  const api = {
    speech: (_text: string, _language: string, signal: AbortSignal) =>
      new Promise<ArrayBuffer>((_resolve, reject) => {
        signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
      }),
  } as unknown as VoiceboxApi;
  const fallback: PreparedSpeech = {
    lang: "ja",
    chunks: ["フォールバック"],
    voice: HARUKA,
    installed: 1,
  };
  const speech: VoiceboxSpeech = { lang: "ja", chunks: ["a"], fallback };
  const deadline = new AbortController();

  const resultP = playVoicebox(t.controller, 1, "owner", speech, api, 1, deadline.signal);
  await flush();
  deadline.abort();
  const result = await resultP;

  expect(result.result).toBe("fallback");
  expect(result.code).toBe("voicebox_timeout");
  expect(t.playCalls).toEqual([
    { ticket: 1, owner: "owner", chunks: fallback.chunks, voice: HARUKA, rate: 1 },
  ]);
});
