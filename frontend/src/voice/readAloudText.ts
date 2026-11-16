/**
 * What a reply reads aloud, in which language, and in which pieces. Pure: it takes the app's own
 * Markdown parse results (the hast trees the column already built) and touches no DOM.
 */
import type { HastNode, ParseResult } from "../components/Conversation/markdownTree";

export const READ_ALOUD_LANGUAGES = [
  "en",
  "zh",
  "hi",
  "es",
  "ar",
  "fr",
  "bn",
  "pt",
  "id",
  "ur",
  "ru",
  "de",
  "ja",
  "vi",
  "te",
  "tr",
  "ta",
  "fa",
  "ko",
  "am",
  "th",
  "it",
  "gu",
  "kn",
] as const;
export type ReadAloudLanguage = (typeof READ_ALOUD_LANGUAGES)[number];

export const MAX_CHUNK_CHARS = 1000;

/** Never read: code blocks, images, task-list boxes and footnote references. */
const SKIPPED_TAGS = new Set(["pre", "img", "input", "sup"]);

const BLOCK_TAGS = new Set(
  "p h1 h2 h3 h4 h5 h6 li blockquote ul ol table thead tbody tr div dl dt dd section details summary".split(
    " ",
  ),
);

interface Reading {
  /** Everything read, inline code included. */
  text: string;
  /** The same without inline code: the sample for language detection. */
  prose: string;
}

function emit(out: Reading, value: string, prose: boolean): void {
  out.text += value;
  if (prose) out.prose += value;
}

function propertiesOf(node: HastNode): Record<string, unknown> {
  return (node.properties as Record<string, unknown> | undefined) ?? {};
}

function walkChildren(node: HastNode, out: Reading, prose: boolean): void {
  for (const child of node.children ?? []) walk(child, out, prose);
}

/** Recursion is bounded: parseMarkdown refuses a tree nested past MAX_MARKDOWN_DEPTH. */
function walk(node: HastNode, out: Reading, prose: boolean): void {
  if (node.type === "text") {
    // Markdown renders a newline inside text as a space; a line break here would split a sentence.
    emit(out, String(node.value).replace(/\r\n|\n|\r/g, " "), prose);
    return;
  }
  if (node.type !== "element") {
    walkChildren(node, out, prose);
    return;
  }
  const tag = String(node.tagName);
  const properties = propertiesOf(node);
  if (
    SKIPPED_TAGS.has(tag) ||
    (tag === "section" && properties.dataFootnotes) ||
    (tag === "a" && properties.dataFootnoteBackref)
  ) {
    return;
  }
  if (tag === "br" || tag === "hr") {
    emit(out, "\n", true);
    return;
  }
  if (tag === "code") {
    walkChildren(node, out, false);
    return;
  }
  if (tag === "a") {
    const link: Reading = { text: "", prose: "" };
    walkChildren(node, link, prose);
    const href = typeof properties.href === "string" ? properties.href : undefined;
    const shown = link.text.trim();
    // A link whose text is its own address says nothing a listener can use.
    if (href !== undefined && (shown === href || shown === href.replace(/^mailto:/i, ""))) return;
    out.text += link.text;
    out.prose += link.prose;
    return;
  }
  if (tag === "tr") {
    emit(out, "\n", true);
    let first = true;
    for (const cell of node.children ?? []) {
      if (cell.type !== "element") continue;
      if (!first) emit(out, ", ", true);
      first = false;
      walkChildren(cell, out, prose);
    }
    emit(out, "\n", true);
    return;
  }
  const block = BLOCK_TAGS.has(tag);
  if (block) emit(out, "\n", true);
  walkChildren(node, out, prose);
  if (block) emit(out, "\n", true);
}

/** Printable ASCII only, so Japanese right after an address stays. The scheme is capped at 32
 * characters: an uncapped run of `a.a.a.` would rescan to its end from every dot. */
const REMOVED = [
  /\b[a-z][a-z0-9+.-]{0,31}:\/\/[!-~]+/gi,
  /\bmailto:[!-~]+/gi,
  /\bwww\.[!-~]+/gi,
  // The redactor's token shape (src/viki_agent/app/diagnostics.py `_LONG_TOKEN_RE`): keys and hashes are not read.
  /[A-Za-z0-9_-]{32,}/g,
];

function tidy(raw: string): string {
  let value = raw;
  for (const pattern of REMOVED) value = value.replace(pattern, "");
  return value
    .replace(/[ \t]+/g, " ")
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line !== "")
    .join("\n");
}

/** What to read and the language sample, or null when a block rendered plain (its text is not
 * known to be free of Markdown syntax). Both are "" when nothing is left to read. */
export function speakable(results: readonly ParseResult[]): { text: string; prose: string } | null {
  if (results.some((result) => result.kind === "plain")) return null;
  const out: Reading = { text: "", prose: "" };
  for (const result of results) {
    if (result.kind !== "tree") continue;
    walk(result.tree, out, true);
    emit(out, "\n\n", true);
  }
  return { text: tidy(out.text), prose: tidy(out.prose) };
}

function count(text: string, pattern: RegExp): number {
  return text.match(pattern)?.length ?? 0;
}

/** Script, its weight (a CJK or Hangul character carries more than a letter), the pattern. In
 * tie order. */
const NON_LATIN_SCRIPTS: readonly [string, number, RegExp][] = [
  ["cjk", 3, /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}]/gu],
  ["hangul", 3, /\p{Script=Hangul}/gu],
  ["thai", 1, /\p{Script=Thai}/gu],
  ["arabic", 1, /\p{Script=Arabic}/gu],
  ["devanagari", 1, /\p{Script=Devanagari}/gu],
  ["bengali", 1, /\p{Script=Bengali}/gu],
  ["tamil", 1, /\p{Script=Tamil}/gu],
  ["telugu", 1, /\p{Script=Telugu}/gu],
  ["gujarati", 1, /\p{Script=Gujarati}/gu],
  ["kannada", 1, /\p{Script=Kannada}/gu],
  ["ethiopic", 1, /\p{Script=Ethiopic}/gu],
  ["cyrillic", 1, /\p{Script=Cyrillic}/gu],
];

const LANGUAGE_OF_SCRIPT: Record<string, ReadAloudLanguage> = {
  hangul: "ko",
  thai: "th",
  ethiopic: "am",
  cyrillic: "ru",
  devanagari: "hi",
  bengali: "bn",
  tamil: "ta",
  telugu: "te",
  gujarati: "gu",
  kannada: "kn",
};

const KANA = /[\p{Script=Hiragana}\p{Script=Katakana}]/gu;
const URDU_LETTERS = /[ٹڈڑںھہے]/;
const PERSIAN_LETTERS = /[پچژگکی]/;
const VIETNAMESE_LETTERS = /[ăđơưĂĐƠƯẠ-ỹ]/g;
const TURKISH_LETTERS = /[ıİğĞşŞ]/g;

/** Common words that tell the Latin-script languages apart. A word may sit in two lists. */
const WORDS: Record<string, ReadonlySet<string>> = {
  en: new Set("the and of to is are was you that this with for it not have be".split(" ")),
  es: new Set("el los las del que y es una por para con se lo como pero está".split(" ")),
  fr: new Set("le les des est et une pour dans pas vous sur avec du au ce qui".split(" ")),
  de: new Set("der die das und ist nicht ein eine zu mit den sie auf für ich auch".split(" ")),
  pt: new Set("os da não uma com para em é que um se ao dos das você também são".split(" ")),
  it: new Set("il gli che è di della del non per una sono con un le anche questo".split(" ")),
  id: new Set(
    "yang dan di ini itu dengan untuk tidak dari dalam akan ada bisa saya anda juga".split(" "),
  ),
};

function arabicScriptLanguage(prose: string): ReadAloudLanguage {
  if (URDU_LETTERS.test(prose)) return "ur";
  if (PERSIAN_LETTERS.test(prose)) return "fa";
  return "ar";
}

// Every threshold and word list here is a heuristic; tune on real replies.
function latinLanguage(prose: string, latin: number): ReadAloudLanguage {
  if (count(prose, VIETNAMESE_LETTERS) >= 0.05 * latin) return "vi";
  if (count(prose, TURKISH_LETTERS) >= 0.02 * latin) return "tr";
  const hits = new Map<string, number>();
  for (const word of prose.toLowerCase().match(/\p{L}+/gu) ?? []) {
    for (const [language, words] of Object.entries(WORDS)) {
      if (words.has(word)) hits.set(language, (hits.get(language) ?? 0) + 1);
    }
  }
  const ranked = [...hits].sort((a, b) => b[1] - a[1]);
  const best = ranked[0];
  if (best && best[1] >= 2 && best[1] > (ranked[1]?.[1] ?? 0)) return best[0] as ReadAloudLanguage;
  return "en";
}

/** One language for a whole reply: the dominant script, else common words, else null when the
 * sample holds no letter. */
export function detectLanguage(prose: string): ReadAloudLanguage | null {
  const latin = count(prose, /\p{Script=Latin}/gu);
  let top: { script: string; weight: number } | null = null;
  for (const [script, weight, pattern] of NON_LATIN_SCRIPTS) {
    const total = weight * count(prose, pattern);
    if (total > 0 && (top === null || total > top.weight)) top = { script, weight: total };
  }
  // A non-Latin script wins from a quarter of the Latin letters (identifiers and
  // product names are Latin inside Japanese or Russian prose).
  if (top && top.weight >= 0.25 * latin) {
    if (top.script === "cjk") {
      const cjk = top.weight / 3;
      return count(prose, KANA) >= 0.1 * cjk ? "ja" : "zh";
    }
    if (top.script === "arabic") return arabicScriptLanguage(prose);
    return LANGUAGE_OF_SCRIPT[top.script] ?? null;
  }
  return latin > 0 ? latinLanguage(prose, latin) : null;
}

const CUT_AFTER = /[\s、。，,；;：:]/;

/** Cuts one sentence longer than `max` at the last space or clause mark that fits, else at `max`. */
function cutLong(sentence: string, max: number): string[] {
  const pieces: string[] = [];
  let rest = sentence.trim();
  while (rest.length > max) {
    const head = rest.slice(0, max);
    let cut = 0;
    for (let index = head.length - 1; index >= 0 && cut === 0; index--) {
      if (CUT_AFTER.test(head.charAt(index))) cut = index + 1;
    }
    if (cut === 0) cut = max;
    // Never split a surrogate pair: half an emoji is not a character.
    const last = rest.charCodeAt(cut - 1);
    if (cut > 1 && last >= 0xd800 && last <= 0xdbff) cut -= 1;
    const piece = rest.slice(0, cut).trim();
    if (piece) pieces.push(piece);
    rest = rest.slice(cut).trimStart();
  }
  if (rest) pieces.push(rest);
  return pieces;
}

function packSentences(line: string, language: ReadAloudLanguage, max: number): string[] {
  const chunks: string[] = [];
  let current = "";
  const flush = () => {
    const piece = current.trim();
    if (piece) chunks.push(piece);
    current = "";
  };
  const sentences = new Intl.Segmenter(language, { granularity: "sentence" }).segment(line);
  for (const { segment } of sentences) {
    if (segment.length > max) {
      flush();
      chunks.push(...cutLong(segment, max));
      continue;
    }
    if (current.length + segment.length > max) flush();
    current += segment;
  }
  flush();
  return chunks;
}

/** The pieces handed to the speech engine one at a time: a line each, a long line by sentence. */
export function speechChunks(
  text: string,
  language: ReadAloudLanguage,
  max = MAX_CHUNK_CHARS,
): string[] {
  const chunks: string[] = [];
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    if (line.length <= max) chunks.push(line);
    else chunks.push(...packSentences(line, language, max));
  }
  return chunks;
}
