/**
 * Pins `appearance.css`'s own rules against its source text:
 * jsdom applies no CSS from stylesheets, so these can only be checked by reading the file, same
 * technique as every other `*.css.unit.test.ts` in this app (`../../testing/css`).
 */
import { expect, test } from "vitest";
import { readCss, ruleIn } from "../../testing/css";

const css = readCss(import.meta.url, "appearance.css");

test("root zoom drives the font scale, defaulting to 1 before applyAppearance has ever run", () => {
  expect(ruleIn(css, "html")).toMatch(/zoom:\s*var\(--font-scale,\s*1\)/);
});

test("compact density overrides exactly the five surfaces the record names, each -4px off its own base", () => {
  // Shell.css: `.conversation-scroll { padding: 24px 24px 0 }`.
  expect(ruleIn(css, 'html[data-density="compact"] .conversation-scroll')).toMatch(
    /padding:\s*16px 16px 0/,
  );
  // Conversation.css: `.conversation-list { gap: 18px }` — the record calls this ".turn gap",
  // but `.turn` sets no gap of its own; its parent's `gap` is what actually spaces turns apart.
  expect(ruleIn(css, 'html[data-density="compact"] .conversation-list')).toMatch(/gap:\s*14px/);
  // LeftRail.css: `.sess { padding: 8px 12px }` / `.sess-selected { padding: 7px 11px }`.
  expect(ruleIn(css, 'html[data-density="compact"] .sess')).toMatch(/padding:\s*6px 12px/);
  expect(ruleIn(css, 'html[data-density="compact"] .sess-selected')).toMatch(/padding:\s*5px 11px/);
  // Shell.css: `.composer-slot { padding: 10px 16px 14px }`.
  expect(ruleIn(css, 'html[data-density="compact"] .composer-slot')).toMatch(
    /padding:\s*6px 16px 10px/,
  );
  // RightRail.css: `.rail-section { margin-top: 18px }` — the record calls this "padding", but
  // that property does not exist on `.rail-section`; `margin-top` is the one that does.
  expect(ruleIn(css, 'html[data-density="compact"] .rail-section')).toMatch(/margin-top:\s*14px/);
});

test("reduce-motion stops exactly the four animated surfaces the record names", () => {
  const rule = css.match(/html\[data-motion="reduce"\][^{]*\{[^}]*\}/);
  expect(rule).not.toBeNull();
  const block = rule?.[0] ?? "";
  for (const selector of [".breathe", ".caret", ".rail-drawer", ".rail-drawer-backdrop"]) {
    expect(block).toContain(selector);
  }
  expect(block).toMatch(/animation:\s*none/);
});

test("allow and system leave no [data-motion] rule of their own (neither can cancel the OS query)", () => {
  expect(css).not.toMatch(/data-motion="allow"/);
  expect(css).not.toMatch(/data-motion="system"/);
});

test("nothing outside the five density surfaces and the four motion surfaces is overridden", () => {
  const densitySelectors = css.match(/html\[data-density="compact"\][^{]*\{/g) ?? [];
  expect(densitySelectors).toHaveLength(6);
  const motionSelectors = css.match(/html\[data-motion="reduce"\]/g) ?? [];
  expect(motionSelectors).toHaveLength(4);
});
