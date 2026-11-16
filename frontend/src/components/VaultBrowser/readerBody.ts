// Note content renders `text` (the display copy), never `source` (which the editor alone uses):
// a leading frontmatter block is already shown through the chips and `.vault-ref`, and the
// title through the h2, so both are redundant in the body. Scans only the first 257 lines for a
// closing `---` (matching the server's own frontmatter cap); an unterminated block is left
// whole, dashes and all, rather than guessed at.
export const FRONTMATTER_SCAN_LINES = 257;
export function readerBody(text: string, title: string): string {
  const lines = text.split("\n");
  if ((lines[0] ?? "").trim() !== "---") return text;
  const closeOffset = lines
    .slice(1, FRONTMATTER_SCAN_LINES)
    .findIndex((line) => line.trim() === "---");
  if (closeOffset === -1) return text;
  const body = lines.slice(closeOffset + 2);
  let start = 0;
  while (start < body.length && (body[start] ?? "").trim() === "") start += 1;
  if ((body[start] ?? "").trim() === `# ${title}`) start += 1;
  return body.slice(start).join("\n");
}
