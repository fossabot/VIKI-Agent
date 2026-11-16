import { expect, test } from "vitest";
import { readCss, ruleIn } from "../testing/css";

// jsdom applies no stylesheet, so the source of the shared primitives is pinned.
const css = readCss(import.meta.url, "./base.css");

const RULES: [string, RegExp[]][] = [
  [".btn > .ico:first-child:not(:only-child)", [/margin-inline-end:\s*6px/]],
  ['.btn[aria-disabled="true"]', [/cursor:\s*default/, /opacity:\s*0\.5/]],
  [".btn:focus-visible", [/outline:\s*2px solid var\(--or\)/, /outline-offset:\s*2px/]],
  [
    ".page-head",
    [
      /display:\s*grid/,
      /grid-template-columns:\s*minmax\(min\(10rem, 100%\), 1fr\) auto/,
      /gap:\s*4px 16px/,
      /margin:\s*0 0 16px/,
    ],
  ],
  [".page-head > .page-title", [/grid-row:\s*1/, /grid-column:\s*1;/]],
  [".page-head > .page-actions", [/grid-row:\s*1/, /grid-column:\s*2/]],
  [".page-title", [/font-size:\s*24px/, /font-weight:\s*600/, /color:\s*var\(--t0\)/]],
  [".page-intro", [/max-width:\s*65ch/, /font-size:\s*14px/, /color:\s*var\(--t2\)/]],
  [".page-actions", [/display:\s*flex/, /flex-wrap:\s*wrap/, /justify-content:\s*flex-end/]],
  [".section-hint", [/font-size:\s*12px/, /color:\s*var\(--t3\)/]],
  [
    ".attention-count",
    [
      /background:\s*var\(--or\)/,
      /color:\s*var\(--on-or\)/,
      /min-width:\s*20px/,
      /height:\s*20px/,
      /border-radius:\s*10px/,
      /font-variant-numeric:\s*tabular-nums/,
    ],
  ],
];

test.each(RULES)("base.css %s", (selector, declarations) => {
  const rule = ruleIn(css, selector);
  for (const declaration of declarations) expect(rule).toMatch(declaration);
});

test("the forced-colors block outlines the count", () => {
  expect(css).toMatch(/\.attention-count\s*\{\s*outline:\s*1px solid CanvasText/);
});

// ruleIn does not escape `*`, so this selector is matched by hand.
test("base.css .page-head > * spans the grid and may shrink", () => {
  expect(css).toMatch(/\.page-head > \*\s*\{[^}]*grid-column:\s*1 \/ -1[^}]*min-width:\s*0/);
});
