/**
 * Turns `[[wikilinks]]` in note text into markdown links the Markdown component can render as
 * clickable buttons (see `NOTE_LINK_PREFIX` in Markdown.tsx). The grammar — character classes,
 * optional heading and alias — mirrors `WIKI` and `_link_targets` in
 * src/viki_agent/vaults/vault.py so a target computed here always matches the one the server
 * already resolved when it listed the note's links.
 */
import { NOTE_LINK_PREFIX } from "../Conversation/Markdown";

// Same character classes as the server's WIKI_NAME, WIKI_HEADING and LINK_TEXT: each negated
// class already excludes the delimiters, so a failed match stops at the next `[` instead of
// backtracking across the note.
const WIKI_NAME = "[^\\[\\]|#\\n]";
const WIKI_HEADING = "[^\\[\\]|\\n]";
const LINK_TEXT = "[^\\[\\]]";
const WIKI_RE = new RegExp(
  `\\[\\[(${WIKI_NAME}+)(?:#${WIKI_HEADING}+)?(?:\\|(${LINK_TEXT}+))?\\]\\]`,
  "g",
);

/** Mirrors the server's `_link_targets`: text before the first `#`, trimmed, then `<` and `>`
 * stripped from both ends (any run of either, not just one matched pair — same as Python's
 * `str.strip("<>")`). */
export function wikiTarget(name: string): string {
  const beforeHash = name.split("#")[0] ?? "";
  return beforeHash
    .trim()
    .replace(/^[<>]+/, "")
    .replace(/[<>]+$/, "");
}

// Markdown link text: `[` or `]` would end it early, `\` would escape the next char.
function escapeLinkLabel(label: string): string {
  return label.replace(/[\\[\]]/g, (c) => `\\${c}`);
}

// A label rendered as plain text, outside any link: also guard the inline markers it could
// otherwise open (emphasis, inline code, autolink).
function escapePlainLabel(label: string): string {
  return label.replace(/[\\[\]*_`<]/g, (c) => `\\${c}`);
}

// encodeURIComponent leaves `(` and `)` as-is, so a target holding either would otherwise land in
// the markdown destination raw — `)` can close the destination early, `(` can show up as stray
// text. Markdown.tsx's decodeNoteTarget runs plain decodeURIComponent, which reverses %28/%29
// along with everything else it already decodes.
function encodeTarget(target: string): string {
  return encodeURIComponent(target).replace(/[()]/g, (paren) => (paren === "(" ? "%28" : "%29"));
}

interface Range {
  start: number;
  end: number;
}

// First later run with the same length as `runs[i]` (CommonMark's closer rule: the next run of
// equal length). A plain scan from i + 1, not `runs.findIndex`, which rescans from the start of
// the array on every call — on a line with many same-length runs (e.g. many single backticks)
// that redundant prefix made codeSpansOnLine quadratic in the run count.
function closingRun(runs: Range[], i: number, length: number): number {
  for (let j = i + 1; j < runs.length; j += 1) {
    const run = runs[j] as Range;
    if (run.end - run.start === length) return j;
  }
  return -1;
}

// A backtick run up to the next run of the same length, on the same line — CommonMark's inline
// code span rule, simplified: an unmatched run just isn't an opener.
function codeSpansOnLine(line: string): Range[] {
  const runs: Range[] = [];
  const runRe = /`+/g;
  for (const match of line.matchAll(runRe)) {
    const start = match.index ?? 0;
    runs.push({ start, end: start + match[0].length });
  }
  const spans: Range[] = [];
  let i = 0;
  while (i < runs.length) {
    const open = runs[i] as Range;
    const openLength = open.end - open.start;
    const closeIndex = closingRun(runs, i, openLength);
    if (closeIndex === -1) {
      i += 1;
      continue;
    }
    spans.push({ start: open.start, end: (runs[closeIndex] as Range).end });
    i = closeIndex + 1;
  }
  return spans;
}

// Ranges (absolute offsets into `text`) a wikilink match must not overlap: fenced code blocks (a
// line whose trimmed start is ``` or ~~~, open until a line with the same fence closes it) and,
// outside those, inline code spans. Returned in source order, so `linkWikilinks` can walk them
// with one advancing index instead of rescanning all of them for every match.
function excludedRanges(text: string): Range[] {
  const ranges: Range[] = [];
  let fence: "`" | "~" | null = null;
  let offset = 0;
  for (const line of text.split("\n")) {
    const lineEnd = offset + line.length;
    const trimmed = line.trim();
    if (fence) {
      ranges.push({ start: offset, end: lineEnd });
      if (
        (fence === "`" && trimmed.startsWith("```")) ||
        (fence === "~" && trimmed.startsWith("~~~"))
      ) {
        fence = null;
      }
    } else if (trimmed.startsWith("```") || trimmed.startsWith("~~~")) {
      ranges.push({ start: offset, end: lineEnd });
      fence = trimmed.startsWith("```") ? "`" : "~";
    } else {
      for (const span of codeSpansOnLine(line)) {
        ranges.push({ start: offset + span.start, end: offset + span.end });
      }
    }
    offset = lineEnd + 1; // skip the '\n'
  }
  return ranges;
}

// The run of consecutive backslashes immediately before `index` — the same test markdown itself
// uses to tell an escaped character from a literal one: an odd run escapes, an even run is itself
// escaped backslashes and leaves the next character live.
function backslashesBefore(text: string, index: number): number {
  let count = 0;
  let i = index - 1;
  while (i >= 0 && text[i] === "\\") {
    count += 1;
    i -= 1;
  }
  return count;
}

/** Rewrites each `[[name(#heading)?(|alias)?]]` into a markdown link the Markdown component
 * turns into a button (when `linkable` says the target resolves) or the alias/name alone
 * otherwise, escaped for safe plain-text rendering. Leaves untouched: an embed (`[[` preceded by
 * `!`), a `[[` escaped by an odd run of backslashes, a match that itself spans a newline (the
 * alias group's class allows one, but a blank line or a setext `---` inside it breaks the
 * markdown the rewrite relies on), and any match whose span overlaps a fenced code block or
 * an inline code span — not just a match that starts inside one, since a `|alias` can carry a
 * match from prose into a fence. Text with no match is returned as-is. */
export function linkWikilinks(text: string, linkable: (target: string) => boolean): string {
  if (!text.includes("[[")) return text;

  const excluded = excludedRanges(text);
  let result = "";
  let lastIndex = 0;
  let rangeIndex = 0;
  for (const match of text.matchAll(WIKI_RE)) {
    const index = match.index ?? 0;
    const matchEnd = index + match[0].length;
    result += text.slice(lastIndex, index);
    const isEmbed = text[index - 1] === "!";
    const isEscaped = backslashesBefore(text, index) % 2 === 1;
    const hasNewline = match[0].includes("\n");
    // Matches and ranges both run in source order: drop ranges that end before this match starts
    // (they are behind every later match too), then the next remaining range — if any — overlaps
    // the match (`range.start < matchEnd && matchStart < range.end`) iff it starts before the
    // match ends. Total work across the whole loop is O(matches + ranges), not O(matches x ranges).
    while (rangeIndex < excluded.length && (excluded[rangeIndex] as Range).end <= index) {
      rangeIndex += 1;
    }
    const nextRange = excluded[rangeIndex];
    const isExcluded = nextRange !== undefined && nextRange.start < matchEnd;
    if (isEmbed || isEscaped || hasNewline || isExcluded) {
      result += match[0];
    } else {
      const name = match[1] ?? "";
      const alias = match[2];
      const label = (alias ?? name).trim();
      const target = wikiTarget(name);
      result += linkable(target)
        ? `[${escapeLinkLabel(label)}](${NOTE_LINK_PREFIX}${encodeTarget(target)})`
        : escapePlainLabel(label);
    }
    lastIndex = matchEnd;
  }
  result += text.slice(lastIndex);
  return result;
}
