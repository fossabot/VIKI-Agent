/**
 * Hands-free's short spoken answer. Each automatic Send carries a fixed
 * request after the words heard, asking for a 1–3 sentence plain answer as the reply's first
 * paragraph; the request stays in the message and its saved transcript, and the user turn shows
 * a "voice" tag in its place. The loop speaks only that paragraph, cleaned and capped, or the
 * reply's first sentences when it is empty — one rule whether or not the reply honoured the
 * request. Marker-free on purpose: nothing odd ever renders in the chat.
 */
import type { ParseResult } from "../components/Conversation/markdownTree";
import { EXCERPT_LABEL } from "../context/excerptBlocks";
import { detectLanguage, type ReadAloudLanguage, speakable, speechChunks } from "./readAloudText";

export const SPOKEN_ANSWER_REQUEST =
  "[voice] Start your final reply with a spoken answer as its own first paragraph: one to three " +
  "plain, conversational sentences, with no Markdown, URLs, emoji, code or tables. Put any " +
  "detail after it.";

const TAIL = `\n\n${SPOKEN_ANSWER_REQUEST}`;

export function withSpokenAnswerRequest(heard: string): string {
  return `${heard}${TAIL}`;
}

/** Whether `rest` is only the vault excerpt blocks `session.py`'s `joined_text` appends after a
 * turn's text (one `"\n"` before each block's own label line, `src/context/excerptBlocks.ts`'s `EXCERPT_LABEL`). */
function isExcerptBlocks(rest: string): boolean {
  if (!rest.startsWith("\n")) return false;
  const labelEnd = rest.indexOf("\n", 1);
  const label = labelEnd === -1 ? rest.slice(1) : rest.slice(1, labelEnd);
  return EXCERPT_LABEL.test(label);
}

/** The words heard, or null when the text does not carry the request after a blank line. An
 * automatic send with vault excerpts attached has the request followed by `joined_text`'s excerpt
 * blocks rather than ending the text, so the request is found there too and stripped, the blocks
 * kept; a tail merely mentioned mid-text, or followed by anything else, is left alone. */
export function withoutSpokenAnswerRequest(text: string): string | null {
  if (text.endsWith(TAIL)) return text.slice(0, -TAIL.length);
  const at = text.indexOf(TAIL);
  if (at === -1) return null;
  const rest = text.slice(at + TAIL.length);
  return isExcerptBlocks(rest) ? text.slice(0, at) + rest : null;
}

export const MAX_SPOKEN_SENTENCES = 3;
export const MAX_SPOKEN_CHARS = 350;

const EMOJI =
  /\p{Extended_Pictographic}|\p{Emoji_Modifier}|\p{Regional_Indicator}|\uFE0F|\u200D|\u20E3/gu;

/** The first top-level paragraph of the first block that has one, as a parse result of its own. */
function firstParagraph(results: readonly ParseResult[]): ParseResult | null {
  for (const result of results) {
    if (result.kind !== "tree") continue;
    const paragraph = result.tree.children.find(
      (node) => node.type === "element" && node.tagName === "p",
    );
    if (paragraph) return { kind: "tree", tree: { type: "root", children: [paragraph] }, nodes: 0 };
  }
  return null;
}

export function cleanLines(text: string): string[] {
  return text
    .split("\n")
    .map((line) => line.replace(EMOJI, "").replace(/\s+/g, " ").trim())
    .filter((line) => line !== "");
}

/** Up to three sentences within the length cap, each line ending one; a first sentence over the
 * cap is cut where `speechChunks` cuts a long line. */
function capped(lines: readonly string[], lang: ReadAloudLanguage): string {
  const segmenter = new Intl.Segmenter(lang, { granularity: "sentence" });
  const sentences = lines.flatMap((line) =>
    [...segmenter.segment(line)].map(({ segment }) => segment.trim()).filter(Boolean),
  );
  let out = "";
  for (const sentence of sentences.slice(0, MAX_SPOKEN_SENTENCES)) {
    const next = out === "" ? sentence : `${out} ${sentence}`;
    if (next.length > MAX_SPOKEN_CHARS) break;
    out = next;
  }
  if (out === "" && sentences[0]) {
    return speechChunks(sentences[0], lang, MAX_SPOKEN_CHARS)[0] ?? "";
  }
  return out;
}

/** What hands-free speaks of a reply, in `speakable`'s shape (null when a block rendered plain):
 * its first paragraph, else its first sentences, without Markdown, URLs or emoji. */
export function spokenAnswer(
  results: readonly ParseResult[],
): { text: string; prose: string } | null {
  const whole = speakable(results);
  if (whole === null) return null;
  const paragraph = firstParagraph(results);
  const first = paragraph ? speakable([paragraph]) : null;
  const head = first ? cleanLines(first.text).join(" ") : "";
  const source = head !== "" && first ? first : whole;
  const lines = head !== "" ? [head] : cleanLines(whole.text);
  return { text: capped(lines, detectLanguage(source.prose) ?? "en"), prose: source.prose };
}
