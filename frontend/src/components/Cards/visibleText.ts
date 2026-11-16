/**
 * Text this app did not write, as a surface that asks for a decision shows it.
 *
 * A request's command, path, plan or reason runs or is recorded exactly as sent, so what the
 * user reads has to be that same sequence of characters. Some characters break that: a bidi
 * control reorders the text painted around it, a zero-width or other default-ignorable character
 * paints nothing, and a control character or a line separator paints as nothing or as a break the
 * tool does not see. CSS cannot undo a bidi control that sits inside the text, so each such
 * character is replaced here by its code point, written `<U+202E>`, which paints in the order it
 * was sent.
 */

/** A selector or a joiner where an emoji uses it, which hides nothing there: the presentation
 * selector right after a pictograph or inside a keycap, and the joiner between two pictographs
 * (a person and a laptop drawn as one). Anywhere else the same characters are written out. */
const EMOJI_GLUE = [
  /(?<=\p{Extended_Pictographic})[\u{FE0E}\u{FE0F}]/u,
  /(?<=[0-9#*])\u{FE0F}(?=\u{20E3})/u,
  /(?<=\p{Extended_Pictographic}\u{FE0F}?|\p{Emoji_Modifier})\u{200D}(?=\p{Extended_Pictographic})/u,
]
  .map((glue) => glue.source)
  .join("|");

/** One character the reader cannot see for what it is:
 * - control characters, but for the tab and the line feed a command keeps as layout;
 * - format characters, the bidi controls and the zero-width characters among them;
 * - the line and paragraph separators;
 * - the rest of Unicode's default-ignorable set (fillers, variation selectors, tags). */
const HIDDEN = `(?![\\t\\n]|${EMOJI_GLUE})[\\p{Cc}\\p{Cf}\\p{Zl}\\p{Zp}\\p{Default_Ignorable_Code_Point}]`;

/** With a capture, so `split` keeps each hidden character as its own piece. */
const SPLIT = new RegExp(`(${HIDDEN})`, "u");
const EVERY = new RegExp(HIDDEN, "gu");

function codeOf(char: string): string {
  const point = char.codePointAt(0) ?? 0;
  return `<U+${point.toString(16).toUpperCase().padStart(4, "0")}>`;
}

/** `text` in the pieces a surface draws: plain runs as they are, and each hidden character as
 * its code with `code` set, so the surface can mark the codes it wrote apart from a command that
 * spells `<U+202E>` out itself. Never empty. */
export function visibleParts(text: string): { text: string; code: boolean }[] {
  const parts = text
    .split(SPLIT)
    .map((piece, index) =>
      index % 2 === 1 ? { text: codeOf(piece), code: true } : { text: piece, code: false },
    )
    .filter((part) => part.code || part.text !== "");
  return parts.length > 0 ? parts : [{ text: "", code: false }];
}

/** `text` with every hidden character written as its code, and whether there was one. */
export function visibleText(text: string): { text: string; hidden: boolean } {
  let hidden = false;
  const shown = text.replace(EVERY, (char) => {
    hidden = true;
    return codeOf(char);
  });
  return { text: shown, hidden };
}

/** `text` for a surface that draws one line (a row's title): hidden characters as their codes,
 * and each line break as ` ↵ `. Such a surface paints a line break as a space, which would make a
 * second command read as arguments of the first. */
export function visibleLine(text: string): string {
  return visibleText(text).text.replace(/\n/g, " ↵ ");
}
