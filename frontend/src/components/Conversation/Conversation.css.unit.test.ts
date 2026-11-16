/**
 * Conversation.css facts jsdom cannot see from a render — it applies no CSS from stylesheets
 * — pinned against the source, the way LeftRail.css.unit.test.ts pins its own.
 */
import { expect, test } from "vitest";
import { narrowBlock, readCss, ruleIn } from "../../testing/css";

const css = readCss(import.meta.url, "Conversation.css");
const tokens = readCss(import.meta.url, "../../styles/tokens.css");

test("the fills and borders code blocks use are global, so a plan card's code keeps them", () => {
  // `.md`, `.md-code` and `.md-inline-code` also render inside `.card-plan`, which App mounts as
  // a sibling of `.conversation-list`, never a descendant. While these five were declared on
  // that element, a fenced block in a plan resolved none of them and lost its border and its
  // fill — silently, because an unresolved custom property produces no console error.
  // The colour tokens live in tokens.css's first block, whose selector list starts with :root.
  const root = tokens.match(/:root,\s*\[data-theme="dark"\]\s*\{[^}]*\}/)?.[0];
  for (const name of ["--s0", "--s1", "--b1", "--b2", "--err"]) {
    expect(root).toContain(`${name}:`);
  }
  expect(ruleIn(css, ".conversation-list")).not.toMatch(/--s0|--s1|--b1|--b2|--err/);
});

test("a tool row clips what it cannot fit instead of widening the whole column", () => {
  // `.tool-head` stays `flex: none` — the tool's name is the part that must never truncate — so
  // the column's `overflow-wrap` cannot reach it and it is laid out at its full text width. For
  // Bash and PowerShell that text is the command's first whitespace-delimited token, cut at
  // mapper.py's 160 characters, which for an argv written without spaces is the whole command.
  expect(ruleIn(css, ".tool-head")).toMatch(/flex:\s*none/);
  expect(ruleIn(css, ".tool-row")).toMatch(/overflow:\s*hidden/);
});

test("a block-level jump outlines the row and flashes it once, but not under reduced motion", () => {
  const target = ruleIn(css, ".routing-target");
  expect(target).toMatch(/outline:\s*2px solid var\(--or\)/);
  expect(target).toMatch(/outline-offset:\s*4px/);
  expect(target).toMatch(/border-radius:\s*5px/);
  expect(target).toMatch(/animation:\s*routing-flash 1\.6s ease-out 1/);
  expect(css).toMatch(/@keyframes routing-flash\s*\{/);
  expect(css).toMatch(/rgb\(var\(--or-rgb\)\s*\/\s*0\.18\)/);
  const reduced = narrowBlock(css, "@media (prefers-reduced-motion: reduce)");
  expect(reduced).toMatch(/\.routing-target\s*\{[^}]*animation:\s*none/);
});

test("plain-text markdown keeps the reply's font and wraps like prose", () => {
  // Deep, oversized, slow or not yet parsed text renders as `.md-plain`, a `<pre>`; without
  // these it would switch to the monospace font, keep its margins and run off the column.
  const rule = ruleIn(css, ".md-plain");
  expect(rule).toMatch(/margin:\s*0;/);
  expect(rule).toMatch(/white-space:\s*pre-wrap;/);
  expect(rule).toMatch(/font:\s*inherit;/);
});

test("reply activity has a stable wrapping row and keeps the configured portrait dot static", () => {
  const row = ruleIn(css, ".reply-name-row");
  expect(row).toContain("min-height: 26px");
  expect(row).toContain("height: auto");
  expect(row).toContain("flex-wrap: wrap");
  expect(ruleIn(css, ".reply-has-activity > .face .face-presence")).toContain("animation: none");
});

test("opened routing summaries remove the clamp while disclosure focus remains visible", () => {
  const open = ruleIn(css, ".routing-summary--open");
  expect(open).toContain("display: block");
  expect(open).toContain("-webkit-line-clamp: unset");
  expect(open).toContain("white-space: pre-wrap");
  expect(ruleIn(css, ".routing-summary-toggle:focus-visible")).toContain("outline: 2px solid");
  expect(narrowBlock(css, "@media (forced-colors: active)")).toContain(
    ".routing-summary-toggle:focus-visible",
  );
});
