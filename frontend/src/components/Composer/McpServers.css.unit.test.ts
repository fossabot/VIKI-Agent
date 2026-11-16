import { describe, expect, it } from "vitest";
import { narrowBlock, readCss, ruleIn } from "../../testing/css";

const css = readCss(import.meta.url, "McpServers.css");
const composerCss = readCss(import.meta.url, "Composer.css");
const ANCHOR_SUPPORTS = "@supports (position-area: top)";

describe("McpServers.css", () => {
  it("Composer.css names the anchor the dialog positions itself against", () => {
    expect(ruleIn(composerCss, ".composer-wrap")).toContain("anchor-name: --composer");
  });

  it("opens upward from the composer's anchor where position-area is supported", () => {
    const dialog = ruleIn(narrowBlock(css, ANCHOR_SUPPORTS), ".mcp-servers-dialog");
    expect(dialog).toContain("position-anchor: --composer");
    expect(dialog).toContain("position-area: top span-left");
    expect(dialog).toContain("inset: auto");
    expect(dialog).toContain("transform: none");
    expect(dialog).toContain("margin-bottom: 6px");
    expect(dialog).toContain("width: min(480px, calc(100% - 32px))");
    expect(dialog).toContain("max-height: calc(100% - 22px)");
  });

  it("keeps the centered fixed-position panel as the fallback", () => {
    const dialog = ruleIn(css, ".mcp-servers-dialog");
    expect(dialog).toContain("position: fixed");
  });
});
