import { describe, expect, it } from "vitest";
import { FRONTMATTER_SCAN_LINES, readerBody } from "./readerBody";

describe("readerBody", () => {
  it("strips terminated frontmatter and duplicate H1 without changing input", () => {
    const text = "---\r\ntitle: one.md\r\n---\r\n\r\n# one.md\r\n\r\nBody text.\r\n";
    expect(readerBody(text, "one.md")).toBe("\r\nBody text.\r\n");
    expect(text).toContain("title: one.md");
  });

  it("keeps unterminated frontmatter whole", () => {
    const text = "---\ntitle: one.md\nstill open\n\nBody text.\n";
    expect(readerBody(text, "one.md")).toBe(text);
  });

  it("keeps a duplicate H1 when no terminated frontmatter precedes it", () => {
    expect(readerBody("# one.md\nBody", "one.md")).toBe("# one.md\nBody");
  });

  it("retains the established scan cap", () => {
    expect(FRONTMATTER_SCAN_LINES).toBe(257);
  });
});
