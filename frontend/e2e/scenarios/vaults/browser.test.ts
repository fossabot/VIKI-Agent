/**
 * The Vault view end-to-end, against the e2e server's scratch vault: a Japanese query finding a
 * note written in half-width kana, the folder select, a star that survives leaving the view, the
 * history section, and the one-column / two-column layout the 800 px breakpoint draws (which no
 * unit test can prove: jsdom has no layout).
 *
 * The scratch vault starts with no notes, so each test writes its own into it after the server is
 * up (the launch wipes the scratch root, so earlier would be lost). The vault is not a git
 * repository, so the history section answers `unknown`.
 *
 * Run from frontend/ (binds 8765; check it is free first):
 *   npm run e2e -- e2e/scenarios/vaults/browser.test.ts
 */
// The harness owns `e2e/node.d.ts`, which declares only what `server.ts` needs; these three
// modules are this file's own business, so they are suppressed here rather than added to it
// (`e2e/scenarios/shell/onboarding.test.ts` does the same).
// @ts-expect-error see above
import { mkdirSync, writeFileSync } from "node:fs";
// @ts-expect-error see above
import { homedir, tmpdir } from "node:os";
// @ts-expect-error see above
import { join } from "node:path";
import type { Browser, Page } from "playwright-core";
import { afterAll, afterEach, beforeAll, expect, test } from "vitest";
import { appUrl, launchEdge } from "../../harness";
import { type Server, startServer } from "../../server";
import { navigateSidebar } from "../../sidebar-navigation";

/** `scripts/e2e_server.py`'s own SCRATCH root, mirrored as `e2e/scenarios/shell/onboarding.test.ts` does. */
const SCRATCH =
  process.platform === "win32" ? join(tmpdir(), "viki-e2e") : join(homedir(), ".cache", "viki-e2e");

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

function writeNote(ref: string, text: string): void {
  const path = join(SCRATCH, "vault", ...ref.split("/"));
  mkdirSync(join(path, ".."), { recursive: true });
  writeFileSync(path, text, "utf-8");
}

/** Use the shared sidebar, opening its drawer when it is not docked. */
async function showView(page: Page, view: "Chat" | "Vault"): Promise<void> {
  await navigateSidebar(page, view);
}

/** Starts the server, seeds three notes in two folders (the vault also lists its CLAUDE.md, so four rows) (one in half-width kana) and opens the
 * Vault view. */
async function openVault(width = 1440): Promise<Page> {
  server = await startServer({});
  writeNote("alpha/plain.md", "# Plain\n\nnothing special here\n");
  writeNote("alpha/second.md", "# Second\n\nanother english note\n");
  writeNote("beta/kana.md", "# Kana\n\nﾃｽﾄﾉｰﾄ\n");
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({ viewport: { width, height: 900 } });
  await page.goto(appUrl());
  await page.waitForSelector('[data-region="shell"]');
  await showView(page, "Vault");
  await page.getByRole("navigation", { name: "Notes" }).waitFor();
  return page;
}

const rowButtons = (page: Page) => page.locator(".vault-list .vault-row-label > button");

test("a Japanese query finds the note written in half-width kana", async () => {
  const page = await openVault();
  await page.getByLabel("Search note contents").fill("テスト");
  await rowButtons(page).first().waitFor();
  const rows = await rowButtons(page).allTextContents();
  expect(rows).toHaveLength(1);
  expect(rows[0]).toContain("ﾃｽﾄﾉｰﾄ");
  await page.close();
});

test("the folder select narrows the list", async () => {
  const page = await openVault();
  expect(await rowButtons(page).count()).toBe(4);
  await page.getByLabel("Folder").selectOption("beta");
  await page.waitForFunction(
    () => document.querySelectorAll(".vault-list .vault-row").length === 1,
  );
  expect(await rowButtons(page).allTextContents()).toEqual(["kana"]);
  await page.close();
});

test("a star survives leaving the view and coming back", async () => {
  const page = await openVault();
  const star = () =>
    page.getByRole("button", { name: "Remove bookmark for alpha/plain.md", exact: true });
  await page.getByRole("button", { name: "Add bookmark for alpha/plain.md", exact: true }).click();
  await page.waitForFunction(
    () =>
      document
        .querySelector('[aria-label="Remove bookmark for alpha/plain.md"]')
        ?.getAttribute("aria-pressed") === "true",
  );
  await showView(page, "Chat");
  await page.waitForSelector('[data-region="conversation"]');
  await showView(page, "Vault");
  await page.getByRole("navigation", { name: "Notes" }).waitFor();
  expect(await star().getAttribute("aria-pressed")).toBe("true");
  expect(await star().textContent()).toBe("★");
  await page.close();
});

test("the selected note shows its history section", async () => {
  const page = await openVault();
  await rowButtons(page).first().click();
  const history = page.locator(".vault-inspector .vault-history");
  await history.waitFor();
  // History is closed by default: open it before reading its content.
  await history.locator("summary").click();
  // The scratch vault is not a git repository: the route answers `unknown`.
  await history.getByText("History isn't available for this note.").waitFor();
  expect(await history.locator("summary").textContent()).toBe("History");
  await page.close();
});

/** The distinct `left` offsets of the list and the inspector: equal means one column. */
async function paneLefts(page: Page): Promise<{ list: number; inspector: number }> {
  const left = async (selector: string) => {
    const box = await page.locator(selector).first().boundingBox();
    if (!box) throw new Error(`${selector} is not rendered`);
    return box.x;
  };
  return { list: await left(".vault-list"), inspector: await left(".vault-inspector") };
}

test("at 1440x900 the list and the inspector are two columns", async () => {
  const page = await openVault(1440);
  await rowButtons(page).first().click();
  await page.locator(".vault-inspector").waitFor();
  const { list, inspector } = await paneLefts(page);
  expect(inspector).toBeGreaterThan(list);
  const back = page.getByRole("button", { name: "Back", exact: true });
  expect(await back.isVisible()).toBe(true);
  expect(await back.isEnabled()).toBe(true);
  await page.close();
});

test("at 400x900 the list and the inspector are one column", async () => {
  const page = await openVault(400);
  await rowButtons(page).first().click();
  await page.locator(".vault-inspector").waitFor();
  // The list is hidden while a note is open; the back control returns to it.
  expect(await page.locator(".vault-list").isVisible()).toBe(false);
  const back = page.getByRole("button", { name: "Back", exact: true });
  expect(await back.isVisible()).toBe(true);
  expect(await back.isEnabled()).toBe(true);
  await back.click();
  await page.locator(".vault-list").waitFor();
  expect(await page.locator(".vault-inspector").count()).toBe(0);
  await page.close();
});
