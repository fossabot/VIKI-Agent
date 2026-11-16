/** Builds note text or an unsent chat hand-off from a reply. These pure builders do not access the vault or publish a note. */
import type { ToolSource } from "../../clients/conversations/conversationSearchApi";
import type { Source } from "../../state/sources";
import { validWriteRef } from "../../state/vaultApi";
import type { TextBlock, Turn } from "../../state/view";
import { fenceFor } from "../Decisions/handoff";

export interface NoteDraft {
  ref: string;
  content: string;
}

const LEADING_MARKER = /^(?:#{1,6}|>|[-*]|1\.)\s*/;
const FORBIDDEN_TITLE_CHARS = /[*_`[\]\\/:?"<>|\p{Cc}]/gu;
const TRAILING_DOT_SPACE = /[.\s]+$/u;
const MAX_TITLE_CODEPOINTS = 60;
const NO_SOURCES = "No vault note or web page was read for this reply.";

/** Every text block's words, in arrival order, joined by a blank line — the same join as the
 * reply's own CopyButton (Conversation.tsx's replyText) — with the outer whitespace trimmed,
 * since a note's title and a hand-off message both read the first and last line. */
export function replyWords(turn: Turn): string {
  return turn.blocks
    .filter((block): block is TextBlock => block.kind === "text")
    .map((block) => block.text)
    .join("\n\n")
    .trim();
}

/** `value` cut to at most `max` code points, breaking at the last space within that cut so a
 * title never ends mid-word — unless there is no space to break at, in which case it hard-cuts. */
function cutAtSpace(value: string, max: number): string {
  const codePoints = [...value];
  if (codePoints.length <= max) return value;
  const cut = codePoints.slice(0, max).join("");
  const lastSpace = cut.lastIndexOf(" ");
  return lastSpace > 0 ? cut.slice(0, lastSpace) : cut;
}

/** The reply's title: the first non-blank line of `words`, its leading markdown marker (a
 * heading, a blockquote, a bullet or the first numbered item) stripped, then every markdown or
 * filename-hostile character and control character removed, trailing dots and spaces removed,
 * and the result cut to 60 code points at a space. `Saved reply ${today}` when nothing usable is
 * left. */
function titleFrom(words: string, today: string): string {
  const line = words.split("\n").find((candidate) => candidate.trim() !== "") ?? "";
  const cleaned = line
    .trim()
    .replace(LEADING_MARKER, "")
    .replace(FORBIDDEN_TITLE_CHARS, "")
    .replace(TRAILING_DOT_SPACE, "");
  return cutAtSpace(cleaned, MAX_TITLE_CODEPOINTS) || `Saved reply ${today}`;
}

/** One `## Sources` line per source. A note links its stem; `withRef` also appends its ref (for
 * the hand-off message, so the agent reading it has the exact path). A search or web row prefers
 * the matching `tool-sources` resolution over the row's own display text, falling back to that
 * text when the id did not resolve. */
function sourceLines(
  sources: readonly Source[],
  resolved: ReadonlyMap<string, ToolSource>,
  withRef: boolean,
): string[] {
  if (sources.length === 0) return [NO_SOURCES];
  return sources.map((item) => {
    if (item.kind === "note") {
      return withRef ? `- [[${item.label}]] (${item.ref})` : `- [[${item.label}]]`;
    }
    const match = resolved.get(item.toolUseId);
    if (item.kind === "search") {
      if (match && (match.tool === "Grep" || match.tool === "Glob")) {
        const path = match.path_state === "root" ? "the whole vault" : match.path;
        return `- Searched ${path} for "${match.pattern}"`;
      }
      return `- Searched: ${item.label}`;
    }
    if (match && match.tool === "WebFetch") return `- ${match.url}`;
    return `- A page on ${item.label}`;
  });
}

/** A draft for a direct "New note" editor: a title and `.md` ref from the reply's first
 * line, the reply words, and a `## Sources` list. The ref falls back to `Saved reply ${today}.md`
 * when the title does not pass `validWriteRef` — only the filename gets that safer fallback; the
 * content's own `# title` heading keeps the extracted title either way. Null when the turn has no
 * words to save. */
export function noteDraft(
  turn: Turn,
  sources: readonly Source[],
  resolved: ReadonlyMap<string, ToolSource>,
  today: string,
): NoteDraft | null {
  const words = replyWords(turn);
  if (!words) return null;
  const title = titleFrom(words, today);
  const ref = `${title}.md`;
  const content = [
    `# ${title}`,
    "",
    words,
    "## Sources",
    ...sourceLines(sources, resolved, false),
  ].join("\n");
  return { ref: validWriteRef(ref) ? ref : `Saved reply ${today}.md`, content };
}

/** A new chat draft asking VIKI to save the reply itself in a protected vault: the sources
 * read, then the reply words in a `fenceFor` fence tagged markdown. Null when the turn has no
 * words to save. */
export function handoffText(
  turn: Turn,
  sources: readonly Source[],
  resolved: ReadonlyMap<string, ToolSource>,
): string | null {
  const words = replyWords(turn);
  if (!words) return null;
  const fence = fenceFor(words);
  return [
    "Please save the reply below as a knowledge note in this vault. Follow the vault's own note conventions, and link the sources listed.",
    "",
    "Sources read for this reply:",
    ...sourceLines(sources, resolved, true),
    "",
    "Reply:",
    "",
    `${fence}markdown`,
    words,
    fence,
  ].join("\n");
}
