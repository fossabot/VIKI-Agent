export interface NotePeek {
  title: string;
  summary: string | null;
  modified: string | null;
  confidence: string | null;
}

/**
 * Extract peek information from a note's text and frontmatter.
 * @param text - The full text content of the note
 * @param frontmatter - A key-value mapping from the YAML frontmatter
 * @param title - The display title of the note
 * @returns NotePeek with extracted title, summary, modified, and confidence
 */
export function peekNote(
  text: string,
  frontmatter: Record<string, string>,
  title: string,
): NotePeek {
  return {
    title,
    summary: extractSummary(text),
    modified: extractFrontmatterField(frontmatter, "modified", 32),
    confidence: extractFrontmatterField(frontmatter, "confidence", 16),
  };
}

/**
 * Extract the summary from note text.
 * Finds the first line matching `^##\s+Summary\s*$` (case-insensitive),
 * then collects non-blank lines until the next blank line or heading.
 * Collapses whitespace and cuts at 280 code points with "…".
 */
function extractSummary(text: string): string | null {
  const lines = text.split(/\r?\n/);

  // Find the Summary heading line
  let summaryIndex = -1;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line && /^##\s+summary\s*$/i.test(line)) {
      summaryIndex = i;
      break;
    }
  }

  if (summaryIndex === -1) return null;

  // Skip blank lines after the heading
  let contentStart = summaryIndex + 1;
  while (contentStart < lines.length && lines[contentStart]?.trim() === "") {
    contentStart++;
  }

  // If we've reached the end or the next line is a heading, there's no summary
  if (contentStart >= lines.length) return null;
  const nextLine = lines[contentStart];
  if (nextLine && /^#+/.test(nextLine)) return null;

  // Collect lines until blank line or next heading
  const contentLines: string[] = [];
  for (let i = contentStart; i < lines.length; i++) {
    const line = lines[i];
    if (line === undefined) break;

    // Stop at blank line
    if (line.trim() === "") break;

    // Stop at next heading
    if (/^#+/.test(line)) break;

    contentLines.push(line);
  }

  if (contentLines.length === 0) return null;

  // Join lines and collapse whitespace
  let summary = contentLines.join("\n").replace(/\s+/g, " ").trim();

  // Cut at 280 code points with ellipsis
  const codePoints = Array.from(summary);
  if (codePoints.length > 280) {
    summary = codePoints.slice(0, 280).join("") + "…";
  }

  return summary;
}

/**
 * Extract a field from frontmatter, trim it, remove one pair of surrounding quotes,
 * and truncate to maxLength. Returns null if empty after processing.
 */
function extractFrontmatterField(
  frontmatter: Record<string, string>,
  field: string,
  maxLength: number,
): string | null {
  let value = frontmatter[field];
  if (!value) return null;

  // Trim
  value = value.trim();

  // Remove one pair of surrounding quotes
  if (
    value.length >= 2 &&
    ((value[0] === '"' && value[value.length - 1] === '"') ||
      (value[0] === "'" && value[value.length - 1] === "'"))
  ) {
    value = value.slice(1, -1);
  }

  // Trim again after removing quotes
  value = value.trim();

  // Return null if empty
  if (!value) return null;

  // Truncate to maxLength
  return value.substring(0, maxLength);
}
