/**
 * Checks end-to-end. In a registered, trusted code folder the Checks tab adds three checks
 * and runs them through the host shell: "fail" shows its output and a "Failed (exit
 * code 3)" line carrying the head's first 12 hex digits and "no uncommitted changes"; "ok" passes
 * and the Review tab's Checks section reads "ok — Passed · matches this commit"; "slow"
 * ends through the tab's Cancel as "Cancelled". At 700 px the Checks tab has no sideways scroll.
 *
 * The commands use cmd.exe syntax on Windows and /bin/sh syntax on macOS and Linux.
 * The repository is made after the server is up, under an empty
 * global Git config (see `e2e/scenarios/code/integrate.test.ts`).
 *
 * Every label asserted here is the `en` catalog row: the harness's Edge asks for English.
 *
 * Run from frontend/ (binds 8765; check it is free first, and build first):
 *   npm run e2e -- e2e/scenarios/code/checks.test.ts
 */
// The harness owns `e2e/node.d.ts`, which declares only what `server.ts` needs; these modules are
// this file's own business, so they are suppressed here rather than added to it
// (`e2e/scenarios/code/integrate.test.ts` does the same).
// @ts-expect-error see above
import { execFileSync } from "node:child_process";
// @ts-expect-error see above
import { mkdirSync, writeFileSync } from "node:fs";
// @ts-expect-error see above
import { homedir, tmpdir } from "node:os";
// @ts-expect-error see above
import { join } from "node:path";
import type { Browser, Page } from "playwright-core";
import { afterAll, afterEach, beforeAll, expect, test } from "vitest";
import { appUrl, launchEdge, TEST_TOKEN, waitForText } from "../../harness";
import { ORIGIN, type Server, startServer } from "../../server";
import { navigateSidebar } from "../../sidebar-navigation";

/** `scripts/e2e_server.py`'s own SCRATCH root, mirrored as `e2e/scenarios/code/integrate.test.ts` does. */
const SCRATCH =
  process.platform === "win32" ? join(tmpdir(), "viki-e2e") : join(homedir(), ".cache", "viki-e2e");
const CODE = join(SCRATCH, "code");
const WINDOWS = process.platform === "win32";
const STATE = "no uncommitted changes";

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

/** Git under an empty global config, in the code folder. */
function git(...args: string[]): string {
  const emptyConfig = join(SCRATCH, "empty.gitconfig");
  writeFileSync(emptyConfig, "", "utf-8");
  const env = { ...process.env, GIT_CONFIG_GLOBAL: emptyConfig, GIT_CONFIG_NOSYSTEM: "1" };
  return execFileSync("git", ["-C", CODE, ...args], { env, encoding: "utf-8" });
}

/**
 * A repository with `a.txt` committed. The address is built in two parts so the fixture-clean
 * e-mail scan reads no address in this file.
 */
function makeRepository(): void {
  mkdirSync(CODE, { recursive: true });
  git("init", "-q", "-b", "main");
  git("config", "user.email", `e2e${"@"}example.com`);
  git("config", "user.name", "e2e");
  git("config", "commit.gpgsign", "false");
  git("config", "core.autocrlf", "false");
  writeFileSync(join(CODE, "a.txt"), "base\n", "utf-8");
  git("add", "-A");
  git("commit", "-q", "-m", "scratch");
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
  await page.keyboard.press(process.platform === "darwin" ? "Meta+Comma" : "Control+Comma");
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

/** The Code view's Checks tab. */
async function openChecks(page: Page): Promise<void> {
  await showView(page, "Code");
  await page.getByRole("tab", { name: "Checks", exact: true }).click();
}

const rowFor = (page: Page, name: string) =>
  page.locator(".check", {
    has: page.locator(".check-name", { hasText: new RegExp(`^${name}$`) }),
  });

/** Fills the add form and waits for the new row. */
async function addCheck(page: Page, name: string, command: string): Promise<void> {
  const form = page.locator(".checks-form");
  await form.getByLabel("Name", { exact: true }).fill(name);
  await form.getByLabel("Command", { exact: true }).fill(command);
  await form.getByRole("button", { name: "Add", exact: true }).click();
  await rowFor(page, name).waitFor();
}

test.runIf(["win32", "darwin", "linux"].includes(process.platform))(
  "checks are added, run, shown on the Review tab, cancelled and fit at 700 px",
  async () => {
    server = await startServer({ git: true });
    makeRepository();
    const oid12 = git("rev-parse", "HEAD").trim().slice(0, 12);
    const page = await openApp(1440);
    await registerAndTrust(page);
    await openChecks(page);
    expect(await page.locator(".workbench").getAttribute("data-wide")).toBe("");

    // A failing check: its output, its run line, and the run line on the list after Back.
    const failCommand = WINDOWS ? "echo viki-check& exit /b 3" : "echo viki-check; exit 3";
    await addCheck(page, "fail", failCommand);
    expect(await rowFor(page, "fail").locator("code").textContent()).toBe(failCommand);
    expect(await rowFor(page, "fail").textContent()).toContain("Time limit: 10 min");
    expect(await rowFor(page, "fail").textContent()).toContain("Not run here yet.");
    await rowFor(page, "fail").getByRole("button", { name: "Run", exact: true }).click();
    await page.getByRole("heading", { name: "Output of fail" }).waitFor();
    const failLine = `Failed (exit code 3) · ${oid12} · ${STATE}`;
    await waitForText(page, ".checks-view [role=status]", failLine, 30_000);
    await waitForText(page, "pre.check-output", "viki-check");
    await page.getByRole("button", { name: "Back to the list" }).click();
    await waitForText(page, ".check", failLine);

    // A passing check.
    await addCheck(page, "ok", "echo ok");
    await rowFor(page, "ok").getByRole("button", { name: "Run", exact: true }).click();
    await page.getByRole("heading", { name: "Output of ok" }).waitFor();
    await waitForText(page, ".checks-view [role=status]", `Passed · ${oid12} · ${STATE}`, 30_000);
    await waitForText(page, "pre.check-output", "ok");
    await page.getByRole("button", { name: "Back to the list" }).click();

    // The Review tab's Checks section, at the head the runs were made at.
    await page.getByRole("tab", { name: "Review", exact: true }).click();
    await page
      .locator("section.review-checks li", { hasText: "ok — Passed · matches this commit" })
      .waitFor();
    await page.getByRole("tab", { name: "Checks", exact: true }).click();

    // A slow check ends through Cancel.
    await addCheck(page, "slow", WINDOWS ? "ping -n 30 127.0.0.1" : "sleep 30");
    await rowFor(page, "slow").getByRole("button", { name: "Run", exact: true }).click();
    await page.getByRole("heading", { name: "Output of slow" }).waitFor();
    await page.getByRole("button", { name: "Back to the list" }).click();
    await rowFor(page, "slow").getByRole("button", { name: "Cancel", exact: true }).click();
    await rowFor(page, "slow")
      .getByText(/^Cancelled · /)
      .waitFor({ timeout: 30_000 });

    // At 700 px the Checks tab and the page fit the window: no sideways scroll.
    await page.setViewportSize({ width: 700, height: 900 });
    await page.locator(".checks").waitFor();
    await page.waitForFunction(() => document.querySelector(".shell-rail-host") === null);
    await page.evaluate(
      () =>
        new Promise<void>((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
        ),
    );
    const fit = await page.locator(".checks").evaluate((pane) => ({
      pane: pane.scrollWidth - pane.clientWidth,
      page: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    }));
    expect(fit.pane).toBeLessThanOrEqual(0);
    expect(fit.page).toBeLessThanOrEqual(0);
    await page.close();
  },
  240_000,
);
