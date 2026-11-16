/**
 * Reading a component's own stylesheet from a test.
 *
 * jsdom applies no CSS from stylesheets — only inline styles — so a layout rule can only be
 * pinned by checking the source text. Nine test files were doing that by building the path from
 * `process.cwd()`, which silently means "wherever vitest happened to be invoked from": run the
 * suite with `--root` elsewhere, or copy a file into a probe directory to check whether a fix
 * really bites, and every one of them dies with an unrelated ENOENT (G3B-9).
 *
 * `here` is the caller's own `import.meta.url`, so the path is anchored to the test file itself.
 * It must arrive as a variable: `new URL("...", import.meta.url)` written inline is Vite's own
 * asset-URL pattern, rewritten at build time into a dev-server URL rather than a real path.
 *
 * Node's modules are imported once, here, rather than in nine files: this tsconfig is
 * browser-facing and carries no @types/node, though Node itself is present because Vitest runs
 * tests through it. Nothing outside a test imports this file, so it never reaches the bundle.
 */
// @ts-expect-error no @types/node in this browser-focused tsconfig; see above
import { readFileSync } from "node:fs";
// @ts-expect-error see above
import { fileURLToPath } from "node:url";

/** The source of a stylesheet sitting beside the caller, e.g. readCss(import.meta.url, "X.css"). */
export function readCss(here: string, name: string): string {
  return readFileSync(fileURLToPath(new URL(name, here)), "utf-8") as string;
}

/** The query every narrow ("S") block in this app is written with.
 *
 * A negation rather than `(max-width: 799px)`, so the CSS and useLayoutMode's
 * `(min-width: 800px)` cannot disagree about a fractional width: an 879px window at 110% zoom
 * reports about 799.1 CSS px, where `max-width: 799px` is false and `min-width: 800px` is false
 * too — the rails would be drawers while the tabs, the wide gutters, the composer's decoration
 * and the 50px card indent all stayed. `not all and (...)` rather than Level 4's bare `not (...)`
 * for the same reason the rest of this app avoids new syntax: it is the form every engine has
 * parsed for twenty years. */
export const NARROW_QUERY = "@media not all and (min-width: 800px)";

/** The body of a stylesheet's `query` block (the narrow one by default), or a throw naming what
 * was missing. Read by balancing braces rather than by regex: `[^}]*` stops at the first nested
 * rule's own brace. One copy here rather than one per test file — three brace parsers would
 * drift apart. */
export function narrowBlock(source: string, query: string = NARROW_QUERY): string {
  const start = source.indexOf(query);
  if (start < 0) throw new Error(`no "${query}" block in the stylesheet`);
  let depth = 0;
  for (let i = source.indexOf("{", start); i < source.length; i++) {
    if (source[i] === "{") depth++;
    else if (source[i] === "}" && --depth === 0) return source.slice(start, i + 1);
  }
  throw new Error(`unbalanced "${query}" block in the stylesheet`);
}

/** The whole `selector { ... }` rule, or a throw naming what was missing. */
export function ruleIn(css: string, selector: string): string {
  const escaped = selector.replace(/[.:>()[\]]/g, "\\$&");
  const match = css.match(new RegExp(`${escaped}\\s*\\{[^}]*\\}`));
  if (!match) throw new Error(`no ${selector} rule in the stylesheet`);
  return match[0];
}
