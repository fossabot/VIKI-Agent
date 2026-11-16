/**
 * Pins Loading.css's and tokens.css's own values against their source text (jsdom applies no
 * CSS from stylesheets — same technique as every other `*.css.unit.test.ts` in this app,
 * ../../testing/css).
 */
// @ts-expect-error no @types/node in this browser-focused tsconfig; see ../../testing/css.ts
import { readFileSync } from "node:fs";
// @ts-expect-error see above
import { fileURLToPath } from "node:url";
import { expect, test } from "vitest";
import { readCss } from "../../testing/css";
import { MIN_VISIBLE_MS, SHOW_DELAY_MS } from "./Loading";

const tokens = readCss(import.meta.url, "../../styles/tokens.css");
const css = readCss(import.meta.url, "Loading.css");

/** The brace-balanced body of the first `marker {` block, searched from `from`: the same
 * technique as styles/tokens.unit.test.ts's findBlock, kept local since this file's markers
 * (media queries, grouped selectors) differ from that one's `[data-theme]` blocks. */
function block(source: string, marker: string, from = 0): string {
  const at = source.indexOf(marker, from);
  if (at < 0) throw new Error(`no "${marker}" in the source`);
  let depth = 0;
  for (let i = source.indexOf("{", at); i < source.length; i++) {
    if (source[i] === "{") depth++;
    else if (source[i] === "}" && --depth === 0) return source.slice(at, i + 1);
  }
  throw new Error(`unbalanced "${marker}" in the source`);
}

/** A block's text with all whitespace runs collapsed to one space, so assertions do not depend
 * on exactly how the formatter wrapped or indented a declaration. */
function norm(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

function tokenValue(name: string): string {
  const m = new RegExp(`${name}:\\s*([^;]+);`).exec(tokens);
  if (!m) throw new Error(`no ${name} in tokens.css`);
  return (m[1] as string).trim();
}

// --- tokens.css: the loading kit's own motion tokens, pinned to the design's constants ---

test("the motion tokens hold the design record's values", () => {
  const expected: Record<string, string> = {
    "--loading-delay": "300ms",
    "--motion-fast": "150ms",
    "--motion-base": "300ms",
    "--motion-content": "220ms",
    "--motion-ease": "cubic-bezier(0.33, 0, 0.67, 1)",
    "--motion-ease-out": "cubic-bezier(0.2, 0.9, 0.25, 1)",
    "--skeleton-sweep": "2s",
    "--spinner-lap": "1.5s",
    "--spinner-lap-reduced": "1.8s",
    "--shimmer-sweep": "2s",
    "--digit-roll": "520ms",
    "--progress-height": "6px",
  };
  for (const [name, value] of Object.entries(expected)) {
    expect(tokenValue(name), name).toBe(value);
  }
});

test("--loading-delay equals SHOW_DELAY_MS, and the hold window is SHOW_DELAY_MS + MIN_VISIBLE_MS", () => {
  expect(tokenValue("--loading-delay")).toBe(`${SHOW_DELAY_MS}ms`);
  expect(SHOW_DELAY_MS + MIN_VISIBLE_MS).toBe(700);
});

// --- Loading.tsx imports its own stylesheet ---

test("Loading.tsx imports ./Loading.css", () => {
  // `here` must be a variable, not `import.meta.url` written inline in `new URL(...)`: see the
  // doc comment on ../../testing/css.ts's `readCss` for why the inline form breaks under Vite.
  const here = import.meta.url;
  const tsx = readFileSync(fileURLToPath(new URL("Loading.tsx", here)), "utf-8") as string;
  expect(tsx).toMatch(/import\s+["']\.\/Loading\.css["']/);
});

// --- the check mark: no bare ".check" (collides with Workbench/Checks.css's own ".check") ---

test("no selector in Loading.css is the bare .check class", () => {
  expect(css).not.toMatch(/\.check(?!-)/);
});

// --- the spinner: a growing arc, laps on --spinner-lap, eased by --motion-ease ---

test("the spinner turns and its arc dashes over --spinner-lap, eased by --motion-ease", () => {
  const svgRule = norm(block(css, ".spinner svg {"));
  expect(svgRule).toContain("--lap: var(--spinner-lap);");
  expect(svgRule).toContain("animation: spinner-turn var(--lap) linear infinite;");
  const arcRule = norm(block(css, ".spinner-arc {"));
  expect(arcRule).toContain("animation: spinner-dash var(--lap) var(--motion-ease) infinite;");
});

test("the dash keyframe grows 1 to 22 and back to 1, of 40", () => {
  const dash = norm(block(css, "@keyframes spinner-dash {"));
  expect(dash).toContain("stroke-dasharray: 1 40;");
  expect(dash).toContain("stroke-dasharray: 22 40;");
});

test("the spinner never turns off under reduced motion in either trigger, only slower", () => {
  const media = norm(block(css, "@media (prefers-reduced-motion: reduce) {"));
  const attr = norm(block(css, 'html[data-motion="reduce"] .spinner svg {'));
  for (const rule of [media, attr]) {
    expect(rule).toContain("--lap: var(--spinner-lap-reduced);");
  }
  expect(media).not.toMatch(/\.spinner svg \{[^}]*animation:\s*none/);
  expect(attr).not.toMatch(/animation:\s*none/);
});

// --- the skeleton: a 2 s Primer mask sweep, static (no mask) under reduced motion ---

test("the skeleton is an A2 mask sweep over --skeleton-sweep (2 s)", () => {
  expect(tokenValue("--skeleton-sweep")).toBe("2s");
  const skel = norm(block(css, ".skel {"));
  expect(skel).toContain("background: var(--fill-raised);");
  expect(skel).toContain("mask-size: 200%;");
  expect(skel).toContain("animation: skel-sweep var(--skeleton-sweep) ease infinite;");
});

test("the skeleton's mask carries no literal colour, only alpha via color-mix", () => {
  const skel = block(css, ".skel {");
  expect(skel).not.toMatch(/#[0-9a-fA-F]{3,8}/);
  expect(skel).not.toMatch(/\brgba?\(\s*[-+.\d]/);
  expect(skel).toContain("color-mix(in srgb, black 65%, transparent)");
});

test("the skeleton goes fully static (no mask, no animation) under both reduced-motion triggers", () => {
  const media = norm(block(css, "@media (prefers-reduced-motion: reduce) {"));
  const attr = norm(block(css, 'html[data-motion="reduce"] .skel {'));
  expect(media).toMatch(/\.skel \{[^}]*animation:\s*none;[^}]*mask-image:\s*none;/);
  expect(attr).toContain("animation: none;");
  expect(attr).toContain("mask-image: none;");
});

test("the skeleton stays visible under forced colours, outlined in CanvasText", () => {
  const forced = norm(block(css, "@media (forced-colors: active) {"));
  expect(forced).toContain("outline: 1px solid CanvasText;");
});

// --- the delayed reveal: .loading / .reveal-late / .spinner-late share one fade ---

test("the three reveal classes fade in together over --motion-fast, after --loading-delay, filling both ends", () => {
  const group = norm(block(css, ".loading,", css.indexOf(".loading,")));
  expect(group).toContain(".reveal-late");
  expect(group).toContain(".spinner-late");
  expect(group).toContain(
    "animation: loading-reveal var(--motion-fast) var(--motion-ease-out) var(--loading-delay) both;",
  );
});

test("the reveal keeps its delay but drops to a 1ms animation under both reduced-motion triggers", () => {
  const media = norm(block(css, "@media (prefers-reduced-motion: reduce) {"));
  const attrStart = css.indexOf('html[data-motion="reduce"] .loading,');
  const attr = norm(block(css, 'html[data-motion="reduce"] .loading,', attrStart));
  for (const rule of [media, attr]) {
    expect(rule).toContain(".reveal-late");
    expect(rule).toContain(".spinner-late");
    expect(rule).toContain("animation-duration: 1ms;");
  }
});
