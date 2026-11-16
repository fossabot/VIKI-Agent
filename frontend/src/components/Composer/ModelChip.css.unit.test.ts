/**
 * ModelChip.css's focus indicators, pinned against the source: jsdom applies no CSS from
 * stylesheets, so a render cannot see them (same technique as LeftRail.css.unit.test.ts).
 *
 * The chip suppresses three rings of its own — `all: unset` on the trigger, `outline: none` on
 * the model rows and on the effort segments — and for a while replaced none of them, which left
 * a keyboard user tabbing to the one control that picks the model and the effort, opening it and
 * arrowing through both lists with nothing moving on screen.
 */
import { expect, test } from "vitest";
import { narrowBlock, readCss, ruleIn } from "../../testing/css";

const css = readCss(import.meta.url, "ModelChip.css");

test("long labels are constrained at desktop widths and the model menu scrolls within its viewport", () => {
  expect(ruleIn(css, ".model-chip")).toMatch(/max-width:/);
  expect(ruleIn(css, ".model-chip")).toMatch(/text-overflow:\s*ellipsis/);
  expect(ruleIn(css, ".chip-menu")).toMatch(/max-height:/);
  expect(ruleIn(css, ".chip-menu")).toMatch(/overflow-y:\s*auto/);
  expect(ruleIn(css, ".chip-model-label")).toMatch(/text-overflow:\s*ellipsis/);
});

test("the trigger draws a ring when it is tabbed to, which `all: unset` would otherwise remove", () => {
  expect(ruleIn(css, ".model-chip:focus-visible")).toMatch(/outline:\s*1px solid var\(--or\)/);
});

test("both kinds of menu row highlight, keyed off Radix's attribute rather than :focus-visible", () => {
  // Radix moves focus itself, for arrow keys and pointermove alike, which `:focus-visible` need
  // not match — the reason RowMenu.css gives for keying off the attribute.
  expect(ruleIn(css, ".chip-menu .mi[data-highlighted]")).toMatch(/background:\s*var\(--fill-hi\)/);
  expect(ruleIn(css, ".chip-effort[data-highlighted]")).toMatch(/outline:\s*1px solid var\(--or\)/);
});

test("neither highlight can be mistaken for the model and effort actually in effect", () => {
  // The row highlight leaves `.mi.on`'s inset orange bar alone, and the effort highlight uses
  // the outline channel rather than `.on`'s inset ring: two rows lit identically would leave the
  // user unable to tell which one Enter commits.
  expect(ruleIn(css, ".chip-menu .mi[data-highlighted]")).not.toMatch(/box-shadow/);
  expect(ruleIn(css, ".chip-menu .mi.on")).toMatch(/box-shadow:\s*inset 2px 0 0 var\(--or\)/);
  expect(ruleIn(css, ".chip-effort[data-highlighted]")).not.toMatch(/box-shadow/);
});

test("under forced colours, the in-effect row keeps a distinct mark from the highlighted row", () => {
  // The fills and the inset bar that told the two states apart are gone under forced colours, so
  // the block needs its own way to keep them apart: a row that is both shows the highlighted
  // row's solid outline, because that rule comes after the in-effect row's dashed one.
  const forced = narrowBlock(css, "@media (forced-colors: active)");
  expect(ruleIn(forced, ".chip-menu .mi.on")).toMatch(/outline:\s*2px dashed Highlight/);
  expect(ruleIn(forced, ".chip-menu .mi[data-highlighted]")).toMatch(
    /outline:\s*2px solid Highlight/,
  );
  // The effort in effect draws as a border, not an outline: `.chip-effort[data-highlighted]`
  // (above, outside this block) keeps the one outline rule for this control, so an outline here
  // would win the cascade (same specificity, later in the sheet) and hide the highlight ring
  // when the two coincide.
  expect(ruleIn(forced, ".chip-effort.on")).toMatch(/border:\s*2px solid Highlight/);
  expect(ruleIn(forced, ".chip-effort.on")).not.toMatch(/outline/);
  expect(forced.indexOf(".chip-menu .mi.on")).toBeLessThan(
    forced.indexOf(".chip-menu .mi[data-highlighted]"),
  );
  // No later rule for these three selectors follows the forced-colours block.
  const after = css.slice(css.indexOf(forced) + forced.length);
  for (const selector of [
    ".chip-menu .mi.on",
    ".chip-menu .mi[data-highlighted]",
    ".chip-effort.on",
  ]) {
    expect(after).not.toContain(selector);
  }
});
