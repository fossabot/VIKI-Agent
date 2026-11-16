/**
 * Dictation.css at S (below 800px), pinned against the source: jsdom applies no CSS
 * from stylesheets. That a ready mic and Send fit inside the pill is measured in real Edge by the
 * e2e files; what is checked here is which rules hide the mic.
 */
import { expect, test } from "vitest";
import { narrowBlock, readCss, ruleIn } from "../testing/css";

const css = readCss(import.meta.url, "Dictation.css");

test("at S only the unready mic hides", () => {
  const narrow = narrowBlock(css);
  expect(ruleIn(narrow, ".voice-mic-unready")).toMatch(/display:\s*none/);
  expect(narrow.match(/display:\s*none/g)).toHaveLength(1);
});

test("at S the ready mic stays", () => {
  const narrow = narrowBlock(css);
  expect(narrow).not.toMatch(/\.voice-mic(?![-\w])/);
  expect(narrow).not.toMatch(/\.voice-mic-ready/);
  expect(css).not.toMatch(/\.voice-mic(-ready)?\s*\{[^}]*display:\s*none/);
});

test("the button is the 32px round control, red while pressed", () => {
  const rule = ruleIn(css, ".voice-mic");
  expect(rule).toMatch(/width:\s*32px/);
  expect(rule).toMatch(/height:\s*32px/);
  expect(rule).toMatch(/border-radius:\s*50%/);
  expect(ruleIn(css, '.voice-mic[aria-pressed="true"]')).toMatch(/var\(--err\)/);
});

test("the strip keeps the blocked line's geometry and wraps", () => {
  const rule = ruleIn(css, ".voice-strip");
  expect(rule).toMatch(/max-width:\s*760px/);
  expect(rule).toMatch(/margin:\s*0 auto 6px/);
  expect(rule).toMatch(/flex-wrap:\s*wrap/);
});
