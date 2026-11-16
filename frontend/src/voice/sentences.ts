/**
 * Cuts reply text into the sentence-sized pieces Voicebox speaks: `sentenceChunks` for a block
 * that is already final, `createSpokenStream` for the first paragraph of each text stretch while
 * a reply is still streaming in.
 */
import type { HastNode, HastRoot, ParseResult } from "../components/Conversation/markdownTree";
import { parseMarkdown } from "../components/Conversation/markdownTree";
import type { Block } from "../state/view";
import { detectLanguage, type ReadAloudLanguage, speakable, speechChunks } from "./readAloudText";
import { cleanLines, MAX_SPOKEN_CHARS, MAX_SPOKEN_SENTENCES } from "./spokenAnswer";

export const MIN_CHUNK_CHARS = 20;
export const MIN_CJK_CHUNK_CHARS = 6;
export const MAX_VOICEBOX_CHUNK_CHARS = 300;

/** Japanese and Chinese run sentences together with no space; Korean, like the rest, uses one. */
function joinerFor(lang: ReadAloudLanguage): string {
  return lang === "ja" || lang === "zh" ? "" : " ";
}

function minimumFor(lang: ReadAloudLanguage): number {
  return lang === "ja" || lang === "zh" || lang === "ko" ? MIN_CJK_CHUNK_CHARS : MIN_CHUNK_CHARS;
}

function segmentSentences(text: string, lang: ReadAloudLanguage): string[] {
  const segmenter = new Intl.Segmenter(lang, { granularity: "sentence" });
  return [...segmenter.segment(text)].map(({ segment }) => segment.trim()).filter(Boolean);
}

/** Buffers sentence-sized units into Voicebox chunks: never grown past MAX_VOICEBOX_CHUNK_CHARS,
 * emitted as soon as the per-language minimum is reached; `flush` empties what is left. */
function createPacker(lang: ReadAloudLanguage) {
  const joiner = joinerFor(lang);
  const minimum = minimumFor(lang);
  const out: string[] = [];
  let carry = "";

  function add(unit: string): void {
    if (carry !== "" && carry.length + joiner.length + unit.length > MAX_VOICEBOX_CHUNK_CHARS) {
      out.push(carry);
      carry = "";
    }
    carry = carry === "" ? unit : `${carry}${joiner}${unit}`;
    if (carry.length >= minimum) {
      out.push(carry);
      carry = "";
    }
  }

  return {
    out,
    /** A sentence of any length; one over the cap is cut first with speechChunks. */
    push(sentence: string): void {
      if (sentence.length > MAX_VOICEBOX_CHUNK_CHARS) {
        for (const piece of speechChunks(sentence, lang, MAX_VOICEBOX_CHUNK_CHARS)) add(piece);
      } else {
        add(sentence);
      }
    },
    flush(): void {
      if (carry !== "") {
        out.push(carry);
        carry = "";
      }
    },
  };
}

/** A finished block's text, packed into Voicebox-sized chunks. Each non-empty line packs and
 * flushes on its own, as readAloudText's speechChunks also treats lines independently. */
export function sentenceChunks(text: string, lang: ReadAloudLanguage): string[] {
  const out: string[] = [];
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const packer = createPacker(lang);
    for (const sentence of segmentSentences(line, lang)) packer.push(sentence);
    packer.flush();
    out.push(...packer.out);
  }
  return out;
}

// ---- streaming ----

const TERMINATOR_OR_BREAK = /[.!?。！？\n]/;
const MAX_PARSE_CHARS = 4096;

function isBlank(node: HastNode): boolean {
  return node.type === "text" && String(node.value ?? "").trim() === "";
}

/** The first top-level `<p>` in a parsed root, and whether it is done changing: its stretch
 * closed, or a non-blank node (another block starting) already follows it. */
function firstParagraph(
  tree: HastRoot,
  stretchClosed: boolean,
): { node: HastNode; closed: boolean } | null {
  const children = tree.children;
  const index = children.findIndex((node) => node.type === "element" && node.tagName === "p");
  if (index === -1) return null;
  const closed = stretchClosed || children.slice(index + 1).some((node) => !isBlank(node));
  return { node: children[index] as HastNode, closed };
}

interface StretchState {
  closed: boolean;
  /** Capped, exhausted or closed-and-flushed: nothing more will ever come from this stretch. */
  done: boolean;
  lastParseLen: number;
  /** Sentences already handed to the packer; keeps a resegmented prefix from repeating. */
  cursor: number;
  committedChars: number;
  packer: ReturnType<typeof createPacker> | null;
  emitted: number;
}

function newStretch(): StretchState {
  return {
    closed: false,
    done: false,
    lastParseLen: 0,
    cursor: 0,
    committedChars: 0,
    packer: null,
    emitted: 0,
  };
}

function processStretch(
  state: StretchState,
  text: string,
  closed: boolean,
  parse: (text: string) => ParseResult,
): void {
  state.closed = closed;
  if (state.done) return;

  const added = text.slice(state.lastParseLen);
  if (!state.closed && !TERMINATOR_OR_BREAK.test(added)) return;

  const result = parse(text.slice(0, MAX_PARSE_CHARS));
  state.lastParseLen = text.length;
  if (result.kind === "plain") {
    if (state.closed) state.done = true;
    return;
  }

  const found = firstParagraph(result.tree, state.closed);
  if (!found) {
    if (state.closed) state.done = true;
    return;
  }

  const read = speakable([
    { kind: "tree", tree: { type: "root", children: [found.node] }, nodes: 0 },
  ]);
  const headText = read ? cleanLines(read.text).join(" ") : "";
  const lang = detectLanguage(headText) ?? "en";
  const all = segmentSentences(headText, lang);
  const complete = found.closed ? all : all.slice(0, -1);

  const fresh = complete.slice(state.cursor);
  if (fresh.length === 0) {
    if (found.closed && state.cursor >= complete.length) {
      state.packer?.flush();
      state.done = true;
    }
    return;
  }

  if (!state.packer) state.packer = createPacker(lang);
  const packer = state.packer;
  const joiner = joinerFor(lang);
  let hitCap = false;
  for (const sentence of fresh) {
    if (state.cursor === 0 && sentence.length > MAX_SPOKEN_CHARS) {
      const cut = speechChunks(sentence, lang, MAX_SPOKEN_CHARS)[0] ?? "";
      if (cut) packer.push(cut);
      state.cursor += 1;
      hitCap = true;
      break;
    }
    const next =
      state.committedChars === 0
        ? sentence.length
        : state.committedChars + joiner.length + sentence.length;
    if (state.cursor >= MAX_SPOKEN_SENTENCES || next > MAX_SPOKEN_CHARS) {
      hitCap = true;
      break;
    }
    packer.push(sentence);
    state.committedChars = next;
    state.cursor += 1;
    if (state.cursor >= MAX_SPOKEN_SENTENCES) {
      hitCap = true;
      break;
    }
  }

  const exhausted = found.closed && state.cursor >= complete.length;
  if (hitCap || exhausted || state.closed) {
    packer.flush();
    state.done = true;
  }
}

/** Tracks the spoken-text stream of one reply as its blocks grow. Each call hands back only the
 * chunks produced since the last call. */
export function createSpokenStream(parse: (text: string) => ParseResult = parseMarkdown) {
  const stretches: StretchState[] = [];

  function stretchTexts(blocks: readonly Block[]): string[] {
    const texts: string[] = [];
    let parts: string[] = [];
    for (const block of blocks) {
      if (block.kind === "tools") {
        texts.push(parts.join("\n\n"));
        parts = [];
        continue;
      }
      if (block.kind === "text") parts.push(block.text);
    }
    texts.push(parts.join("\n\n"));
    return texts;
  }

  return {
    update(blocks: readonly Block[], done: boolean): { text: string; group: number }[] {
      const out: { text: string; group: number }[] = [];
      const texts = stretchTexts(blocks);
      for (let group = 0; group < texts.length; group++) {
        while (stretches.length <= group) stretches.push(newStretch());
        const state = stretches[group] as StretchState;
        if (state.done) continue;
        const closed = group < texts.length - 1 || done;
        processStretch(state, texts[group] as string, closed, parse);
        if (state.packer) {
          const fresh = state.packer.out.slice(state.emitted);
          state.emitted = state.packer.out.length;
          for (const text of fresh) out.push({ text, group });
        }
      }
      return out;
    },
  };
}
