/** Pins the voices fieldset's reset against `ReadAloudVoicesField.css`'s source text (jsdom applies
 * no stylesheet rules). */
import { expect, test } from "vitest";
import { readCss, ruleIn } from "../../testing/css";

const css = readCss(import.meta.url, "ReadAloudVoicesField.css");

test("the voices fieldset has no border, margin or padding", () => {
  const rule = ruleIn(css, ".read-aloud-voices");
  expect(rule).toMatch(/border:\s*0\b/);
  expect(rule).toMatch(/margin:\s*0\b/);
  expect(rule).toMatch(/padding:\s*0\b/);
  expect(rule).toMatch(/min-inline-size:\s*0\b/);
});
