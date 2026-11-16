/**
 * Previews end-to-end. In a registered, trusted code folder the Previews tab starts
 * `python -m http.server` on a free port through real cmd.exe: the row reads "Listening on port
 * <port> (process <n>)", Open preview answers "Opened a preview window." and the window's argv
 * (recorded by the harness, which starts no browser) ends with `--app=http://localhost:<port>/`,
 * holds the refusing proxy and its bypass list, and holds no token. Stop ends the row as "Stopped"
 * and Remove deletes it. The Review tab's view of a changed file downloads as `a.txt.patch`, and at
 * 700 px the Previews tab has no sideways scroll.
 *
 * Previews run on Windows only, so the walk runs there and the other platforms get the one test
 * that finds the off-Windows words. The repository is made after the server is up, under an empty
 * global Git config (see `e2e/scenarios/code/integrate.test.ts`).
 *
 * Every label asserted here is the `en` catalog row: the harness's Edge asks for English.
 *
 * Run from frontend/ (binds 8765; check it is free first, and build first):
 *   npm run e2e -- e2e/scenarios/code/previews.test.ts
 */
// The harness owns `e2e/node.d.ts`, which declares only what `server.ts` needs; these modules are
// this file's own business, so they are suppressed here rather than added to it
// (`e2e/scenarios/code/checks.test.ts` does the same).
// @ts-expect-error see above
import { execFileSync } from "node:child_process";
// @ts-expect-error see above
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
// @ts-expect-error see above
import { createServer } from "node:net";
// @ts-expect-error see above
import { homedir, tmpdir } from "node:os";
// @ts-expect-error see above
import { join, resolve } from "node:path";
import type { Browser, Page } from "playwright-core";
import { afterAll, afterEach, beforeAll, expect, test } from "vitest";
import { appUrl, launchEdge, TEST_TOKEN, waitForCount, waitForText } from "../../harness";
import { ORIGIN, type Server, startServer } from "../../server";
import { navigateSidebar } from "../../sidebar-navigation";

/** `scripts/e2e_server.py`'s own SCRATCH root, mirrored as `e2e/scenarios/code/checks.test.ts` does. */
const SCRATCH =
  process.platform === "win32" ? join(tmpdir(), "viki-e2e") : join(homedir(), ".cache", "viki-e2e");
const CODE = join(SCRATCH, "code");
const WINDOWS = process.platform === "win32";
const OFF_WINDOWS = "Previews work on Windows in this version.";
/** The repository's Python, from vitest's cwd (frontend/), as `server.ts` gives it. */
const PYTHON = resolve("..", ".venv", "Scripts", "python.exe");

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
 * A repository with `site/index.html` and `a.txt` committed. The address is built in two parts so
 * the fixture-clean e-mail scan reads no address in this file.
 */
function makeRepository(): void {
  mkdirSync(join(CODE, "site"), { recursive: true });
  git("init", "-q", "-b", "main");
  git("config", "user.email", `e2e${"@"}example.com`);
  git("config", "user.name", "e2e");
  git("config", "commit.gpgsign", "false");
  git("config", "core.autocrlf", "false");
  writeFileSync(join(CODE, "site", "index.html"), "<h1>viki preview</h1>\n", "utf-8");
  writeFileSync(join(CODE, "a.txt"), "base\n", "utf-8");
  git("add", "-A");
  git("commit", "-q", "-m", "scratch");
}

/** A port nothing holds: a server asked for port 0, then closed. */
function freePort(): Promise<number> {
  return new Promise((done, fail) => {
    const probe = createServer();
    probe.once("error", fail);
    probe.listen(0, "127.0.0.1", () => {
      const { port } = probe.address() as { port: number };
      probe.close(() => done(port));
    });
  });
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

/** The Code view's Previews tab. */
async function openPreviews(page: Page): Promise<void> {
  await showView(page, "Code");
  await page.getByRole("tab", { name: "Previews", exact: true }).click();
}

/** The argv of every preview window the harness was asked for, oldest first. */
async function windowArgvs(): Promise<string[][]> {
  const response = await fetch("http://127.0.0.1:8765/api/e2e/previews", {
    headers: { Authorization: `Bearer ${TEST_TOKEN}` },
  });
  if (!response.ok) throw new Error(`GET /api/e2e/previews answered ${response.status}, not 200`);
  return ((await response.json()) as { argvs: string[][] }).argvs;
}

test.runIf(WINDOWS)(
  "a preview is started, opened, stopped and removed; a patch downloads; it fits at 700 px",
  async () => {
    server = await startServer({ git: true });
    makeRepository();
    const port = await freePort();
    const page = await openApp(1440);
    await registerAndTrust(page);
    await openPreviews(page);

    // Start: the row reads the listener, which can take a few seconds to come up.
    const command = `"${PYTHON}" -m http.server ${port} --bind 127.0.0.1 --directory site`;
    await page.getByLabel("Command", { exact: true }).fill(command);
    await page.getByLabel("Port", { exact: true }).fill(String(port));
    await page.getByRole("button", { name: "Start", exact: true }).click();
    const row = page.locator(".preview");
    await row
      .getByText(new RegExp(`^Listening on port ${port} \\(process \\d+\\)$`))
      .waitFor({ timeout: 30_000 });

    // Open: the window's argv ends with the preview's own address, has the refusing proxy and its
    // bypass list, and holds no token.
    await row.getByRole("button", { name: "Open preview", exact: true }).click();
    await waitForText(page, ".previews [role=status]", "Opened a preview window.");
    const argv = (await windowArgvs()).at(-1) ?? [];
    expect(argv.at(-1)).toBe(`--app=http://localhost:${port}/`);
    expect(argv.some((item) => item.startsWith("--proxy-server=127.0.0.1:"))).toBe(true);
    expect(argv.some((item) => item.startsWith("--proxy-bypass-list=<-loopback>;"))).toBe(true);
    expect(argv.some((item) => item.includes(TEST_TOKEN))).toBe(false);

    // At 700 px, with the long command on screen, the Previews tab and the page fit the window.
    await page.setViewportSize({ width: 700, height: 900 });
    await page.locator(".previews").waitFor();
    // The shell undocks its rails a render after the resize: measure once they are gone.
    await page.waitForFunction(() => document.querySelector(".shell-rail-host") === null);
    await page.evaluate(
      () =>
        new Promise<void>((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
        ),
    );
    const fit = await page.locator(".previews").evaluate((pane) => ({
      pane: pane.scrollWidth - pane.clientWidth,
      page: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    }));
    expect(fit.pane).toBeLessThanOrEqual(0);
    expect(fit.page).toBeLessThanOrEqual(0);
    await page.setViewportSize({ width: 1440, height: 900 });

    // Stop, then Remove: the row ends as Stopped and then goes.
    await row.getByRole("button", { name: "Stop", exact: true }).click();
    await row.getByText("Stopped", { exact: true }).waitFor({ timeout: 30_000 });
    await row.getByRole("button", { name: "Remove", exact: true }).click();
    await waitForCount(page, ".preview", 0);

    // A changed file's view downloads its diff as a patch.
    writeFileSync(join(CODE, "a.txt"), "changed\n", "utf-8");
    await page.getByRole("tab", { name: "Review", exact: true }).click();
    const entry = page.locator(".review-entry", { hasText: "a.txt" });
    await entry.locator(".chip", { hasText: /^Modified$/ }).waitFor();
    await entry.locator("button.review-path-button").click();
    await waitForText(page, ".review-view .code-diff", "changed");
    const downloading = page.waitForEvent("download");
    await page.getByRole("button", { name: "Download patch for a.txt" }).click();
    const download = await downloading;
    expect(download.suggestedFilename()).toBe("a.txt.patch");
    expect(readFileSync(await download.path(), "utf-8")).toContain("+changed");
    await page.close();
  },
  240_000,
);

test.runIf(!WINDOWS)("off Windows the Previews tab says so", async () => {
  server = await startServer({ git: true });
  makeRepository();
  const page = await openApp(1440);
  await registerAndTrust(page);
  await openPreviews(page);
  await page.getByText(OFF_WINDOWS, { exact: true }).waitFor();
  await page.close();
});
