import { expect, test } from "vitest";
import { narrowBlock, readCss, ruleIn } from "../../testing/css";

const css = readCss(import.meta.url, "rowState.css");
const rail = readCss(import.meta.url, "LeftRail.css");
test("edge decoration cannot intercept row actions or replace the selected bar", () => {
  expect(ruleIn(css, ".sess-activity")).toMatch(/pointer-events:\s*none/);
  expect(ruleIn(rail, ".sess-selected::before")).toMatch(/background:\s*var\(--or\)/);
  expect(ruleIn(css, ".sess-has-activity .sess-dot")).toMatch(/display:\s*none/);
});
test("running, completion and attention have different token-based motion", () => {
  expect(css).toContain("--sess-edge-lap: 3.2s");
  expect(ruleIn(css, '.sess-activity[data-activity="done"]')).toContain("--sess-edge-lap: 11s");
  expect(ruleIn(css, '.sess-activity[data-activity="needs-you"]')).toContain(
    "--sess-edge-lap: 6.8s",
  );
  expect(css).toMatch(/conic-gradient\(\s*from var\(--viki-edge-turn\)/);
  expect(css).toContain("sess-edge-attention");
  expect(css).toContain("sess-edge-breathe");
});
test("OS and app reduced motion make all activity edges static", () => {
  const reduced = narrowBlock(css, "@media (prefers-reduced-motion: reduce)");
  for (const source of [reduced, css.slice(css.indexOf('html[data-motion="reduce"]'))]) {
    expect(source).toContain(".sess-activity[data-activity]::before");
    expect(source).toContain(".sess-activity[data-activity]::after");
    expect(source).toMatch(/animation:\s*none/);
    expect(source).toMatch(/background:\s*var\(--sess-edge-color\)/);
  }
});
test("the translucent rail has opaque contrast/transparency/forced-colors fallbacks", () => {
  expect(ruleIn(rail, ".left-rail")).toContain("color-mix(in srgb, var(--g1) 45%, transparent)");
  expect(ruleIn(rail, ".left-rail")).toContain("backdrop-filter: blur(22px)");
  for (const query of [
    "@media (prefers-reduced-transparency: reduce), (prefers-contrast: more)",
    "@media (forced-colors: active)",
  ]) {
    const fallback = narrowBlock(rail, query);
    expect(ruleIn(fallback, ".left-rail")).toContain("background: var(--g1)");
    expect(ruleIn(fallback, ".left-rail")).toContain("backdrop-filter: none");
  }
  const forced = narrowBlock(css, "@media (forced-colors: active)");
  expect(forced).toContain("border-color: CanvasText");
  expect(forced).toContain(".sess-has-activity:focus-visible");
});
