import { expect, test } from "vitest";
import { readCss, ruleIn } from "../../testing/css";

const css = readCss(import.meta.url, "Pages.css");

test("this file never redeclares base.css's .sr-only utility", () => {
  expect(css).not.toMatch(/\.sr-only\s*\{/);
  expect(css).not.toMatch(/\.visually-hidden\s*\{/);
});

test("this file never redeclares Settings.css's card chrome", () => {
  expect(css).not.toMatch(/\.settings-card\s*\{/);
  expect(css).not.toMatch(/\.settings-facts\s*\{/);
  expect(css).not.toMatch(/\.settings-pill\s*\{/);
});

test("a long registered root wraps at any character, at any width", () => {
  const rule = ruleIn(css, ".workspace-root");
  expect(rule).toMatch(/font-size:\s*12px/);
  expect(rule).toMatch(/color:\s*var\(--t2\)/);
  expect(rule).toMatch(/overflow-wrap:\s*anywhere/);
});

test("the layout and source labels are joined by a CSS-drawn separator, never a literal character", () => {
  expect(ruleIn(css, ".workspace-source::before")).toMatch(/content:\s*" · "/);
});

test("policy markers lay out as a grid checklist", () => {
  const rule = ruleIn(css, ".policy-markers");
  expect(rule).toMatch(/display:\s*grid/);
});

test("a present marker's ring fills with --or; an absent one dims its text", () => {
  const absent = ruleIn(css, 'li[data-present="false"]');
  expect(absent).toMatch(/color:\s*var\(--t3\)/);
  const present = ruleIn(css, 'li[data-present="true"] .policy-marker-mark');
  expect(present).toMatch(/border-color:\s*var\(--or\)/);
  expect(present).toMatch(/background:\s*var\(--or\)/);
});

test("the policy panel is a top rule, never a boxed or indented aside", () => {
  const rule = ruleIn(css, ".policy-panel");
  expect(rule).toMatch(/border-top:\s*1px solid var\(--hair\)/);
  expect(rule).not.toMatch(/margin/);
  expect(rule).not.toMatch(/padding/);
  expect(rule).not.toMatch(/border-radius/);
  expect(rule).not.toMatch(/border:\s*1px solid/);
});

test("a refused policy control dims even outside .settings-page, for the onboarding host", () => {
  const rule = ruleIn(css, '.policy-panel [aria-disabled="true"]');
  expect(rule).toMatch(/opacity:\s*0\.5/);
});

test("the policy form fieldset is unboxed; its legend is a quiet 12px title", () => {
  const fieldset = ruleIn(css, ".policy-form");
  expect(fieldset).toMatch(/border:\s*none/);
  // The top margin keeps it off whatever comes before it (often the identity facts grid).
  expect(fieldset).toMatch(/margin:\s*8px 0 0/);
  const legend = ruleIn(css, ".policy-form legend");
  expect(legend).toMatch(/font-size:\s*12px/);
  expect(legend).toMatch(/color:\s*var\(--t2\)/);
});

test("a chosen policy target reads --or, under forced colours too", () => {
  const pressed = ruleIn(css, '.policy-targets button[aria-pressed="true"]');
  expect(pressed).toMatch(/border-color:\s*var\(--or\)/);
  expect(pressed).toMatch(/background:\s*var\(--fill-hi\)/);
  expect(css).toContain("@media (forced-colors: active)");
  const forcedStart = css.indexOf("@media (forced-colors: active)");
  const forced = css.slice(forcedStart);
  expect(ruleIn(forced, '.policy-targets button[aria-pressed="true"]')).toMatch(
    /border-color:\s*Highlight/,
  );
});
