/**
 * Pins `Avatar.css`'s accent rules against its source text: jsdom applies no CSS from
 * stylesheets, so these can only be checked by reading the file, same technique as every other
 * `*.css.unit.test.ts` in this app (`../../testing/css`).
 */
import { expect, test } from "vitest";
import { ACCENTS } from "../../clients/profiles/profileApi";
import { readCss, ruleIn } from "../../testing/css";

const css = readCss(import.meta.url, "Avatar.css");

for (const accent of ACCENTS) {
  test(`the ${accent} accent reads its own tokens.css token`, () => {
    const rule = ruleIn(css, `.avatar[data-accent="${accent}"]`);
    expect(rule).toMatch(new RegExp(`--avatar-accent:\\s*var\\(--accent-${accent}\\)`));
  });
}

test("a color avatar shows its image without the grayscale filter; every other stays grayscale", () => {
  expect(ruleIn(css, ".avatar img")).toMatch(/filter:\s*grayscale\(1\)/);
  expect(ruleIn(css, '.avatar[data-color="true"] img')).toMatch(/filter:\s*none/);
});

test("the avatar opts out of forced colors so its accent stays visible", () => {
  expect(ruleIn(css, ".avatar")).toMatch(/forced-color-adjust:\s*none/);
});
