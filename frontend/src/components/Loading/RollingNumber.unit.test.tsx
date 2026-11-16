/**
 * RollingNumber.tsx: `value` as one real text node (so a plain-text query matches the host's
 * own text), plus an aria-hidden `.roll` overlay of empty
 * per-character spans that add nothing to textContent, and per-digit element identity across a
 * digit-count change. The reduced-motion "move instantly" rule
 * is pinned here against RollingNumber.css's own source text, the same technique as every other
 * `*.css.unit.test.ts` in this app (jsdom applies no CSS from stylesheets) -- there is
 * no separate css test file for it, so it lives beside the behaviour.
 */
import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { readCss } from "../../testing/css";
import { RollingNumber } from "./RollingNumber";

afterEach(cleanup);

test.each([
  [7, "7"],
  [12, "12"],
  ["1,234", "1,234"],
])("value %s renders with textContent exactly %s", (value, text) => {
  const { container } = render(<RollingNumber value={value} />);
  expect(container.textContent).toBe(text);
});

test("a roll-host's own text is the whole value -- a plain-text query matches it like the frozen tile test does", () => {
  const { getByText } = render(
    <div className="tile-n roll-host">
      <RollingNumber value={116} />
    </div>,
  );
  const host = getByText("116", { selector: ".tile-n" });
  expect(host.className).toContain("roll-host");
});

test("the overlay is aria-hidden and its per-character spans carry no DOM text of their own", () => {
  const { container } = render(<RollingNumber value="1,234" />);
  const roll = container.querySelector(".roll");
  expect(roll?.getAttribute("aria-hidden")).toBe("true");
  const spans = [...container.querySelectorAll<HTMLElement>(".roll-digit, .roll-sep")];
  expect(spans).toHaveLength(5);
  expect(spans.every((span) => span.textContent === "")).toBe(true);
});

test("digits carry --d and the comma carries data-c, in the value's own order", () => {
  const { container } = render(<RollingNumber value="1,234" />);
  const roll = container.querySelector(".roll") as HTMLElement;
  const kids = [...roll.children] as HTMLElement[];
  expect(kids.map((k) => k.className)).toEqual([
    "roll-digit",
    "roll-sep",
    "roll-digit",
    "roll-digit",
    "roll-digit",
  ]);
  expect(kids.map((k) => k.style.getPropertyValue("--d"))).toEqual(["1", "", "2", "3", "4"]);
  expect(kids[1]?.getAttribute("data-c")).toBe(",");
});

test("the units digit's overlay span keeps its own element from 9 to 10, not a freshly rebuilt strip", () => {
  const { container, rerender } = render(<RollingNumber value={9} />);
  const before = container.querySelectorAll<HTMLElement>(".roll-digit")[0];
  expect(before?.style.getPropertyValue("--d")).toBe("9");
  rerender(<RollingNumber value={10} />);
  const after = [...container.querySelectorAll<HTMLElement>(".roll-digit")];
  expect(after).toHaveLength(2);
  expect(after[0]?.style.getPropertyValue("--d")).toBe("1");
  // The units digit is still last in document order, and must be the SAME element, only updated
  // in place: an implementation that counts keys from the left (or otherwise rebuilds the whole
  // overlay on a digit-count change) would fail this `toBe` (reference equality) even though the
  // textContent checks above would still read correctly.
  expect(after[1]).toBe(before);
  expect(after[1]?.style.getPropertyValue("--d")).toBe("0");
});

// --- RollingNumber.css: jsdom applies no CSS from stylesheets, so the roll and its reduced-motion
// shutoff are pinned against the source text (same technique as every other *.css.unit.test.ts).

const css = readCss(import.meta.url, "RollingNumber.css");

function block(source: string, marker: string, from = 0): string {
  const at = source.indexOf(marker, from);
  if (at < 0) throw new Error(`no "${marker}" in the source`);
  let depth = 0;
  for (let i = source.indexOf("{", at); i < source.length; i++) {
    if (source[i] === "{") depth++;
    else if (source[i] === "}" && --depth === 0) return source.slice(at, i + 1);
  }
  throw new Error(`unbalanced "${marker}" in the source`);
}

function norm(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

test(".roll-host hides its own (real) glyphs but stays the positioning root", () => {
  const rule = norm(block(css, ".roll-host {"));
  expect(rule).toContain("position: relative;");
  expect(rule).toContain("-webkit-text-fill-color: transparent;");
});

test(".roll is painted over the host, repainted visible", () => {
  const rule = norm(block(css, ".roll {"));
  expect(rule).toContain("position: absolute;");
  expect(rule).toContain("inset: 0;");
  expect(rule).toContain("-webkit-text-fill-color: currentColor;");
});

test("::before rolls the 0-9 column over --digit-roll, by the digit's own --d, and stays in flow", () => {
  const rule = norm(block(css, ".roll-digit::before {"));
  expect(rule).toContain("transform: translateY(calc(var(--d) * -1lh));");
  expect(rule).toContain("transition: transform var(--digit-roll)");
  // An absolutely positioned ::before takes the empty .roll-digit out of flow, so it has
  // nothing left to size itself on and collapses to 0 width -- invisible in a real browser,
  // though jsdom has no layout and never catches it. In flow (display: block), the "0..9" column
  // sizes the inline-block to one digit's width instead.
  expect(rule).toContain("display: block;");
  expect(rule).not.toMatch(/position:\s*absolute/);
  expect(rule).not.toMatch(/inset:\s*0/);
});

test(".roll-digit and .roll-sep share vertical-align: top, so a clipped digit lines up with a plain separator", () => {
  const rule = norm(block(css, ".roll-digit,"));
  expect(rule).toContain(".roll-sep");
  expect(rule).toContain("display: inline-block;");
  // A clipped (overflow: hidden) inline-block takes its own bottom edge as its baseline,
  // so without this an unclipped .roll-sep would sit noticeably lower than the digits.
  expect(rule).toContain("vertical-align: top;");
});

test("a separator's ::before paints its own data-c attribute", () => {
  const rule = norm(block(css, ".roll-sep::before {"));
  expect(rule).toContain("content: attr(data-c);");
});

test("the overlay is dropped and the host's real glyphs shown under both reduced-motion triggers", () => {
  const media = norm(block(css, "@media (prefers-reduced-motion: reduce) {"));
  const attrHost = norm(block(css, 'html[data-motion="reduce"] .roll-host {'));
  const attrRoll = norm(block(css, 'html[data-motion="reduce"] .roll {'));
  expect(media).toContain(".roll-host");
  expect(media).toContain("-webkit-text-fill-color: currentColor;");
  expect(media).toMatch(/\.roll\s*\{\s*display:\s*none;/);
  expect(attrHost).toContain("-webkit-text-fill-color: currentColor;");
  expect(attrRoll).toContain("display: none;");
});

test("the overlay is dropped and the host's real glyphs shown under forced colours too", () => {
  const forced = norm(block(css, "@media (forced-colors: active) {"));
  expect(forced).toContain("-webkit-text-fill-color: currentColor;");
  expect(forced).toMatch(/\.roll\s*\{\s*display:\s*none;/);
});
