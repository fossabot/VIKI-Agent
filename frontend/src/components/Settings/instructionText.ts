/**
 * The text model behind the instruction editor. A file is editable only when its bytes survive an
 * edit unchanged: an optional BOM and one uniform line break (all LF, all CRLF, or none) are kept
 * out of the draft and put back by `contentFor`. A lone CR, mixed breaks or a C0 control character
 * would be altered by a textarea, so those files are shown read-only with a reason.
 */
import { SHA256 } from "../../state/vaultApi";
import { visibleText as codesFor } from "../Cards/visibleText";

export type EditableText = { bom: boolean; newline: "lf" | "crlf" | "none"; text: string };
export type NotEditable = { reason: "control" | "line_endings" };
/** `source` and `sha256` (the file's bytes as text, and their hash) come with `text` when the note
 * response carries both. */
export type InstructionNote = { text: string; source?: string; sha256?: string };

const BOM = "\uFEFF";

/** Accept only the requested file and a text body from a note response. */
export function instructionNote(
  body: Record<string, unknown>,
  name: string,
): InstructionNote | null {
  const note = body.note;
  if (typeof note !== "object" || note === null) return null;
  const { ref, text, source, sha256 } = note as Record<string, unknown>;
  if (ref !== name || typeof text !== "string") return null;
  return typeof source === "string" && typeof sha256 === "string" && SHA256.test(sha256)
    ? { text, source, sha256 }
    : { text };
}

/**
 * Where the first character a save refuses sits, or -1: a C0 control other than tab, LF and CR, or
 * half of a surrogate pair. The vault client and the server refuse the same class.
 */
export function unsavableAt(text: string): number {
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i);
    if (c < 0x20 && c !== 0x09 && c !== 0x0a && c !== 0x0d) return i;
    if (c >= 0xd800 && c <= 0xdfff) {
      const next = text.charCodeAt(i + 1);
      if (c >= 0xdc00 || !(next >= 0xdc00 && next <= 0xdfff)) return i;
      i += 1;
    }
  }
  return -1;
}

/** The draft text (LF breaks) and what to restore around it, or why the source cannot be edited. */
export function editableText(source: string): EditableText | NotEditable {
  const bom = source.startsWith(BOM);
  const body = bom ? source.slice(1) : source;
  if (unsavableAt(body) !== -1) return { reason: "control" };
  if (body.includes("\r")) {
    if (/\r(?!\n)/.test(body) || /(?<!\r)\n/.test(body)) return { reason: "line_endings" };
    return { bom, newline: "crlf", text: body.replace(/\r\n/g, "\n") };
  }
  return { bom, newline: body.includes("\n") ? "lf" : "none", text: body };
}

/** The file content for a draft: the BOM and line breaks `base` was read with. */
export function contentFor(base: EditableText, text: string): string {
  const body = base.newline === "crlf" ? text.replace(/\n/g, "\r\n") : text;
  return (base.bom ? BOM : "") + body;
}

/** The text without the BOM a file may start with. */
export function withoutBom(source: string): string {
  return source.startsWith(BOM) ? source.slice(1) : source;
}

// Left as written: a CRLF break, which is layout, and a joiner between two non-ASCII characters,
// which shapes an emoji sequence or Persian and Indic text and moves nothing. Instruction files in
// those scripts would otherwise be flagged on every line.
const KEPT = /(\r\n|(?<=\P{ASCII})[\u200C\u200D](?=\P{ASCII}))/u;

/**
 * `text` for reading: every other character that draws nothing or reorders the line around it is
 * written as its code, `<U+202E>`, so a line cannot read differently on screen from what Claude
 * Code receives. `hidden` says whether any was found. For display only; the text is not changed.
 */
export function visibleText(text: string): { text: string; hidden: boolean } {
  let hidden = false;
  const shown = text
    .split(KEPT)
    .map((piece, index) => {
      if (index % 2 === 1) return piece;
      const part = codesFor(piece);
      hidden ||= part.hidden;
      return part.text;
    })
    .join("");
  return { text: shown, hidden };
}

/** The lines of `text` that hold a character that draws nothing, numbered from 1, for reading. */
export function hiddenLines(text: string, limit: number): { line: number; text: string }[] {
  if (!visibleText(text).hidden) return [];
  const found: { line: number; text: string }[] = [];
  const lines = text.split("\n");
  for (let i = 0; i < lines.length && found.length < limit; i++) {
    const shown = visibleText(lines[i] ?? "");
    if (shown.hidden) found.push({ line: i + 1, text: shown.text });
  }
  return found;
}
