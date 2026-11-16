/**
 * The composer at S (below 800px), pinned against the source: jsdom applies no CSS from
 * stylesheets and lays nothing out, so no render test can catch a pill whose contents overflow
 * it. What the composer LOSES at S is checked here; that it still fits, and that the Stop pill
 * and the model chip stay inside it, is measured in real Edge by e2e/scenarios/shell/narrow-layout.test.ts and
 * e2e/scenarios/shell/narrow-app.test.ts.
 */
import { expect, test } from "vitest";
import { narrowBlock, readCss, ruleIn } from "../../testing/css";

const composer = readCss(import.meta.url, "Composer.css");
const chip = readCss(import.meta.url, "ModelChip.css");
const excerpts = readCss(import.meta.url, "ExcerptChips.css");

test("at S the pill drops its decoration and keeps a 120px floor for the text", () => {
  const narrow = narrowBlock(composer);
  // The plus mark and the mic are aria-hidden in Composer.tsx and do nothing when clicked; they
  // are the only things in the pill that can go without taking a control with them.
  expect(narrow).toMatch(/\.composer-icon,\s*\.composer-mic\s*\{\s*display:\s*none/);
  expect(ruleIn(narrow, ".composer-box")).toMatch(/min-width:\s*120px/);
  expect(ruleIn(narrow, ".composer")).toMatch(/padding:\s*0 8px 0 12px/);
});

test("at S the Stop pill drops the Esc hint, not the Stop button", () => {
  const narrow = narrowBlock(composer);
  expect(narrow).toMatch(/\.composer-stop \.kbd\s*\{\s*display:\s*none/);
  // Nothing hides the pill itself, and Esc goes on working: the hint was a reminder, not the key.
  expect(narrow).not.toMatch(/\.composer-stop\s*\{[^}]*display:\s*none/);
  expect(narrow).not.toMatch(/\.composer-submit\s*\{[^}]*display:\s*none/);
});

test("at S the model chip truncates at 110px and keeps its whole label", () => {
  const narrow = narrowBlock(chip);
  const rule = ruleIn(narrow, ".model-chip");
  expect(rule).toMatch(/max-width:\s*110px/);
  expect(rule).toMatch(/text-overflow:\s*ellipsis/);
  expect(rule).toMatch(/overflow:\s*hidden/);
  // The ellipsis is the browser's, drawn over the same text node: nothing here shortens the
  // string, so the button's accessible name is still the model's full label.
  expect(narrow).not.toMatch(/content:/);
});

test("the design's own pill metrics are untouched above 800px", () => {
  expect(ruleIn(composer, ".composer")).toMatch(/max-width:\s*760px/);
  expect(ruleIn(composer, ".composer")).toMatch(/min-height:\s*56px/);
  expect(ruleIn(composer, ".composer")).toMatch(/padding:\s*0 10px 0 14px/);
  expect(ruleIn(chip, ".model-chip")).toMatch(/white-space:\s*nowrap/);
});

test("excerpt refs wrap without clipping and diagnostics stay within their card", () => {
  const label = ruleIn(excerpts, ".excerpt-chip-label");
  expect(label).toMatch(/overflow-wrap:\s*anywhere/);
  expect(label).not.toMatch(/white-space:\s*nowrap|overflow:\s*hidden/);
  expect(ruleIn(excerpts, ".excerpt-chip-details")).toMatch(/min-width:\s*0/);
  expect(ruleIn(excerpts, ".excerpt-chip-diagnostics dd")).toMatch(/overflow-wrap:\s*anywhere/);
});
