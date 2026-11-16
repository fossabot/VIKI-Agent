/**
 * Pins Help's copies of the product facts (./productFacts) against the topic
 * markdown that repeats them word for word: welcome.md (product framing and independence)
 * and data.md (the "Where your data goes" rows). A wording change in either place without the
 * other fails here first.
 */
import { describe, expect, it } from "vitest";
import dataMd from "./help/topics/data.md?raw";
import welcomeMd from "./help/topics/welcome.md?raw";
import { DATA_FLOWS, NOT_AFFILIATED, PRODUCT_LINE } from "./productFacts";

describe("welcome.md", () => {
  it("opens 'What VIKI Agent is' with PRODUCT_LINE and 'What it is not' with NOT_AFFILIATED", () => {
    expect(welcomeMd).toContain(PRODUCT_LINE);
    expect(welcomeMd).toContain(NOT_AFFILIATED);
  });
});

describe("data.md", () => {
  it("lists every DATA_FLOWS row as '- label: fact', in order", () => {
    let previousIndex = -1;
    for (const { label, fact } of DATA_FLOWS) {
      const line = `- ${label}: ${fact}`;
      const index = dataMd.indexOf(line);
      expect(index, `missing or out of order: ${line}`).toBeGreaterThan(previousIndex);
      previousIndex = index;
    }
  });
});

describe("product framing", () => {
  it("never calls VIKI a companion or Claude our model", () => {
    const forbidden = /companion|our model/i;
    const constants = [PRODUCT_LINE, NOT_AFFILIATED, ...DATA_FLOWS.map((row) => row.fact)];
    for (const constant of constants) {
      expect(constant).not.toMatch(forbidden);
    }
  });
});
