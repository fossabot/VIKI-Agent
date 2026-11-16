export const EXCERPT_LABEL = /^\[vault excerpt\] (.+), lines (\d+)-(\d+), sha256 ([0-9a-f]{12})$/;

export type ExcerptSegment =
  | { kind: "text"; text: string }
  | { kind: "excerpt"; ref: string; start: number; end: number; hash12: string; body: string };

// A ref is at most 512 code points (1024 UTF-16 units); a longer line is never a label, so the
// label regex only ever runs over a bounded line.
const MAX_LABEL = 1200;
const FENCE = /^`{3,}$/;

const lineEnd = (text: string, from: number): number => {
  const newline = text.indexOf("\n", from);
  return newline === -1 ? text.length : newline;
};

// The start of the first line at or after `from` that is exactly `fence`, or -1.
const findClose = (text: string, from: number, fence: string): number => {
  for (let start = from; start <= text.length; ) {
    const end = lineEnd(text, start);
    if (text.slice(start, end) === fence) return start;
    start = end + 1;
  }
  return -1;
};

function parseLabel(
  line: string,
): { ref: string; start: number; end: number; hash12: string } | null {
  if (line.length > MAX_LABEL) return null;
  const match = EXCERPT_LABEL.exec(line);
  if (!match) return null;
  const start = Number(match[2]);
  const end = Number(match[3]);
  if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 1 || end < start)
    return null;
  return { ref: match[1] as string, start, end, hash12: match[4] as string };
}

/**
 * Splits a turn's text into plain text and the excerpt blocks the app prepends (a label line, a
 * fence of three or more backticks, the body, the same fence). The text may have been typed by
 * hand, so anything that is not a complete block stays text; the scan walks line by line with no
 * regex over the whole text. One newline on each side of a block is its separator, not text.
 */
export function splitExcerpts(text: string): ExcerptSegment[] {
  const segments: ExcerptSegment[] = [];
  // A fence with no closing line stays unclosed for every later label, whose scan is a suffix.
  const unclosed = new Set<string>();
  let cursor = 0;
  let position = 0;
  while (position <= text.length) {
    const labelEnd = lineEnd(text, position);
    const label = parseLabel(text.slice(position, labelEnd));
    const fenceEnd = lineEnd(text, labelEnd + 1);
    const fence = text.slice(labelEnd + 1, fenceEnd);
    let closeStart = -1;
    if (label && FENCE.test(fence) && !unclosed.has(fence)) {
      closeStart = findClose(text, fenceEnd + 1, fence);
      if (closeStart === -1) unclosed.add(fence);
    }
    // An empty body (the closing fence right after the opening one) is not a block the app writes.
    if (!label || closeStart <= fenceEnd + 1) {
      position = labelEnd + 1;
      continue;
    }
    const before = text.slice(cursor, Math.max(cursor, position - 1));
    if (before !== "") segments.push({ kind: "text", text: before });
    segments.push({
      kind: "excerpt",
      ref: label.ref,
      start: label.start,
      end: label.end,
      hash12: label.hash12,
      body: text.slice(fenceEnd + 1, closeStart - 1),
    });
    cursor = Math.min(closeStart + fence.length + 1, text.length);
    position = cursor;
    if (cursor === text.length) break;
  }
  if (segments.length === 0) return [{ kind: "text", text }];
  if (cursor < text.length) segments.push({ kind: "text", text: text.slice(cursor) });
  return segments;
}
