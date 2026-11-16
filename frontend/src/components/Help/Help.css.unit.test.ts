import { describe, expect, it } from "vitest";
import { NARROW_QUERY, narrowBlock, readCss, ruleIn } from "../../testing/css";

const css = readCss(import.meta.url, "Help.css");

describe("Help.css", () => {
  it(`has a ${NARROW_QUERY} block`, () => {
    expect(() => narrowBlock(css)).not.toThrow();
  });

  it("in the narrow block, the nav scrolls within 40vh instead of pushing the page to zero height", () => {
    const block = narrowBlock(css);

    const nav = ruleIn(block, ".help-nav");
    expect(nav).toContain("flex: none");
    expect(nav).toContain("min-height: 0");
    expect(nav).toContain("max-height: 40vh");

    const page = ruleIn(block, ".help-page");
    expect(page).toContain("flex: 1 1 auto");
    expect(page).toContain("min-height: 0");
  });

  it("the .help-head row is a flex row, Help heading next to Close help", () => {
    const head = ruleIn(css, ".help-head");
    expect(head).toContain("display: flex");
    expect(head).toContain("justify-content: space-between");
  });

  it(".help-nav-top sticks to the top of the scrolling nav", () => {
    const navTop = ruleIn(css, ".help-nav-top");
    expect(navTop).toContain("position: sticky");
    expect(navTop).toContain("top: 0");
  });

  it(".help-group-title is sized under the page heading, matching the rails' small labels", () => {
    const groupTitle = ruleIn(css, ".help-group-title");
    expect(groupTitle).toContain("font-size: 12px");
  });
});
