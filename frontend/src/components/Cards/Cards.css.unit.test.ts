/**
 * Cards.css facts jsdom cannot see from a render (it applies no CSS from stylesheets), pinned
 * against the source the way LeftRail.css.unit.test.ts pins its own.
 */
import { expect, test } from "vitest";
import { readCss, ruleIn } from "../../testing/css";

const css = readCss(import.meta.url, "Cards.css");

test("a card's chip truncates a long header instead of pushing the card past its column", () => {
  // The chip carries AskUserQuestion's own `header`, which the server passes through uncut
  // (driver.py's `_questions`), and base.css's `.chip` is `white-space: nowrap` at a fixed 20px:
  // wrapping cannot help it, and a flex item's automatic minimum size is its content's until its
  // overflow stops being visible — so without this it refuses to shrink and overflows.
  expect(ruleIn(css, ".card-head .chip")).toMatch(/overflow:\s*hidden/);
});

test("the command box keeps the command's own line breaks, wraps and scrolls rather than clipping", () => {
  // A shell reads a line break as the end of a command: collapsed to a space, a second command
  // reads as arguments of the first, or as part of the comment before it.
  const box = ruleIn(css, ".card-tool");
  expect(box).toMatch(/white-space:\s*pre-wrap/);
  expect(box).toMatch(/overflow-wrap:\s*anywhere/);
  expect(box).toMatch(/max-height:\s*\d+px/);
  expect(box).toMatch(/overflow-y:\s*auto/);
  expect(box).not.toMatch(/nowrap|text-overflow|overflow:\s*hidden/);
});

test("a written-out code is marked with the palette's own contrast pair, and in forced colors", () => {
  const notes = readCss(import.meta.url, "DecisionText.css");
  const code = ruleIn(notes, ".hidden-code");
  expect(code).toMatch(/background:\s*var\(--or\)/);
  expect(code).toMatch(/color:\s*var\(--on-or\)/);
  expect(notes).toMatch(
    /@media\s*\(forced-colors:\s*active\)[\s\S]*?\.hidden-code\s*\{[^}]*background:\s*Mark;[^}]*color:\s*MarkText/s,
  );
});

test("target cards keep a static outline and flash only when motion is allowed", () => {
  expect(ruleIn(css, ".card-target")).toMatch(/outline:\s*2px solid var\(--or\)/);
  expect(ruleIn(css, ".card-target")).toMatch(/animation:\s*card-target-flash\b/);
  expect(css).toMatch(/@keyframes\s+card-target-flash\s*\{/);
  expect(css).toMatch(
    /@media\s*\(prefers-reduced-motion:\s*reduce\)[\s\S]*?\.card-target\s*\{[^}]*animation:\s*none/s,
  );
  expect(css).toMatch(/html\[data-motion="reduce"\]\s*\.card-target\s*\{[^}]*animation:\s*none/);
});

test("forced colors retain a visible target outline", () => {
  expect(css).toMatch(
    /@media\s*\(forced-colors:\s*active\)[\s\S]*?\.card-target\s*\{[^}]*outline:\s*2px solid Highlight/s,
  );
});
