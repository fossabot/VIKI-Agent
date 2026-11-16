/**
 * Data.css facts jsdom cannot see from a render: the narrow block turns every
 * `.data-table` into stacked cards labelled by `data-label`, and the reset confirmation is a
 * centred fixed dialog, not the toast shell.
 */
import { expect, test } from "vitest";
import { narrowBlock, readCss, ruleIn } from "../../testing/css";

const css = readCss(import.meta.url, "Data.css");

test("the narrow block turns tables into stacked, labelled blocks", () => {
  const narrow = narrowBlock(css);
  expect(narrow).toMatch(/\.data-table[\s\S]*\{[\s\S]*display:\s*block/);
  expect(narrow).toContain("content: attr(data-label)");
});

test("the reset dialog is centred and fixed, independent of the toast shell", () => {
  const dialog = ruleIn(css, ".reset-dialog");
  expect(dialog).toMatch(/position:\s*fixed/);
  expect(dialog).toMatch(/left:\s*50%/);
});

test("a long path or sha256 breaks instead of widening the view", () => {
  expect(ruleIn(css, ".data-table td")).toMatch(/overflow-wrap:\s*anywhere/);
  expect(ruleIn(css, ".data-export-result dd")).toMatch(/overflow-wrap:\s*anywhere/);
});

test("the fixed dialog is clamped against the window, not against the zoomed page", () => {
  // Measured in Edge: without the divisor, 400px at font scale 1.5 rendered a 552px dialog whose
  // confirm button sat 76px past the window edge, with `position: fixed` leaving nothing to scroll.
  const dialog = ruleIn(css, ".reset-dialog");
  expect(dialog).toMatch(/max-width:\s*calc\(\(100vw - 32px\) \/ var\(--font-scale, 1\)\)/);
  expect(dialog).toMatch(/max-height:\s*calc\(\(100vh - 32px\) \/ var\(--font-scale, 1\)\)/);
  expect(dialog).toMatch(/overflow-y:\s*auto/);
});
