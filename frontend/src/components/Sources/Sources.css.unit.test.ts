import { describe, expect, it } from "vitest";
import { readCss, ruleIn } from "../../testing/css";

const css = readCss(import.meta.url, "Sources.css");

describe("Sources.css", () => {
  it("a chip with no agent falls back to inline-block so its label still ellipses", () => {
    const rule = ruleIn(css, ".source-chip:not(:has(> .source-chip-agent))");
    expect(rule).toContain("display: inline-block");
  });

  it("keeps exact source refs wrapped and source rows within their container", () => {
    expect(ruleIn(css, ".source-strip-ref")).toContain("overflow-wrap: anywhere");
    expect(ruleIn(css, ".source-chip-ref")).toContain("overflow-wrap: anywhere");
    expect(ruleIn(css, ".reply-source")).toContain("min-width: 0");
  });
});
