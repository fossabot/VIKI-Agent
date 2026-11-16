/**
 * The Kanban view, empty and with cards in every lane, rendered for a person to look at. Real
 * backend, real bundle, real Edge; never part of scripts/check.py. Synthetic data only.
 *
 *   npx vitest run --config vitest.shots.config.ts e2e/kanban.shots.ts
 *
 * Run from frontend/. It binds 8765 (check it is free first) and needs a fresh build.
 *
 * Two sizes (1440×1000 and 400×700) in light and dark, for the empty view and the lanes view:
 * PNGs land in `frontend/test-results/kanban/` as `kanban-<empty|lanes>-<1440|400>-<theme>.png`.
 */
// The harness owns `e2e/node.d.ts`, which declares only what `server.ts` needs; these modules are
// this file's own business (e2e/scenarios/vaults/browser.test.ts does the same).
// @ts-expect-error see above
import { mkdirSync, utimesSync, writeFileSync } from "node:fs";
// @ts-expect-error see above
import { homedir, tmpdir } from "node:os";
// @ts-expect-error see above
import { join } from "node:path";
import type { Browser, Page } from "playwright-core";
import { afterAll, beforeAll, test } from "vitest";
import { appUrl, launchEdge } from "./harness";
import { type Server, startServer } from "./server";
import { navigateSidebar } from "./sidebar-navigation";

const OUT = "test-results/kanban";

/** `scripts/e2e_server.py`'s own SCRATCH root, mirrored as `e2e/scenarios/vaults/browser.test.ts` does. */
const SCRATCH =
  process.platform === "win32" ? join(tmpdir(), "viki-e2e") : join(homedir(), ".cache", "viki-e2e");
const VAULT = join(SCRATCH, "workspaces", "vault");

interface Size {
  width: number;
  height: number;
}

const SIZES: readonly Size[] = [
  { width: 1440, height: 1000 },
  { width: 400, height: 700 },
];
const THEMES = ["light", "dark"] as const;
type Theme = (typeof THEMES)[number];

let browser: Browser | undefined;
let server: Server | undefined;

beforeAll(async () => {
  mkdirSync(OUT, { recursive: true });
  browser = await launchEdge();
  server = await startServer({
    bots: true,
    team: true,
    fixture: "tests/fixtures/claude/text-two-turns.jsonl",
  });
}, 120_000);

afterAll(async () => {
  await browser?.close();
  await server?.stop();
});

async function open(size: Size, theme: Theme): Promise<Page> {
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({
    viewport: { width: size.width, height: size.height },
    colorScheme: theme,
  });
  await page.goto(appUrl());
  await page.locator('[data-region="shell"]').waitFor({ timeout: 15_000 });
  await page.waitForFunction((t) => document.documentElement.dataset.theme === t, theme);
  await navigateSidebar(page, "Kanban");
  await page.getByRole("heading", { name: /^To do \(\d+\)$/ }).waitFor({ timeout: 15_000 });
  await page.evaluate(() => document.fonts.ready);
  return page;
}

/** One view at both sizes in both themes; `ready` waits for what the shot must show. */
async function capture(view: string, ready: (page: Page) => Promise<void>): Promise<void> {
  for (const size of SIZES) {
    for (const theme of THEMES) {
      const page = await open(size, theme);
      await ready(page);
      await page.screenshot({ path: `${OUT}/kanban-${view}-${size.width}-${theme}.png` });
      await page.close();
    }
  }
}

interface Card {
  name: string;
  status: string;
  owner: string;
  days: number;
}

const DAY_MS = 86_400_000;

const CARDS: readonly Card[] = [
  { name: "compare-backup-tools", status: "todo", owner: "research", days: 0 },
  { name: "outline-user-guide", status: "todo", owner: "writer", days: 4 },
  { name: "fix-export-script", status: "working", owner: "engineer", days: 2 },
  { name: "summarize-inbox", status: "working", owner: "ops", days: 0 },
  { name: "choose-hosting-plan", status: "blocked", owner: "chief", days: 1 },
  { name: "release-notes-draft", status: "review", owner: "writer and checker", days: 0 },
  { name: "archive-old-notes", status: "done", owner: "desk", days: 6 },
  { name: "someday-ideas", status: "later", owner: "sam", days: 3 },
];

/** The card's owner tag is one `owner/<seat>` entry per seat, on one line. */
function writeCard(card: Card): void {
  const seats = card.owner.split(" and ").map((seat) => `owner/${seat}`);
  const title = card.name.replaceAll("-", " ");
  const path = join(VAULT, "kanban", `${card.name}.md`);
  writeFileSync(
    path,
    `---\ntype: card\nstatus: ${card.status}\ntags: [${seats.join(", ")}]\n---\n\n# ${title}\n\nDone when: ${title} is finished.\n`,
    "utf-8",
  );
  const when = new Date(Date.now() - card.days * DAY_MS);
  utimesSync(path, when, when);
}

test("the empty Kanban", async () => {
  await capture("empty", async (page) => {
    await page.getByRole("button", { name: "Open Chief" }).waitFor({ timeout: 15_000 });
  });
}, 300_000);

test("the Kanban with cards in every lane", async () => {
  mkdirSync(join(VAULT, "kanban"), { recursive: true });
  for (const card of CARDS) writeCard(card);
  await capture("lanes", async (page) => {
    await page.getByText("1 ready for review, 1 blocked").waitFor({ timeout: 15_000 });
    await page.getByRole("heading", { name: /^Other \(\d+\)$/ }).waitFor({ timeout: 15_000 });
  });
}, 300_000);
