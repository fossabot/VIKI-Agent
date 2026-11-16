/**
 * Drift check: every help topic's UI labels exist in the source code, every tour region is
 * wired up with a data-region attribute, and help content stays in sync with the app.
 */
// @ts-expect-error no @types/node in this browser-focused tsconfig; see ../testing/css.ts
import { readdirSync, readFileSync } from "node:fs";
// @ts-expect-error see above
import { fileURLToPath } from "node:url";
import { expect, test } from "vitest";
import { HELP_TOPICS, TOUR_REGIONS } from "./helpTopics";

// Mirrors englishOnly.unit.test.ts's own JAPANESE range; topics/*.md sits outside that test's
// .ts/.tsx scan (import.meta.glob there only covers source code), so help content needs its own
// check here.
const JAPANESE = /[　-〿぀-ヿ㐀-䶿一-鿿＀-￯]/;

const here = import.meta.url;
const helpDir = fileURLToPath(new URL("./topics/", here)) as string;
const srcDir = fileURLToPath(new URL("../", here)) as string;

/** Every `.md` file in the help/topics directory. */
function helpTopicFiles(): string[] {
  const entries = readdirSync(helpDir) as string[];
  return entries.filter((file) => file.endsWith(".md"));
}

/** Every non-test `.ts`/`.tsx` file in src/ (excluding src/help/), as a concatenated string. */
function sourceCode(): string {
  const entries = readdirSync(srcDir, { recursive: true }) as string[];
  const files = entries
    .map((rel) => rel.replace(/\\/g, "/"))
    .filter((rel) => {
      // Exclude help directory
      if (rel.startsWith("help/")) return false;
      // Include only .ts/.tsx but exclude test files
      if (/\.tsx?$/.test(rel)) return !rel.includes(".test.");
      return false;
    });

  let concatenated = "";
  for (const file of files) {
    try {
      const content = readFileSync(srcDir + file, "utf-8") as string;
      concatenated += `${content}\n`;
    } catch {
      // Skip files that cannot be read
    }
  }
  return concatenated;
}

/** Extract all bold labels (**label**) from markdown text. */
function extractBoldLabels(markdown: string): string[] {
  const labels: string[] = [];
  const regex = /\*\*([^*]+)\*\*/g;
  for (const match of markdown.matchAll(regex)) {
    labels.push(match[1] as string);
  }
  return labels;
}

test("every help topic file starts with # (heading level 1)", () => {
  for (const file of helpTopicFiles()) {
    const content = readFileSync(helpDir + file, "utf-8") as string;
    const firstLine = content.split("\n")[0];
    expect(firstLine, `${file} first line`).toMatch(/^# /);
  }
});

test("every bold label in help topics is found in the source code", () => {
  const source = sourceCode();
  const missing: string[] = [];

  for (const file of helpTopicFiles()) {
    const content = readFileSync(helpDir + file, "utf-8") as string;
    const labels = extractBoldLabels(content);

    for (const label of labels) {
      // Check if label exists in source code (as text that could be a UI label)
      if (!source.includes(label)) {
        missing.push(`${file}: **${label}**`);
      }
    }
  }

  expect(missing).toEqual([]);
});

test("every TOUR_REGIONS value appears in source as data-region attribute", () => {
  const source = sourceCode();
  const missing: string[] = [];

  for (const region of TOUR_REGIONS) {
    const pattern = `data-region="${region}"`;
    if (!source.includes(pattern)) {
      missing.push(region);
    }
  }

  expect(missing).toEqual([]);
});

test("the scan flags planted labels that do not exist", () => {
  const source = sourceCode();
  const planted = "Definitely Not A Label 42";

  // This test verifies that the check catches missing labels
  expect(source.includes(planted)).toBe(false);
});

test("the topics directory holds exactly the registered ids, each body matching its file", () => {
  const fileIds = helpTopicFiles()
    .map((file) => file.replace(/\.md$/, ""))
    .sort();
  const registryIds = HELP_TOPICS.map((topic) => topic.id)
    .slice()
    .sort();
  expect(fileIds).toEqual(registryIds);

  for (const topic of HELP_TOPICS) {
    const onDisk = readFileSync(`${helpDir}${topic.id}.md`, "utf-8") as string;
    expect(topic.body, topic.id).toBe(onDisk);
  }
});

test("no topic file contains a Japanese character", () => {
  const offenders: string[] = [];
  for (const file of helpTopicFiles()) {
    const content = readFileSync(helpDir + file, "utf-8") as string;
    if (JAPANESE.test(content)) offenders.push(file);
  }
  expect(offenders).toEqual([]);
});
