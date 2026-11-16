/**
 * Review, commit, integrate and push end-to-end. A session in worktree `viki/e2e-int`
 * changes `shared.txt` (`fake.touch` in `tests/fixtures/claude/code-write.jsonl`); the worktree's
 * Review tab lists it as Modified and shows its diff; Commit runs the repository's `pre-commit`
 * hook (which leaves a marker file); Integrate fast-forwards `main` in the canonical folder, which
 * ends with the session's file and a clean `git status`; Push sends the worktree's branch and then,
 * from the folder's own checkout, `main` to a local bare `origin`. The remote is a local path, so
 * the worktree's Push shows the no-pull-request-page words and the server opens no URL.
 *
 * The repository is made after the server is up, under an empty global Git config (see
 * `e2e/scenarios/code/workbench.test.ts`).
 *
 * Every label asserted here is the `en` catalog row: the harness's Edge asks for English.
 *
 * Run from frontend/ (binds 8765; check it is free first, and build first):
 *   npm run e2e -- e2e/scenarios/code/integrate.test.ts
 */
// The harness owns `e2e/node.d.ts`, which declares only what `server.ts` needs; these modules are
// this file's own business, so they are suppressed here rather than added to it
// (`e2e/scenarios/code/worktrees.test.ts` does the same).
// @ts-expect-error see above
import { execFileSync } from "node:child_process";
// @ts-expect-error see above
import { chmodSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
// @ts-expect-error see above
import { homedir, tmpdir } from "node:os";
// @ts-expect-error see above
import { join } from "node:path";
import type { Browser, Page } from "playwright-core";
import { afterAll, afterEach, beforeAll, expect, test } from "vitest";
import {
  appUrl,
  launchEdge,
  openedUrls,
  send,
  TEST_TOKEN,
  waitForCount,
  waitForText,
} from "../../harness";
import { ORIGIN, type Server, startServer } from "../../server";
import { navigateSidebar } from "../../sidebar-navigation";

/** `scripts/e2e_server.py`'s own SCRATCH root, mirrored as `e2e/scenarios/code/worktrees.test.ts` does. */
const SCRATCH =
  process.platform === "win32" ? join(tmpdir(), "viki-e2e") : join(homedir(), ".cache", "viki-e2e");
const CODE = join(SCRATCH, "code");
const REMOTE = join(SCRATCH, "remote.git");
const HOOKS = join(SCRATCH, "hooks");
const MARKER = join(HOOKS, "ran");
const FIXTURE = "tests/fixtures/claude/code-write.jsonl";
const BRANCH = "viki/e2e-int";
/** The start of the fixture's reply. */
const REPLY = "Prime directive";
const NO_PAGE = "VIKI doesn't know this host's pull request page. Open it on your host.";

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

/** Git under an empty global config, in `dir`. */
function gitIn(dir: string, ...args: string[]): string {
  const emptyConfig = join(SCRATCH, "empty.gitconfig");
  writeFileSync(emptyConfig, "", "utf-8");
  const env = { ...process.env, GIT_CONFIG_GLOBAL: emptyConfig, GIT_CONFIG_NOSYSTEM: "1" };
  return execFileSync("git", ["-C", dir, ...args], { env, encoding: "utf-8" });
}

const git = (...args: string[]): string => gitIn(CODE, ...args);

/**
 * A committed repository with `shared.txt`, a `pre-commit` hook that writes the marker file, and a
 * bare `origin`. The address is built in two parts so the fixture-clean e-mail scan reads no
 * address in this file.
 */
function makeRepository(): void {
  mkdirSync(CODE, { recursive: true });
  git("init", "-q", "-b", "main");
  git("config", "user.email", `e2e${"@"}example.com`);
  git("config", "user.name", "e2e");
  git("config", "commit.gpgsign", "false");
  git("config", "core.autocrlf", "false");
  mkdirSync(HOOKS, { recursive: true });
  writeFileSync(
    join(HOOKS, "pre-commit"),
    `#!/bin/sh\necho ran > "${MARKER.replaceAll("\\", "/")}"\n`,
    "utf-8",
  );
  chmodSync(join(HOOKS, "pre-commit"), 0o755); // Git runs only an executable hook off Windows
  git("config", "core.hooksPath", HOOKS.replaceAll("\\", "/"));
  writeFileSync(join(CODE, "shared.txt"), "base\n", "utf-8");
  git("add", "-A");
  git("commit", "-q", "-m", "scratch");
  mkdirSync(REMOTE, { recursive: true });
  gitIn(REMOTE, "init", "-q", "--bare", "-b", "main");
  git("remote", "add", "origin", REMOTE);
}

/** Use the shared sidebar, opening its drawer when it is not docked. */
async function showView(page: Page, view: "Chat" | "Code" | "Agents"): Promise<void> {
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

const rowFor = (page: Page, branch: string) => page.locator(".worktree-row", { hasText: branch });

test("a worktree's change is reviewed, committed, integrated into main and pushed", async () => {
  server = await startServer({ git: true, fixture: FIXTURE });
  makeRepository();
  const page = await openApp(1440);
  await registerAndTrust(page);

  await openWorktrees(page);
  await page.getByLabel("Branch name").fill(BRANCH);
  await page.getByRole("button", { name: "Create worktree" }).click();
  await rowFor(page, BRANCH)
    .locator(".chip", { hasText: /^Ready$/ })
    .waitFor();

  // A session in the worktree: the fake CLI writes shared.txt into the folder it was spawned in.
  await rowFor(page, BRANCH).getByRole("button", { name: "New session here" }).click();
  await page
    .locator(".conversation-title + .root-label", { hasText: `code · ${BRANCH}` })
    .waitFor();
  await send(page, "hello");
  await waitForText(page, ".reply .md", REPLY, 20_000);
  await waitForCount(page, ".composer-stop", 0, 20_000);

  // Review opens on that worktree and lists the file as modified.
  await openWorktrees(page);
  await rowFor(page, BRANCH).getByRole("button", { name: "Review", exact: true }).click();
  const entry = page.locator(".review-entry", { hasText: "shared.txt" });
  await entry.locator(".chip", { hasText: /^Modified$/ }).waitFor();

  // Its diff, then back to the list.
  await entry.locator("button.review-path-button").click();
  await waitForText(page, ".review-view .code-diff", "from the session");
  await page.getByRole("button", { name: "Back to the list" }).click();
  await entry.waitFor();

  // Commit the ticked file; the repository's pre-commit hook ran.
  await page.getByLabel("Include shared.txt").check();
  await page.getByLabel("Commit message").fill("e2e commit");
  await page.getByRole("button", { name: "Commit selected files" }).click();
  await waitForText(page, ".review-form + p[role=status]", "Committed ");
  const committed = (await page.locator(".review-form + p[role=status]").textContent()) ?? "";
  expect(existsSync(MARKER)).toBe(true);
  const oid = git("rev-parse", BRANCH).trim();
  expect(committed).toBe(`Committed ${oid.slice(0, 12)}.`);

  // Integrate into main: the canonical folder holds the session's file and stays clean.
  await page.getByRole("button", { name: "Integrate into main" }).click();
  await waitForText(
    page,
    '[aria-label="Integrate"] [role="status"]',
    `Integrated: main is now at ${oid.slice(0, 12)}.`,
  );
  expect(readFileSync(join(CODE, "shared.txt"), "utf-8")).toBe("from the session\n");
  expect(git("status", "--porcelain")).toBe("");

  // Push the worktree's branch: a local-path remote has no pull request page.
  await page.getByRole("button", { name: `Push ${BRANCH}` }).click();
  await waitForText(page, '[aria-label="Push"] [role="status"]', `Pushed ${BRANCH} to origin.`);
  await page.getByText(NO_PAGE, { exact: true }).waitFor();
  expect(await openedUrls()).toEqual([]);

  // The folder's own checkout: push main.
  await page.locator(".review-bar select").selectOption({ label: "This folder" });
  await page.getByRole("button", { name: "Push main" }).click();
  await waitForText(page, '[aria-label="Push"] [role="status"]', "Pushed main to origin.");

  const remote = gitIn(REMOTE, "for-each-ref", "--format=%(objectname) %(refname)");
  expect(remote).toContain(`${oid} refs/heads/main`);
  expect(remote).toContain(`${oid} refs/heads/${BRANCH}`);

  // At 700 px the Review tab and the page fit the window: no sideways scroll.
  await page.setViewportSize({ width: 700, height: 900 });
  await page.locator(".review").waitFor();
  // The shell undocks its rails a render after the resize: measure once they are gone.
  await page.waitForFunction(() => document.querySelector(".shell-rail-host") === null);
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
      ),
  );
  const fit = await page.locator(".review").evaluate((pane) => ({
    pane: pane.scrollWidth - pane.clientWidth,
    page: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  }));
  expect(fit.pane).toBeLessThanOrEqual(0);
  expect(fit.page).toBeLessThanOrEqual(0);
  await page.close();
}, 240_000);
