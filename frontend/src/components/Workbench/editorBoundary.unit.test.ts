/**
 * The editor must stay out of the entry chunk: only `codemirror.ts` names a CodeMirror or Lezer
 * package, and every other app file reaches `codemirror.ts` through `import(...)`.
 * The walk is the one `styles/tokens.unit.test.ts` uses.
 */
// @ts-expect-error no @types/node in this browser-focused tsconfig; see ../../testing/css.ts
import { readdirSync, readFileSync } from "node:fs";
// @ts-expect-error see above
import { fileURLToPath } from "node:url";
import { expect, test } from "vitest";

/** An import, `require` or mock naming a package: the package name follows a quote. A comment
 * that talks about CodeMirror holds no quote before the name, so it is not a hit. */
const PACKAGE_NAME = /["'`]@(?:codemirror|lezer)\//;

/** `import ... from "x/codemirror"`, `export ... from "x/codemirror"` and `import "x/codemirror"`.
 * `import(...)` has no `from` and no quote after `import`, so it is not a hit. `import type` and
 * `export type` are erased at build time and are the way a caller gets `EditorHandle`. */
const STATIC_IMPORT =
  /\b(?:import|export)(?!\s+type\b)(?:[^;'"`]*?\bfrom\s*|\s*)["'`][^"'`]*\/codemirror["'`]/;

function violations(rel: string, text: string): string[] {
  const found: string[] = [];
  const name = rel.split("/").pop() ?? rel;
  const isTest = name.includes(".test.");
  if (name !== "codemirror.ts" && PACKAGE_NAME.test(text)) {
    found.push(`${rel} names an @codemirror/ or @lezer/ package`);
  }
  if (!isTest && STATIC_IMPORT.test(text)) found.push(`${rel} imports ./codemirror statically`);
  return found;
}

const here = import.meta.url;
const srcDir = fileURLToPath(new URL("../../", here)) as string;

function sourceFiles(): string[] {
  const entries = readdirSync(srcDir, { recursive: true }) as string[];
  return entries.map((rel) => rel.replace(/\\/g, "/")).filter((rel) => /\.tsx?$/.test(rel));
}

test("only codemirror.ts names a CodeMirror or Lezer package, and nothing imports it statically", () => {
  const failures = sourceFiles().flatMap((rel) =>
    violations(rel, readFileSync(srcDir + rel, "utf-8") as string),
  );
  expect(failures).toEqual([]);
});

test("the walk reaches the editor module and the other source files", () => {
  const files = sourceFiles();
  expect(files).toContain("components/Workbench/codemirror.ts");
  expect(files).toContain("App.tsx");
});

test("the scan flags a package name outside codemirror.ts and a static import of it", () => {
  const view = ["@codemirror", "view"].join("/");
  const lezer = ["@lezer", "highlight"].join("/");
  expect(violations("a/B.tsx", `import { EditorView } from "${view}";`)).not.toEqual([]);
  expect(violations("a/B.tsx", `const m = await import('${lezer}');`)).not.toEqual([]);
  expect(violations("a/B.test.ts", `vi.mock("${view}");`)).not.toEqual([]);
  expect(violations("a/codemirror.ts", `import { EditorView } from "${view}";`)).toEqual([]);

  const staticForms = [
    'import { createEditor } from "./codemirror";',
    "import { type EditorHandle, createEditor } from '../Workbench/codemirror';",
    'import "./codemirror";',
    'export { createEditor } from "./codemirror";',
    'import * as cm from "./codemirror"',
    'import {\n  createEditor,\n} from "./codemirror";',
  ];
  for (const text of staticForms) {
    expect(violations("a/B.tsx", text), text).not.toEqual([]);
  }
});

test("the scan allows import(), type-only imports, comments and test files", () => {
  const allowed = [
    'const m = await import("./codemirror");',
    'const { createEditor } = await import("./codemirror");',
    'import type { EditorHandle } from "./codemirror";',
    'export type { EditorHandle } from "./codemirror";',
    'import { x } from "./codemirror.css";',
    "// loads @codemirror/view through ./codemirror when the editor opens",
  ];
  for (const text of allowed) {
    expect(violations("a/B.tsx", text), text).toEqual([]);
  }
  expect(violations("a/B.unit.test.tsx", 'import { x } from "./codemirror";')).toEqual([]);
});
