// @vitest-environment node
/**
 * Ratchet: a `btn` class always names its variant (`go`, `alt` or `txt`). Every string literal
 * and every template quasi in the app's sources is checked on its own, so `className="btn sm"`
 * and `` `btn ${x}` `` both fail while the selector text `".btn"` does not.
 */
import { parseAst } from "vite";
import { expect, test } from "vitest";

const SOURCES = import.meta.glob<string>(
  [
    "../**/*.{ts,tsx}",
    "!../**/*.test.{ts,tsx}",
    "!../**/*.d.ts",
    "!../accept/**",
    "!../testing/**",
    "!../test*.ts",
  ],
  { query: "?raw", import: "default", eager: true },
);

/** The strings in `code` whose classes include `btn` but no variant. */
function variantless(code: string, lang: "ts" | "tsx"): string[] {
  const found: string[] = [];
  const check = (text: unknown) => {
    if (typeof text !== "string") return;
    const tokens = text.split(/\s+/);
    if (tokens.includes("btn") && !["go", "alt", "txt"].some((v) => tokens.includes(v))) {
      found.push(text);
    }
  };
  const visit = (node: unknown) => {
    if (!node || typeof node !== "object") return;
    if (Array.isArray(node)) return node.forEach(visit);
    const record = node as Record<string, unknown>;
    if (record.type === "Literal") check(record.value);
    if (record.type === "TemplateElement") {
      check((record.value as { cooked?: string } | undefined)?.cooked);
    }
    for (const child of Object.values(record)) visit(child);
  };
  visit(parseAst(code, { lang }));
  return found;
}

test("every btn names go, alt or txt", () => {
  const offenders: string[] = [];
  for (const [file, code] of Object.entries(SOURCES)) {
    const lang = file.endsWith(".tsx") ? "tsx" : "ts";
    for (const text of variantless(code, lang)) offenders.push(`${file}: ${JSON.stringify(text)}`);
  }
  expect(offenders).toEqual([]);
});

test("the ratchet flags variant-less btn and passes the rest", () => {
  const flags = (source: string) => variantless(source, "tsx").length;
  expect(flags('const a = <b className="btn sm" />;')).toBe(1);
  expect(flags('const a = "btn";')).toBe(1);
  // biome-ignore lint/suspicious/noTemplateCurlyInString: planted source text
  expect(flags("const a = `btn ${x}`;")).toBe(1);
  expect(flags('const a = "btn go";')).toBe(0);
  expect(flags('const a = "btn alt sm";')).toBe(0);
  // biome-ignore lint/suspicious/noTemplateCurlyInString: planted source text
  expect(flags("const a = `btn txt ${x}`;")).toBe(0);
  expect(flags('const a = ".btn";')).toBe(0);
});
