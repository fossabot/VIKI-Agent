// @vitest-environment node
/**
 * Ratchet: a selector inside a `prefers-reduced-motion` media block has a twin rule under
 * `html[data-motion="reduce"]`, so the Appearance setting reaches everything the OS query does.
 */
// @ts-expect-error no @types/node in this browser-focused tsconfig; see ../testing/css.ts
import { readdirSync, readFileSync } from "node:fs";
// @ts-expect-error see above
import { fileURLToPath } from "node:url";
import { expect, test } from "vitest";

// Read through Node's fs: under Vitest `import.meta.glob(..., { query: "?raw" })` yields empty
// strings for `.css` files, which would pass every ratchet vacuously.
const here = import.meta.url;
const ROOT = fileURLToPath(new URL("../", here)) as string;
const SHEETS: Record<string, string> = Object.fromEntries(
  (readdirSync(ROOT, { recursive: true }) as string[])
    .filter((file) => file.endsWith(".css"))
    .map((file) => [
      `/src/${file.replaceAll("\\", "/")}`,
      readFileSync(ROOT + file, "utf-8") as string,
    ]),
);

const PREFIX = 'html[data-motion="reduce"] ';
// The boolean form `(prefers-reduced-motion)` matches when motion is reduced.
const MOTION = /prefers-reduced-motion\s*(\)|:\s*(reduce|no-preference))/i;
const ROOT_TWIN = 'html[data-motion="reduce"]';

type Rule = { selectors: string[]; inMotionQuery: boolean };

const squash = (text: string) => text.replace(/\s+/g, " ").trim();

/** Split on commas outside parentheses and brackets. */
function splitList(text: string): string[] {
  const parts: string[] = [];
  let depth = 0;
  let start = 0;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (ch === "(" || ch === "[") depth++;
    else if (ch === ")" || ch === "]") depth--;
    else if (ch === "," && depth === 0) {
      parts.push(text.slice(start, i));
      start = i + 1;
    }
  }
  parts.push(text.slice(start));
  return parts.map(squash).filter(Boolean);
}

/** The style rules of a sheet, descending into `@media`, `@supports`, `@layer` and `@container`. */
function rulesOf(css: string, inMotionQuery = false): Rule[] {
  const rules: Rule[] = [];
  let i = 0;
  while (i < css.length) {
    const open = css.indexOf("{", i);
    if (open < 0) break;
    let depth = 0;
    let close = open;
    for (; close < css.length; close++) {
      if (css[close] === "{") depth++;
      else if (css[close] === "}" && --depth === 0) break;
    }
    const prelude = squash(css.slice(i, open).split(";").pop() ?? "");
    const body = css.slice(open + 1, close);
    i = close + 1;
    if (!prelude.startsWith("@")) {
      // Nested rules aren't parsed, so a motion query inside one fails the test instead of
      // passing unchecked.
      if (body.includes("{") && MOTION.test(body)) {
        throw new Error(
          `CSS nesting with a motion query is not supported by this ratchet: ${prelude}`,
        );
      }
      rules.push({ selectors: splitList(prelude), inMotionQuery });
    } else if (/^@(media|supports|layer|container)\b/i.test(prelude)) {
      const motion = inMotionQuery || (/^@media\b/i.test(prelude) && MOTION.test(prelude));
      rules.push(...rulesOf(body, motion));
    }
  }
  return rules;
}

/** Selectors inside a reduced-motion query that no sheet in `sources` twins. */
function untwinned(sources: string[]): string[] {
  const rules = sources.flatMap((source) =>
    rulesOf(
      source
        .replace(/\/\*[\s\S]*?\*\//g, "")
        // A brace or semicolon in a string must not end a block or a statement. The strings
        // stay, because the twin prefix holds `"reduce"`.
        .replace(/"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'/g, (s) => s.replace(/[{};]/g, " ")),
    ),
  );
  const twins = new Set(
    rules
      .filter((rule) => !rule.inMotionQuery)
      .flatMap((rule) => rule.selectors)
      .flatMap((selector) => {
        if (selector.startsWith(PREFIX)) return [selector.slice(PREFIX.length)];
        // A compound twin `html[data-motion="reduce"]...` is the root element itself.
        if (!selector.startsWith(ROOT_TWIN) || selector[ROOT_TWIN.length] === " ") return [];
        const rest = selector.slice(ROOT_TWIN.length);
        return [`html${rest}`, `:root${rest}`];
      }),
  );
  return rules
    .filter((rule) => rule.inMotionQuery)
    .flatMap((rule) => rule.selectors)
    .filter((selector) => !twins.has(selector));
}

test("every reduced-motion selector has an html[data-motion=reduce] twin", () => {
  // The twin may sit in another sheet, so the whole app is checked at once.
  expect(untwinned(Object.values(SHEETS))).toEqual([]);
});

test("the ratchet flags an untwinned selector and passes a twinned one", () => {
  const query = "@media (prefers-reduced-motion: reduce) { .a { animation: none; } }";
  const twin = 'html[data-motion="reduce"]  .a { animation: none; }';
  expect(untwinned([query])).toEqual([".a"]);
  expect(untwinned([query, twin])).toEqual([]);
  const list = "@media (min-width: 1px), (prefers-reduced-motion: no-preference) { .a, .b {} }";
  expect(untwinned([list, twin])).toEqual([".b"]);
});

test("the ratchet parses the forms a later sheet may use", () => {
  const reduce = "@media (prefers-reduced-motion: reduce)";
  expect(untwinned([`@import "x.css"; ${reduce} { .a {} }`])).toEqual([".a"]);
  expect(untwinned([`@layer base; ${reduce} { .a {} }`])).toEqual([".a"]);
  expect(untwinned([`${reduce} { @container (min-width: 1px) { .c {} } }`])).toEqual([".c"]);
  expect(untwinned([`.s::before { content: "}"; } ${reduce} { .a {} }`])).toEqual([".a"]);
  expect(untwinned(["@media (prefers-reduced-motion) { .a {} }"])).toEqual([".a"]);
  expect(untwinned(["@MEDIA (PREFERS-REDUCED-MOTION: REDUCE) { .a {} }"])).toEqual([".a"]);
  expect(() =>
    untwinned([`.n { transition: opacity 1s; ${reduce} { transition: none; } }`]),
  ).toThrow("CSS nesting with a motion query is not supported by this ratchet");
  expect(untwinned([".n { &:hover { opacity: 1; } }"])).toEqual([]);
});

test("a root twin html[data-motion=reduce] covers html and :root", () => {
  const smooth =
    "@media (prefers-reduced-motion: no-preference) { html { scroll-behavior: smooth } }";
  const rootSmooth = smooth.replace("html {", ":root {");
  const twin = 'html[data-motion="reduce"] { scroll-behavior: auto }';
  expect(untwinned([smooth])).toEqual(["html"]);
  expect(untwinned([smooth, twin])).toEqual([]);
  expect(untwinned([rootSmooth, twin])).toEqual([]);
});
