/**
 * HandsFree.css facts pinned against the source (jsdom applies no CSS from stylesheets): every
 * `var(--x)` it declares resolves to a token tokens.css actually defines — the gap that let
 * `.hands-free-toggle`'s border reach a nonexistent `--bd` and silently drop — plus the
 * toggle's visible pressed state, that no row wrapper (`hands-free-toggle-row`) remains,
 * that its tooltip starts hidden, and that the S rule hides only a non-primary control.
 */
import { expect, test } from "vitest";
import { narrowBlock, readCss, ruleIn } from "../testing/css";

const css = readCss(import.meta.url, "HandsFree.css");
const tokens = readCss(import.meta.url, "../styles/tokens.css");

function customPropsIn(text: string): Set<string> {
  const names = new Set<string>();
  for (const m of text.matchAll(/(--[\w-]+)/g)) names.add(m[1] as string);
  return names;
}

test("every var(--x) HandsFree.css uses names a token tokens.css defines", () => {
  const used = new Set<string>();
  for (const m of css.matchAll(/var\((--[\w-]+)\)/g)) used.add(m[1] as string);
  const defined = customPropsIn(tokens);
  const missing = [...used].filter((name) => !defined.has(name));
  expect(missing).toEqual([]);
});

test("no row wrapper remains, the tip is hidden by default, and the S rule hides only a non-primary control", () => {
  expect(css).not.toMatch(/hands-free-toggle-row/);
  expect(ruleIn(css, ".hands-free-tip")).toMatch(/display:\s*none/);
  expect(ruleIn(narrowBlock(css), '.hands-free-voice[data-primary="false"]')).toMatch(
    /display:\s*none/,
  );
});

test("the toggle's pressed state is visibly distinct (border and text colour both change)", () => {
  const rule = ruleIn(css, '.hands-free-toggle[aria-pressed="true"]');
  expect(rule).toMatch(/border-color:\s*var\(--err\)/);
  expect(rule).toMatch(/color:\s*var\(--err\)/);
});
