// @vitest-environment node
/**
 * Ratchet: a width `@media` query is on the 800px / 1280px ladder or is a named exception. The
 * allowlist only shrinks: an entry that no longer occurs fails.
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

const LADDER = [
  "not all and (min-width: 800px)",
  "(min-width: 800px)",
  "not all and (min-width: 1280px)",
  "(min-width: 1280px)",
];

type Allowed = { file: string; prelude: string; reason: string };

const ALLOWED: Allowed[] = [
  {
    file: "Bots/BotThreadWorkspace.css",
    prelude: "(max-width: 900px)",
    reason: "thread layout stacks",
  },
  {
    file: "Routines/RoutinesView.css",
    prelude: "(max-width: 900px)",
    reason: "list and detail stack",
  },
  {
    file: "Routines/RoutinePreview.css",
    prelude: "(max-width: 900px)",
    reason: "preview facts stack",
  },
  {
    file: "Capabilities/InventoryCapabilities.css",
    prelude: "(width < 1000px)",
    reason: "grid and detail stack",
  },
  { file: "Decisions/DecisionSection.css", prelude: "(max-width: 420px)", reason: "filters stack" },
  {
    file: "Workbench/CodeEditor.css",
    prelude: "(min-width: 1100px)",
    reason: "editor and disk copy sit side by side",
  },
];

/** The width `@media` preludes of a stylesheet, without the `@media` keyword. */
function widthPreludes(css: string): string[] {
  const text = css.replace(/\/\*[\s\S]*?\*\//g, "");
  return [...text.matchAll(/@media([^{]*)\{/gi)]
    .map((m) => (m[1] ?? "").replace(/\s+/g, " ").trim())
    .filter((prelude) => /\b(min-|max-)?width\b/i.test(prelude));
}

const isEntry = (a: Allowed, file: string, prelude: string) =>
  file.endsWith(`/${a.file}`) && a.prelude === prelude;

/** "file: prelude" for each width query off the ladder and not allowed. */
function offLadder(sheets: Record<string, string>, allowed: Allowed[] = ALLOWED): string[] {
  return Object.entries(sheets).flatMap(([file, css]) =>
    widthPreludes(css)
      .filter((prelude) => !LADDER.includes(prelude))
      .filter((prelude) => !allowed.some((a) => isEntry(a, file, prelude)))
      .map((prelude) => `${file}: ${prelude}`),
  );
}

test("every width query is on the ladder or allowed", () => {
  expect(offLadder(SHEETS)).toEqual([]);
});

test("every allowlist entry still occurs", () => {
  const stale = ALLOWED.filter(
    (a) =>
      !Object.entries(SHEETS).some(([file, css]) =>
        widthPreludes(css).some((prelude) => isEntry(a, file, prelude)),
      ),
  );
  expect(stale).toEqual([]);
});

test("the ratchet flags an off-ladder query and passes a ladder one", () => {
  const sheet = (css: string) => ({ "../a.css": css });
  expect(offLadder(sheet("@media (max-width: 900px) { .a {} }"), [])).toEqual([
    "../a.css: (max-width: 900px)",
  ]);
  expect(offLadder(sheet("@media (width < 1000px) { .a {} }"), [])).toHaveLength(1);
  expect(offLadder(sheet("@MEDIA (max-width: 900px) { .a {} }"), [])).toHaveLength(1);
  expect(offLadder(sheet("@media not all and (min-width: 800px) { .a {} }"), [])).toEqual([]);
  expect(offLadder(sheet("@media (prefers-reduced-motion: reduce) { .a {} }"), [])).toEqual([]);
});
