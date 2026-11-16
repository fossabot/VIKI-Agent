import { describe, expect, it } from "vitest";
import { NARROW_QUERY, narrowBlock, readCss } from "../../testing/css";

const css = readCss(import.meta.url, "Banners.css");

describe("Banners.css", () => {
  it("both banners are a full-width flex strip at every width", () => {
    // A shared `.chat-only-banner, .readiness-banner { ... }` rule (a comma-separated selector
    // list, which ruleIn's single-selector regex cannot isolate) — checked as plain source text.
    expect(css).toContain(".chat-only-banner,\n.readiness-banner {");
    expect(css).toContain("flex: none");
    expect(css).toContain("display: flex");
  });

  it(`has a ${NARROW_QUERY} block`, () => {
    expect(() => narrowBlock(css)).not.toThrow();
  });

  it("the narrow block wraps text and buttons onto their own line, full width, 16px gutter", () => {
    const block = narrowBlock(css);
    expect(block).toContain(".chat-only-banner");
    expect(block).toContain(".readiness-banner");
    expect(block).toContain("flex-wrap: wrap");
    expect(block).toContain("padding: 9px 16px");
    expect(block).toContain(".chat-only-banner-actions");
    expect(block).toContain("margin-left: 0");
    expect(block).toContain("width: 100%");
  });
});
