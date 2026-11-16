import { describe, expect, it } from "vitest";
import { narrowBlock, readCss, ruleIn } from "../../testing/css";

const css = readCss(import.meta.url, "AppMenu.css");
const ANCHOR_SUPPORTS = "@supports (top: anchor(bottom))";
const COMPACT_QUERY = "@container shell not (min-width: 800px)";

describe("AppMenu.css", () => {
  it("the toggle is a 32px square that never shrinks in the top bar's flex row", () => {
    const rule = ruleIn(css, ".app-menu-toggle");
    expect(rule).toContain("flex: none");
    expect(rule).toContain("width: 32px");
    expect(rule).toContain("height: 32px");
  });

  it("bounds the fallback sheet on both axes and scrolls inside the menu", () => {
    const panel = ruleIn(css, ".app-menu-panel");
    for (const declaration of [
      "position: fixed",
      "top: 49px",
      "left: 12px",
      "right: 12px",
      "bottom: 12px",
      "width: auto",
      "max-width: calc(100% - 24px)",
      "height: auto",
      "max-height: max-content",
      "overflow-y: auto",
      "overscroll-behavior-y: contain",
    ])
      expect(panel).toContain(declaration);
  });

  it("keeps the wide dropdown aligned with its real button when anchor positioning is supported", () => {
    expect(ruleIn(css, ".app-menu-wrap")).toContain("anchor-name: --app-menu");
    const panel = ruleIn(narrowBlock(css, ANCHOR_SUPPORTS), ".app-menu-panel");
    expect(panel).toContain("position-anchor: --app-menu");
    expect(panel).toContain("top: calc(anchor(bottom) + 6px)");
    expect(panel).toContain("right: max(12px, anchor(right))");
    expect(panel).toContain("left: auto");
    expect(panel).toContain("width: 260px");
  });

  it("uses the scaled shell width for its compact sheet", () => {
    const panel = ruleIn(narrowBlock(css, COMPACT_QUERY), ".app-menu-panel");
    expect(panel).toContain("left: 12px");
    expect(panel).toContain("right: 12px");
    expect(panel).toContain("width: auto");
  });

  it("allows full labels to wrap while preserving a normal row's height", () => {
    const row = ruleIn(css, ".app-menu-panel .mi");
    expect(row).toContain("height: auto");
    expect(row).toContain("min-height: 34px");
    const label = ruleIn(css, ".app-menu-row-label");
    expect(label).toContain("min-width: 0");
    expect(label).toContain("white-space: normal");
    expect(label).toContain("overflow-wrap: anywhere");
    expect(label).not.toContain("ellipsis");
    expect(label).not.toContain("hidden");
  });

  it("has no narrow Help fallback row", () => {
    expect(css).not.toContain(".app-menu-help-row");
  });
});
