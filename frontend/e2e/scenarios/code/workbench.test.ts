/**
 * The Code workbench end-to-end, against a scratch Git repository: register it from
 * Settings → Workspaces, trust it, browse its tree (no `.env`), open a file, see it under Changes
 * with its diff, and quick-open a file by its Japanese name, at 1440 px (the docked sidebar) and at
 * 700 px (the compact layout, with the file tree and open file visible together). No session is
 * started.
 *
 * The server starts with `git: true`: without it the app reports git missing and every code Git
 * route answers 503, so Changes and quick open could not be reached.
 *
 * The launch wipes the scratch root, so the repository is made after the server is up. It runs
 * under an empty global Git config and the repository's own identity, so this machine's Git
 * settings cannot change what the server and the test agree about.
 *
 * Every label asserted here is the `en` catalog row: the harness's Edge asks for English.
 *
 * Run from frontend/ (binds 8765; check it is free first, and build first):
 *   npm run e2e -- e2e/scenarios/code/workbench.test.ts
 */
// The harness owns `e2e/node.d.ts`, which declares only what `server.ts` needs; these modules are
// this file's own business, so they are suppressed here rather than added to it
// (`e2e/scenarios/vaults/browser.test.ts` does the same).
// @ts-expect-error see above
import { execFileSync } from "node:child_process";
// @ts-expect-error see above
import { appendFileSync, mkdirSync, writeFileSync } from "node:fs";
// @ts-expect-error see above
import { homedir, tmpdir } from "node:os";
// @ts-expect-error see above
import { join } from "node:path";
import type { Browser, Page } from "playwright-core";
import { afterAll, afterEach, beforeAll, expect, test } from "vitest";
import { appUrl, launchEdge, TEST_TOKEN } from "../../harness";
import { ORIGIN, type Server, startServer } from "../../server";
import { navigateSidebar } from "../../sidebar-navigation";

/** `scripts/e2e_server.py`'s own SCRATCH root, mirrored as `e2e/scenarios/vaults/browser.test.ts` does. */
const SCRATCH =
  process.platform === "win32" ? join(tmpdir(), "viki-e2e") : join(homedir(), ".cache", "viki-e2e");
const CODE = join(SCRATCH, "code");
const ADDED = "added by the e2e run";

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

/** A committed repository of synthetic files, then one uncommitted line in `a.txt`. */
function makeRepository(): void {
  mkdirSync(CODE, { recursive: true });
  const emptyConfig = join(SCRATCH, "empty.gitconfig");
  writeFileSync(emptyConfig, "", "utf-8");
  const env = { ...process.env, GIT_CONFIG_GLOBAL: emptyConfig, GIT_CONFIG_NOSYSTEM: "1" };
  const git = (...args: string[]) => execFileSync("git", ["-C", CODE, ...args], { env });
  git("init", "-q", "-b", "main");
  git("config", "user.email", "e2e@example.com");
  git("config", "user.name", "e2e");
  git("config", "core.autocrlf", "false");
  writeFileSync(join(CODE, "a.txt"), "first line\n", "utf-8");
  writeFileSync(join(CODE, "crlf.txt"), "one\r\ntwo\r\n", "utf-8");
  writeFileSync(join(CODE, "bom.txt"), "﻿with a byte order mark\n", "utf-8");
  writeFileSync(join(CODE, "日本語.txt"), "日本語の一行\n", "utf-8");
  writeFileSync(join(CODE, ".env"), "SYNTHETIC=1\n", "utf-8");
  git("add", "-A");
  git("commit", "-q", "-m", "scratch");
  appendFileSync(join(CODE, "a.txt"), `${ADDED}\n`, "utf-8");
}

/** Use the shared sidebar, opening its drawer when it is not docked. */
async function showView(page: Page, view: "Chat" | "Code"): Promise<void> {
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

async function walkTheWorkbench(width: number, maxRailPreferences = false): Promise<void> {
  server = await startServer({ git: true });
  makeRepository();
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({
    viewport: { width: maxRailPreferences ? 1800 : width, height: 900 },
  });
  await page.goto(appUrl());
  await page.waitForSelector('[data-region="shell"]');
  if (maxRailPreferences) {
    const left = page.getByRole("separator", { name: "Resize left rail" });
    const right = page.getByRole("separator", { name: "Resize right rail" });
    await left.press("End");
    await right.press("End");
    expect(await left.getAttribute("aria-valuenow")).toBe("480");
    expect(await right.getAttribute("aria-valuenow")).toBe("560");
  }
  await registerAndTrust(page);
  if (maxRailPreferences) await page.setViewportSize({ width, height: 900 });

  await showView(page, "Code");
  const workbench = page.locator(".workbench");
  await workbench.waitFor();
  const tree = workbench.locator(".workbench-tree");
  await tree.getByRole("button", { name: "a.txt", exact: true }).waitFor();
  expect(await tree.getByRole("button", { name: ".env", exact: true }).count()).toBe(0);
  if (width >= 1280) {
    const left = await page.locator('[data-region="left-rail"]').boundingBox();
    const center = await page.locator('[data-region="view"]').boundingBox();
    const right = await page.locator('[data-region="right-rail"]').boundingBox();
    if (!left || !center || !right) throw new Error("Code view must sit between both rails");
    expect(left.x + left.width).toBeLessThanOrEqual(center.x + 1);
    expect(center.x + center.width).toBeLessThanOrEqual(right.x + 1);
    expect(right.x + right.width).toBeLessThanOrEqual(width);
  } else {
    const rightToggle = page.getByRole("button", { name: "VIKI panel", exact: true });
    await rightToggle.click();
    await page.getByRole("dialog", { name: "VIKI panel", exact: true }).waitFor();
    expect(await page.locator('[data-region="right-rail"]').count()).toBe(1);
    await page.keyboard.press("Escape");
    expect(await rightToggle.evaluate((button) => button === document.activeElement)).toBe(true);
    await tree.waitFor({ state: "visible" });
  }

  await tree.getByRole("button", { name: "a.txt", exact: true }).click();
  await page.locator(".code-lines .code-gutter").first().waitFor();
  await tree.waitFor({ state: "visible" });
  if (maxRailPreferences) {
    const center = await page.locator('[data-region="view"]').boundingBox();
    const treePane = await page.locator(".workbench-nav").boundingBox();
    const filePane = await page.locator(".workbench-pane").boundingBox();
    if (!center || !treePane || !filePane) throw new Error("Code panes must be visible");
    expect(center.width).toBeGreaterThanOrEqual(536);
    expect(treePane.width).toBeGreaterThanOrEqual(240);
    expect(filePane.width).toBeGreaterThanOrEqual(240);
    expect(treePane.x + treePane.width).toBeLessThanOrEqual(filePane.x);
  }
  expect(await page.locator(".code-lines .code-gutter").first().textContent()).toBe("1");
  expect(await page.locator(".code-lines .code-gutter").allTextContents()).toEqual(["1", "2"]);

  await tree.getByRole("button", { name: "crlf.txt", exact: true }).click();
  const fileTabs = page.locator(".workbench-file-tabs");
  await fileTabs.getByRole("button", { name: "crlf.txt", exact: true }).waitFor();
  await page.locator(".code-file-head", { hasText: "crlf.txt" }).waitFor();
  await tree.waitFor({ state: "visible" });
  expect(await fileTabs.getByRole("button", { name: "a.txt", exact: true }).count()).toBe(1);
  if (width >= 1280) {
    expect(await page.locator('[data-region="left-rail"]').count()).toBe(1);
    expect(await page.locator('[data-region="right-rail"]').count()).toBe(1);
  } else {
    expect(await page.getByRole("button", { name: "VIKI panel", exact: true }).count()).toBe(1);
  }
  await fileTabs.getByRole("button", { name: "a.txt", exact: true }).click();
  expect(
    await fileTabs.getByRole("button", { name: "a.txt", exact: true }).getAttribute("aria-pressed"),
  ).toBe("true");
  await page.locator(".code-file-head", { hasText: "a.txt" }).waitFor();
  await fileTabs.getByRole("button", { name: "Close crlf.txt", exact: true }).click();
  expect(
    await fileTabs.getByRole("button", { name: "a.txt", exact: true }).getAttribute("aria-pressed"),
  ).toBe("true");
  expect(await page.locator(".code-lines .code-gutter").allTextContents()).toEqual(["1", "2"]);

  await workbench.getByRole("tab", { name: "Changes" }).click();
  await workbench
    .locator(".workbench-changes")
    .getByRole("button", { name: /a\.txt/ })
    .click();
  const added = page.locator(".code-diff .diff-add", { hasText: ADDED });
  await added.waitFor();
  expect(await added.allTextContents()).toEqual([`+${ADDED}`]);

  await workbench.getByRole("tab", { name: "Search" }).click();
  await workbench.getByLabel("Find a file").fill("日本語");
  await workbench
    .locator(".workbench-quickopen")
    .getByRole("button", { name: "日本語.txt" })
    .click();
  await page.locator(".code-file-head", { hasText: "日本語.txt" }).waitFor();
  await page.close();
}

test("at 1440 px a trusted scratch repository is browsed, diffed and quick-opened", async () => {
  await walkTheWorkbench(1440);
});

test("at 700 px the compact Code view keeps the tree available with open tabs", async () => {
  await walkTheWorkbench(700);
});

test("at 1280 px maximum rail preferences leave both Code panes usable", async () => {
  await walkTheWorkbench(1280, true);
});
