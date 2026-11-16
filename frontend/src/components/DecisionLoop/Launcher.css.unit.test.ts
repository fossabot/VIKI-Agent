/**
 * Launcher.css facts pinned against the source (jsdom applies no CSS from stylesheets): the
 * launcher's 100 px box and its ring, arc, badge and pulse, all scaled to match. The mark
 * rule, colours and focus ring are not pinned here.
 */
import { expect, test } from "vitest";
import { narrowBlock, readCss, ruleIn } from "../../testing/css";

const css = readCss(import.meta.url, "Launcher.css");

test("the launcher is a 100px square", () => {
  const rule = ruleIn(css, ".dloop-launcher");
  expect(rule).toMatch(/width:\s*100px/);
  expect(rule).toMatch(/height:\s*100px/);
});

test("the ring sits 6px outside the button with a 3px border", () => {
  const rule = ruleIn(css, ".dloop-launcher-ring");
  expect(rule).toMatch(/inset:\s*-6px/);
  expect(rule).toMatch(/border:\s*3px solid transparent/);
});

test("the arc sits 8px outside the button with a 3px border; its top colour is unchanged", () => {
  const rule = ruleIn(css, ".dloop-launcher-arc");
  expect(rule).toMatch(/inset:\s*-8px/);
  expect(rule).toMatch(/border:\s*3px solid transparent/);
  expect(rule).toMatch(/border-top-color:\s*rgb\(var\(--or-rgb\) \/ 0\.9\)/);
});

test("the badge grows to 24px, 2px inside the top-right corner, with bigger text", () => {
  const rule = ruleIn(css, ".dloop-launcher-badge");
  expect(rule).toMatch(/top:\s*2px/);
  expect(rule).toMatch(/right:\s*2px/);
  expect(rule).toMatch(/min-width:\s*24px/);
  expect(rule).toMatch(/height:\s*24px/);
  expect(rule).toMatch(/padding:\s*0 6px/);
  expect(rule).toMatch(/border-radius:\s*12px/);
  expect(rule).toMatch(/font-size:\s*13px/);
});

test("the nudge pulse now spreads to 16px", () => {
  const keyframes = narrowBlock(css, "@keyframes dloop-launcher-pulse");
  expect(ruleIn(keyframes, "to")).toMatch(/0 0 0 16px/);
});
