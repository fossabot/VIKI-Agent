import { expect, test } from "vitest";
import { peekNote } from "./notePeek";

test("returns interface with title, summary, modified, confidence fields", () => {
  const result = peekNote("", {}, "Test");
  expect(result).toHaveProperty("title");
  expect(result).toHaveProperty("summary");
  expect(result).toHaveProperty("modified");
  expect(result).toHaveProperty("confidence");
});

test("title is the passed title string", () => {
  const result = peekNote("", {}, "My Title");
  expect(result.title).toBe("My Title");
});

test("summary: finds first Summary heading and extracts next non-blank lines", () => {
  const text =
    "# Header\n\n## Summary\nThis is a summary.\nStill part of summary.\n\n## Next heading";
  const result = peekNote(text, {}, "Title");
  expect(result.summary).toBe("This is a summary. Still part of summary.");
});

test("summary: case-insensitive matching for Summary heading", () => {
  const text = "## summary\nContent here.";
  const result = peekNote(text, {}, "Title");
  expect(result.summary).toBe("Content here.");
});

test("summary: case-insensitive with SUMMARY", () => {
  const text = "## SUMMARY\nContent here.";
  const result = peekNote(text, {}, "Title");
  expect(result.summary).toBe("Content here.");
});

test("summary: stops at next heading starting with #", () => {
  const text = "## Summary\nLine 1\nLine 2\n# Next heading\nLine 3";
  const result = peekNote(text, {}, "Title");
  expect(result.summary).toBe("Line 1 Line 2");
});

test("summary: stops at next heading with ## prefix", () => {
  const text = "## Summary\nLine 1\nLine 2\n## Next\nLine 3";
  const result = peekNote(text, {}, "Title");
  expect(result.summary).toBe("Line 1 Line 2");
});

test("summary: stops at blank line", () => {
  const text = "## Summary\nLine 1\nLine 2\n\nLine 3";
  const result = peekNote(text, {}, "Title");
  expect(result.summary).toBe("Line 1 Line 2");
});

test("summary: skips initial blank lines after heading", () => {
  const text = "## Summary\n\n\nContent here.";
  const result = peekNote(text, {}, "Title");
  expect(result.summary).toBe("Content here.");
});

test("summary: collapses multiple whitespace into single space", () => {
  const text = "## Summary\nLine 1   with   spaces\nLine 2\t\twith\ttabs";
  const result = peekNote(text, {}, "Title");
  expect(result.summary).toBe("Line 1 with spaces Line 2 with tabs");
});

test("summary: cuts at 280 code points with ellipsis", () => {
  const longText = "a".repeat(290);
  const text = `## Summary\n${longText}`;
  const result = peekNote(text, {}, "Title");
  expect(result.summary?.length).toBeLessThanOrEqual(280 + 1); // 280 + "…" length
  expect(result.summary).toMatch(/…$/);
});

test("summary: returns null when no Summary heading found", () => {
  const text = "# Header\n## Other\nNo summary here.";
  const result = peekNote(text, {}, "Title");
  expect(result.summary).toBeNull();
});

test("summary: returns null when Summary heading found but empty", () => {
  const text = "## Summary\n\n\n";
  const result = peekNote(text, {}, "Title");
  expect(result.summary).toBeNull();
});

test("summary: handles CRLF line endings", () => {
  const text = "## Summary\r\nContent here.\r\n\r\nNext";
  const result = peekNote(text, {}, "Title");
  expect(result.summary).toBe("Content here.");
});

test("summary: heading as next line after Summary", () => {
  const text = "## Summary\n# Next\nContent";
  const result = peekNote(text, {}, "Title");
  expect(result.summary).toBeNull();
});

test("modified: extracts from frontmatter and trims", () => {
  const fm = { modified: "  2026-08-18  " };
  const result = peekNote("", fm, "Title");
  expect(result.modified).toBe("2026-08-18");
});

test("modified: removes one pair of surrounding quotes", () => {
  const fm = { modified: '"2026-08-18"' };
  const result = peekNote("", fm, "Title");
  expect(result.modified).toBe("2026-08-18");
});

test("modified: removes quotes and trims", () => {
  const fm = { modified: '  "2026-08-18"  ' };
  const result = peekNote("", fm, "Title");
  expect(result.modified).toBe("2026-08-18");
});

test("modified: returns null when not in frontmatter", () => {
  const result = peekNote("", {}, "Title");
  expect(result.modified).toBeNull();
});

test("modified: returns null when empty string", () => {
  const fm = { modified: "" };
  const result = peekNote("", fm, "Title");
  expect(result.modified).toBeNull();
});

test("modified: returns null when only whitespace", () => {
  const fm = { modified: "   " };
  const result = peekNote("", fm, "Title");
  expect(result.modified).toBeNull();
});

test("modified: truncates to 32 chars", () => {
  const fm = { modified: "a".repeat(40) };
  const result = peekNote("", fm, "Title");
  expect(result.modified).toBe("a".repeat(32));
});

test("confidence: extracts from frontmatter and trims", () => {
  const fm = { confidence: "  high  " };
  const result = peekNote("", fm, "Title");
  expect(result.confidence).toBe("high");
});

test("confidence: removes one pair of surrounding quotes", () => {
  const fm = { confidence: '"medium"' };
  const result = peekNote("", fm, "Title");
  expect(result.confidence).toBe("medium");
});

test("confidence: removes quotes and trims", () => {
  const fm = { confidence: '  "low"  ' };
  const result = peekNote("", fm, "Title");
  expect(result.confidence).toBe("low");
});

test("confidence: returns null when not in frontmatter", () => {
  const result = peekNote("", {}, "Title");
  expect(result.confidence).toBeNull();
});

test("confidence: returns null when empty string", () => {
  const fm = { confidence: "" };
  const result = peekNote("", fm, "Title");
  expect(result.confidence).toBeNull();
});

test("confidence: returns null when only whitespace", () => {
  const fm = { confidence: "   " };
  const result = peekNote("", fm, "Title");
  expect(result.confidence).toBeNull();
});

test("confidence: truncates to 16 chars", () => {
  const fm = { confidence: "a".repeat(25) };
  const result = peekNote("", fm, "Title");
  expect(result.confidence).toBe("a".repeat(16));
});

test("complete example with all fields", () => {
  const text = `# Title

## Summary
This is a comprehensive summary.
With multiple lines of content.

## Details
More info here.`;
  const fm = { modified: '"2026-08-18"', confidence: "high" };
  const result = peekNote(text, fm, "Example Note");
  expect(result.title).toBe("Example Note");
  expect(result.summary).toBe("This is a comprehensive summary. With multiple lines of content.");
  expect(result.modified).toBe("2026-08-18");
  expect(result.confidence).toBe("high");
});

test("summary: exactly 280 code points is not truncated", () => {
  const content = "a".repeat(280);
  const text = `## Summary\n${content}`;
  const result = peekNote(text, {}, "Title");
  expect(result.summary).toBe(content);
  expect(result.summary?.includes("…")).toBe(false);
});

test("summary: 281 code points is truncated", () => {
  const content = "a".repeat(281);
  const text = `## Summary\n${content}`;
  const result = peekNote(text, {}, "Title");
  expect(result.summary?.endsWith("…")).toBe(true);
  expect((result.summary ?? "").length).toBeLessThanOrEqual(280 + 1);
});

test("summary: 281 astral characters cut to 280 code points plus ellipsis with no lone surrogate", () => {
  // "😀" is a single code point (astral character U+1F600), represented as surrogate pair in UTF-16
  const emoji = "😀";
  const content = emoji.repeat(281);
  const text = `## Summary\n${content}`;
  const result = peekNote(text, {}, "Title");
  expect(result.summary?.endsWith("…")).toBe(true);
  // Check code point count: 280 emojis + "…" = 281 code points
  expect(Array.from(result.summary ?? "").length).toBe(281); // 280 emojis + "…"
  // Ensure no lone surrogate (validate it's well-formed)
  expect(() => JSON.stringify(result.summary)).not.toThrow();
});

test("summary: exactly 280 astral characters are not cut", () => {
  const emoji = "😀";
  const content = emoji.repeat(280);
  const text = `## Summary\n${content}`;
  const result = peekNote(text, {}, "Title");
  expect(result.summary).toBe(content);
  expect(result.summary?.includes("…")).toBe(false);
  // Check code point count matches: 280 emojis
  expect(Array.from(result.summary ?? "").length).toBe(280);
});
