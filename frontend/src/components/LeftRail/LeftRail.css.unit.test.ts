import { expect, test } from "vitest";
import { narrowBlock, readCss, ruleIn } from "../../testing/css";

// jsdom applies no CSS from stylesheets, so this checks the source (same technique as
// RightRail.test.tsx's existing --g0 / --routines checks).
const css = readCss(import.meta.url, "LeftRail.css");

function rule(selector: string): string {
  const match = css.match(new RegExp(`${selector.replace(/[.[\]]/g, "\\$&")}\\s*\\{[^}]*\\}`));
  if (!match) throw new Error(`selector not found in LeftRail.css: ${selector}`);
  return match[0];
}

test(".sess stacks title over meta as the design does, instead of a flex row with the time on the right", () => {
  const sess = rule(".sess");
  expect(sess).not.toMatch(/display:\s*flex/);
  expect(sess).not.toMatch(/justify-content/);
  // .t keeps no flex sizing now that .sess is not a flex container, but stays block so its
  // ellipsis (overflow/text-overflow) still has a box to truncate against.
  const t = rule(".sess .t");
  expect(t).not.toMatch(/flex:/);
  expect(t).toMatch(/display:\s*block/);
  // .m keeps its own flex layout for the breathing dot + text.
  const m = rule(".sess .m");
  expect(m).toMatch(/display:\s*flex/);
});

test("navigation selection, focus and the short-window session area remain distinct", () => {
  expect(ruleIn(css, '.rail-nav-button[aria-current="page"]')).toMatch(
    /border-color:\s*var\(--line\)/,
  );
  expect(ruleIn(css, ".left-rail")).toMatch(/overflow-y:\s*auto/);
  expect(ruleIn(css, ".rail-sessions")).toMatch(/min-height:\s*80px/);
  expect(ruleIn(css, ".rail-sessions")).toMatch(/overflow-y:\s*auto/);
  const focus = ".rail-search:focus-visible,\n.sess:focus-visible,\n.sess-other:focus-visible";
  expect(ruleIn(css, focus)).toMatch(/outline:\s*2px solid/);
  const forced = narrowBlock(css, "@media (forced-colors: active)");
  expect(ruleIn(forced.replace(/^[ \t]+/gm, ""), focus)).toMatch(/outline-color:\s*Highlight/);
});
