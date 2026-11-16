/**
 * Pins AvatarEditor.css's preview box model against its source text: jsdom applies no CSS from
 * stylesheets, so this can only be checked by reading the file, same technique as every other
 * `*.css.unit.test.ts` in this app (`../../testing/css`).
 */
import { expect, test } from "vitest";
import { readCss, ruleIn } from "../../testing/css";

const css = readCss(import.meta.url, "AvatarEditor.css");

test("the preview box keeps its border outside the 256x256 content area", () => {
  expect(ruleIn(css, ".avatar-editor-preview")).toMatch(/box-sizing:\s*content-box/);
});

test("the preview box lets the crop frame's focus outline show past its edge", () => {
  expect(ruleIn(css, ".avatar-editor-preview")).toMatch(/overflow:\s*visible/);
});

test("the preview canvas has no inline baseline gap", () => {
  expect(ruleIn(css, ".avatar-editor-preview canvas")).toMatch(/display:\s*block/);
});
