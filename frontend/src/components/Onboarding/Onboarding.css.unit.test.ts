import { describe, expect, it } from "vitest";
import { NARROW_QUERY, narrowBlock, readCss, ruleIn } from "../../testing/css";

const css = readCss(import.meta.url, "Onboarding.css");

describe("Onboarding.css", () => {
  it("is a single column, max-width 560, centred at every width", () => {
    const rule = ruleIn(css, ".onboarding-view");
    expect(rule).toContain("max-width: 560px");
    expect(rule).toContain("margin: 0 auto");
  });

  it(`has a ${NARROW_QUERY} block`, () => {
    expect(() => narrowBlock(css)).not.toThrow();
  });

  it("the narrow block trims the gutter to 16px and widens the path input", () => {
    const block = narrowBlock(css);
    expect(block).toContain("padding: 16px");
    expect(block).toContain(".path-form-field input");
    expect(block).toContain("width: 100%");
  });

  it("cards stack in a column", () => {
    const rule = ruleIn(css, ".onboarding-cards");
    expect(rule).toContain("flex-direction: column");
  });
});
