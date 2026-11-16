/**
 * The steps that turn a reply's text blocks into one spoken turn: parse every block, wait for
 * the installed voices, pick the dominant language and a voice for it. `useReadAloudReply.start`
 * and hands-free both run them, so hands-free decides the same way whether a turn's reply is
 * speakable, without a reply component of its own; both parse through `readAloudParse`.
 *
 * `prepareReadAloud` can also prepare a Voicebox turn: Voicebox-sized chunks for the settled
 * language, plus today's system voice kept as `fallback` should Voicebox fail. `playVoicebox`
 * plays that turn over `startAudio`, whose own staleness handling decides "stale" before any
 * failure is considered, so a stop during the first request never reaches the fallback.
 */
import { MAX_MARKDOWN_BYTES } from "../components/Conversation/Markdown";
import {
  type MarkdownParser,
  SETTLED_DEADLINE_MS,
} from "../components/Conversation/markdownParser";
import { type ParseResult, parseMarkdown } from "../components/Conversation/markdownTree";
import type { ReadAloud } from "./readAloud";
import { pickVoice } from "./readAloud";
import { detectLanguage, speakable, speechChunks } from "./readAloudText";
import { sentenceChunks } from "./sentences";
import { VOICEBOX_LANGUAGES, type VoiceboxApi } from "./voiceboxApi";
import { createVoiceboxFeed, startAudio, type VoiceboxFeed } from "./voiceboxFeed";

export interface PreparedSpeech {
  lang: string;
  chunks: readonly string[];
  voice: SpeechSynthesisVoice;
  /** How many local voices were installed, for the "no voice for <language>" wording. */
  installed: number;
}

/** A Voicebox turn: its language, Voicebox-sized chunks, and today's system voice to fall back to
 * — null when none is installed for the language. */
export interface VoiceboxSpeech {
  lang: string;
  chunks: readonly string[];
  fallback: PreparedSpeech | null;
}

export type ReadAloudPrepareOutcome =
  | { kind: "speak"; speech: PreparedSpeech }
  | { kind: "voicebox"; speech: VoiceboxSpeech }
  | { kind: "unreadable" }
  | { kind: "nothing" }
  | { kind: "no_voice"; lang: string; installed: number };

export interface TextLike {
  text: string;
}

/** Not parsed, so not known to be free of Markdown syntax: refused, as `Markdown` renders it plain. */
const UNFORMATTED: ParseResult = { kind: "plain", reason: "error" };

/** Parses a block the way the column does: over `MAX_MARKDOWN_BYTES` it is refused unparsed; with
 * the window's worker parser it is a request with the settled deadline, its owner held in `owners`
 * until the answer comes so the caller can cancel it; with no parser (tests) it parses on the spot. */
export function readAloudParse(
  parser: MarkdownParser | null,
  owners: Set<object>,
): (text: string) => Promise<ParseResult> {
  return (text) => {
    if (new TextEncoder().encode(text).byteLength > MAX_MARKDOWN_BYTES) {
      return Promise.resolve(UNFORMATTED);
    }
    if (parser === null) return Promise.resolve(parseMarkdown(text));
    return new Promise((resolve) => {
      const owner = {};
      owners.add(owner);
      parser.request(owner, text, SETTLED_DEADLINE_MS, (result) => {
        owners.delete(owner);
        resolve(result);
      });
    });
  };
}

/** Parses every block (through the caller's own parser, so an already-rendered reply hits its
 * cache), waits for the installed voices, then settles on one language and voice for the whole
 * turn — the same choice `useReadAloudReply.start` makes.
 * `pick` chooses what of the parsed reply is read: all of it, or hands-free's `spokenAnswer`.
 * `voicebox`, when the settled language is in `languages` (the engine's spoken set — all of
 * `VOICEBOX_LANGUAGES` by default, or the pack's narrower list), prepares a Voicebox turn instead,
 * with today's system choice kept as its `fallback`; every other language, and `voicebox` left
 * false, take the system-voice path. */
export async function prepareReadAloud(
  blocks: readonly TextLike[],
  parse: (text: string) => Promise<ParseResult>,
  controller: ReadAloud,
  chosen: Readonly<Record<string, string>>,
  pick: typeof speakable = speakable,
  voicebox = false,
  languages: readonly string[] = VOICEBOX_LANGUAGES,
): Promise<ReadAloudPrepareOutcome> {
  const results = await Promise.all(blocks.map((block) => parse(block.text)));
  const voices = await controller.loadVoices();
  const reading = pick(results);
  if (reading === null) return { kind: "unreadable" };
  const lang =
    reading.text === "" ? null : (detectLanguage(reading.prose) ?? detectLanguage(reading.text));
  if (lang === null) return { kind: "nothing" };
  if (voicebox && languages.includes(lang)) {
    const chunks = sentenceChunks(reading.text, lang);
    if (chunks.length === 0) return { kind: "nothing" };
    const voice = pickVoice(lang, voices, chosen);
    const fallback =
      voice === null
        ? null
        : { lang, chunks: speechChunks(reading.text, lang), voice, installed: voices.length };
    return { kind: "voicebox", speech: { lang, chunks, fallback } };
  }
  const voice = pickVoice(lang, voices, chosen);
  if (voice === null) return { kind: "no_voice", lang, installed: voices.length };
  return {
    kind: "speak",
    speech: { lang, chunks: speechChunks(reading.text, lang), voice, installed: voices.length },
  };
}

/** `playVoicebox` needs `play` beside the three `startAudio` itself uses, for the fallback it may
 * drop to. */
type VoiceboxController = Pick<ReadAloud, "isCurrent" | "playAudio" | "subscribe" | "play">;

/** Plays a prepared Voicebox turn: every chunk pushed into a fresh feed up front, then `end()` and
 * `startAudio`, which `deadline` passes on to bound the first-chunk wait only. A stop during the
 * first request surfaces as `startAudio`'s own "stale" and is reported as-is — never as a reason to
 * fall back. Any other failure, including the deadline's own "voicebox_timeout", falls back to the
 * system voice when `speech.fallback` is set, else reports "failed"; both carry the failure's
 * code. */
export async function playVoicebox(
  controller: VoiceboxController,
  ticket: number,
  owner: string,
  speech: VoiceboxSpeech,
  api: VoiceboxApi,
  rate: number,
  deadline?: AbortSignal,
): Promise<{
  result: "playing" | "fallback" | "stale" | "failed";
  code: string | null;
  feed: VoiceboxFeed;
}> {
  const feed = createVoiceboxFeed((text, signal) => api.speech(text, speech.lang, signal));
  for (const chunk of speech.chunks) feed.push(chunk);
  feed.end();
  const outcome = await startAudio(controller, ticket, owner, feed, deadline);
  if (outcome === "playing" || outcome === "stale") return { result: outcome, code: null, feed };
  const code = outcome.failed;
  if (speech.fallback !== null) {
    controller.play(ticket, owner, speech.fallback.chunks, speech.fallback.voice, rate);
    return { result: "fallback", code, feed };
  }
  return { result: "failed", code, feed };
}
