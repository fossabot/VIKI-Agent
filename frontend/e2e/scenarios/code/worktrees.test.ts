/**
 * Worktrees end-to-end: two sessions in two worktrees of one trusted code folder
 * change the same file name, and the canonical checkout stays as it was. The Worktrees tab creates
 * `viki/e2e-a` and `viki/e2e-b`; "New session here" opens a session in each, whose header and tab
 * carry `code · <branch>`. The fake CLI writes `shared.txt` into the folder it was spawned in
 * (`fake.touch` in `tests/fixtures/claude/code-write.jsonl`), so each worktree folder holds the
 * session's file while `code/shared.txt` is untouched and `git status` there is clean.
 *
 * Remove is refused while a session runs in the worktree, so a's session is closed from Running
 * work in Capabilities first (a tab's close button only hides the tab); the worktree then stays, because the
 * session's file is an untracked change, and the dirty words show with 1. A third worktree with
 * no change is removed: its folder goes and its branch stays.
 *
 * The repository is made after the server is up, under an empty global Git config (see
 * `e2e/scenarios/code/workbench.test.ts`).
 *
 * Every label asserted here is the `en` catalog row: the harness's Edge asks for English.
 *
 * Run from frontend/ (binds 8765; check it is free first, and build first):
 *   npm run e2e -- e2e/scenarios/code/worktrees.test.ts
 */
// The harness owns `e2e/node.d.ts`, which declares only what `server.ts` needs; these modules are
// this file's own business, so they are suppressed here rather than added to it
// (`e2e/scenarios/code/workbench.test.ts` does the same).
// @ts-expect-error see above
import { execFileSync } from "node:child_process";
// @ts-expect-error see above
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
// @ts-expect-error see above
import { homedir, tmpdir } from "node:os";
// @ts-expect-error see above
import { join } from "node:path";
import type { Browser, Locator, Page } from "playwright-core";
import { afterAll, afterEach, beforeAll, expect, test } from "vitest";
import { appUrl, launchEdge, send, TEST_TOKEN, waitForCount, waitForText } from "../../harness";
import { ORIGIN, type Server, startServer } from "../../server";
import { navigateSidebar } from "../../sidebar-navigation";

/** `scripts/e2e_server.py`'s own SCRATCH root, mirrored as `e2e/scenarios/vaults/browser.test.ts` does. */
const SCRATCH =
  process.platform === "win32" ? join(tmpdir(), "viki-e2e") : join(homedir(), ".cache", "viki-e2e");
const CODE = join(SCRATCH, "code");
/** `worktree_area(data_dir)`: the folder beside the server's data folder, `<data dir>-worktrees`. */
const AREA = join(SCRATCH, "data-worktrees");
const FIXTURE = "tests/fixtures/claude/code-write.jsonl";
/** The start of the fixture's reply. */
const REPLY = "Prime directive";

let browser: Browser | undefined;
let server: Server | undefined;

beforeAll(async () => {
  browser = await launchEdge();
});

afterAll(async () => {
  await browser?.close();
});

afterEach(async () => {
  await server?.stop();
  server = undefined;
});

/** Git in the scratch repository, under an empty global config and the repository's own identity. */
function git(...args: string[]): string {
  const emptyConfig = join(SCRATCH, "empty.gitconfig");
  writeFileSync(emptyConfig, "", "utf-8");
  const env = { ...process.env, GIT_CONFIG_GLOBAL: emptyConfig, GIT_CONFIG_NOSYSTEM: "1" };
  return execFileSync("git", ["-C", CODE, ...args], { env, encoding: "utf-8" });
}

/** A committed repository with one file, `shared.txt`. */
function makeRepository(): void {
  mkdirSync(CODE, { recursive: true });
  git("init", "-q", "-b", "main");
  git("config", "user.email", "e2e@example.com");
  git("config", "user.name", "e2e");
  git("config", "core.autocrlf", "false");
  writeFileSync(join(CODE, "shared.txt"), "base\n", "utf-8");
  git("add", "-A");
  git("commit", "-q", "-m", "scratch");
}

/** Use the shared sidebar, opening its drawer when it is not docked. */
async function showView(page: Page, view: "Chat" | "Code" | "Capabilities"): Promise<void> {
  await navigateSidebar(page, view);
}

/** Settings → Workspaces and Vault → Add folders (typed path) → Trust…, acknowledging whatever
 * the panel asks. The typed field shows at once only where the OS folder picker is unavailable
 * (a headless runner); elsewhere it sits behind "Type a path instead". The server answers the
 * status the page read at load, so this branch never races that read. */
async function registerAndTrust(page: Page): Promise<void> {
  const picker = await page.request.get(`${ORIGIN}/api/v2/folders/pick`, {
    headers: { authorization: `Bearer ${TEST_TOKEN}` },
  });
  const { available } = (await picker.json()) as { available: boolean };
  await page.keyboard.press("Control+Comma");
  await page.waitForSelector('[data-region="settings"]');
  await page.getByRole("tab", { name: "Workspaces and Vault" }).click();
  const addFolders = page.locator(".ws-add");
  if (available) await addFolders.getByRole("button", { name: "Type a path instead" }).click();
  await addFolders.getByLabel("Code folder path").fill(CODE);
  await addFolders.getByRole("button", { name: "Add", exact: true }).click();
  await page.locator(".workspace-row .chip", { hasText: /^Not trusted$/ }).waitFor();
  await page.getByRole("button", { name: "Trust…" }).first().click();
  await page.getByRole("button", { name: "Trust this folder" }).click();
  const acknowledgements = page.locator(".policy-acknowledgements input[type=checkbox]");
  for (const box of await acknowledgements.all()) await box.check();
  await page.locator(".policy-acknowledgements").getByRole("button", { name: "Apply" }).click();
  await page.locator(".workspace-row .chip", { hasText: /^Trusted$/ }).waitFor();
}

/** A page whose settings read has landed, which is what the app's own DOM writes wait for. */
async function openApp(width: number): Promise<Page> {
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({ viewport: { width, height: 900 } });
  const settings = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v2/settings" &&
      response.request().method() === "GET",
  );
  await page.goto(appUrl());
  await page.waitForSelector('[data-region="shell"]');
  await settings;
  return page;
}

/** The Code view with its Worktrees tab open. */
async function openWorktrees(page: Page): Promise<void> {
  await showView(page, "Code");
  await page.getByRole("tab", { name: "Worktrees", exact: true }).click();
}

const rowFor = (page: Page, branch: string): Locator =>
  page.locator(".worktree-row", { hasText: branch });

async function createWorktree(page: Page, branch: string): Promise<void> {
  await page.getByLabel("Branch name").fill(branch);
  await page.getByRole("button", { name: "Create worktree" }).click();
  await rowFor(page, branch)
    .locator(".chip", { hasText: /^Ready$/ })
    .waitFor();
}

/** Remove, confirming in the app's dialog. */
async function removeWorktree(page: Page, branch: string): Promise<void> {
  await rowFor(page, branch).getByRole("button", { name: "Remove", exact: true }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "Remove", exact: true }).click();
}

/** "New session here" on a ready row, then one turn: the header shows the folder and the reply. */
async function sessionHere(page: Page, branch: string): Promise<void> {
  await openWorktrees(page);
  await rowFor(page, branch).getByRole("button", { name: "New session here" }).click();
  await page
    .locator(".conversation-title + .root-label", { hasText: `code · ${branch}` })
    .waitFor();
  await send(page, "hello");
  await waitForText(page, ".reply .md", REPLY, 20_000);
  await waitForCount(page, ".composer-stop", 0, 20_000);
}

/** The worktree folders under the area, by name. */
const folders = (): string[] => (existsSync(AREA) ? readdirSync(AREA) : []);

test("two sessions in two worktrees change one file name and the checkout stays clean", async () => {
  server = await startServer({ git: true, fixture: FIXTURE });
  makeRepository();
  const page = await openApp(1440);
  await registerAndTrust(page);

  await openWorktrees(page);
  await createWorktree(page, "viki/e2e-a");
  await createWorktree(page, "viki/e2e-b");

  await sessionHere(page, "viki/e2e-a");
  await sessionHere(page, "viki/e2e-b");

  // Each session wrote the same name in its own folder; the canonical checkout is unchanged.
  const made = folders();
  expect(made).toHaveLength(2);
  for (const folder of made) {
    expect(readFileSync(join(AREA, folder, "shared.txt"), "utf-8")).toBe("from the session\n");
  }
  expect(readFileSync(join(CODE, "shared.txt"), "utf-8")).toBe("base\n");
  expect(git("status", "--porcelain")).toBe("");

  // Both sessions show on the tab strip with their folders, a first.
  await waitForCount(page, ".session-tabs [role=tab]", 2, 15_000);
  expect(await page.locator(".session-tabs .root-label").allTextContents()).toEqual([
    "code · viki/e2e-a",
    "code · viki/e2e-b",
  ]);

  // A session still runs in a: Remove is refused until Running work closes it.
  await openWorktrees(page);
  await removeWorktree(page, "viki/e2e-a");
  await waitForText(
    page,
    '.worktrees [role="status"]',
    "A session is running in this worktree. Close it first.",
  );
  await showView(page, "Capabilities");
  await page.getByRole("tab", { name: "Subagents", exact: true }).click();
  const running = page.getByRole("region", { name: "Running work", exact: true });
  await waitForCount(page, '[aria-label="Running work"] .session-organizer', 2, 15_000);
  await running
    .locator(".session-organizer")
    .first()
    .getByRole("button", { name: "Close", exact: true })
    .click();
  await waitForCount(page, '[aria-label="Running work"] .session-organizer', 1, 15_000);
  await showView(page, "Chat");
  await waitForCount(page, ".session-tabs [role=tab]", 1, 15_000);
  expect(await page.locator(".session-tabs .root-label").allTextContents()).toEqual([
    "code · viki/e2e-b",
  ]);

  // Closed, a still holds the session's file as an untracked change, so Remove keeps it.
  await openWorktrees(page);
  await removeWorktree(page, "viki/e2e-a");
  await waitForText(
    page,
    '.worktrees [role="status"]',
    "Not removed: 1 changed or new files. Commit or discard them first.",
  );
  await rowFor(page, "viki/e2e-a")
    .locator(".chip", { hasText: /^Ready$/ })
    .waitFor();
  expect(folders()).toHaveLength(2);

  // A worktree with no change is removed: its folder goes, its branch stays.
  await createWorktree(page, "viki/e2e-c");
  const [removed] = folders().filter((folder) => !made.includes(folder));
  if (removed === undefined) throw new Error("the third worktree's folder was expected");
  expect(existsSync(join(AREA, removed))).toBe(true);
  await removeWorktree(page, "viki/e2e-c");
  await waitForText(
    page,
    '.worktrees [role="status"]',
    "Removed the worktree. The branch viki/e2e-c stays.",
  );
  await rowFor(page, "viki/e2e-c")
    .locator(".chip", { hasText: /^Removed$/ })
    .waitFor();
  expect(existsSync(join(AREA, removed))).toBe(false);
  expect(git("branch", "--list", "viki/e2e-c")).toContain("viki/e2e-c");

  // At 700 px the panel and the page fit the window: no sideways scroll.
  await page.setViewportSize({ width: 700, height: 900 });
  await openWorktrees(page);
  await rowFor(page, "viki/e2e-b").waitFor();
  const fit = await page.locator(".worktrees").evaluate((panel) => ({
    panel: panel.scrollWidth - panel.clientWidth,
    page: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  }));
  expect(fit.panel).toBeLessThanOrEqual(0);
  expect(fit.page).toBeLessThanOrEqual(0);
  await page.close();
}, 240_000);
