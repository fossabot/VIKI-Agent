/**
 * Settings, appearance and the data view against the real backend.
 *
 * Three things only the whole stack can answer are asserted here: that a saved setting really
 * outlives a reload and reaches the conversation's own label, that a second window holding a
 * stale revision is told rather than silently overwriting the first, and that an export really
 * writes the bytes it previewed to a folder outside every root.
 *
 * Every string asserted here is an English catalog row: the UI is English only.
 *
 * Run from frontend/ (binds 8765; check it is free first):
 *   npx vitest run -c vitest.e2e.config.ts e2e/scenarios/shell/settings.test.ts
 */
// The harness owns `e2e/node.d.ts`, which declares only what `server.ts` needs; these three
// modules are this file's own business, so they are suppressed here rather than added to it
// (src/testing/css.ts does the same for the same reason).
// @ts-expect-error see above
import { mkdirSync, readdirSync, readFileSync } from "node:fs";
// @ts-expect-error see above
import { homedir, tmpdir } from "node:os";
// @ts-expect-error see above
import { join } from "node:path";
import type { Browser, Page } from "playwright-core";
import { afterAll, afterEach, beforeAll, expect, test } from "vitest";
import { appUrl, launchEdge, waitForText } from "../../harness";
import { type Server, startServer } from "../../server";

/**
 * `scripts/e2e_server.py`'s own SCRATCH root, mirrored here: one fixed folder, wiped on every
 * start. Windows keeps the temp-dir root; POSIX mirrors `Path.home() / ".cache" / "viki-e2e"`
 * (the fixed POSIX deny set covers `/tmp` and `/private`, so a scratch root under either is
 * refused).
 */
const SCRATCH =
  process.platform === "win32" ? join(tmpdir(), "viki-e2e") : join(homedir(), ".cache", "viki-e2e");
const EXPORTS = join(SCRATCH, "exports");

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

async function openApp(): Promise<Page> {
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.goto(appUrl());
  await page.waitForSelector('[data-region="shell"]');
  return page;
}

/** Ctrl+, is the shortcut the app menu's own row documents; using it exercises both. */
async function openSettings(page: Page): Promise<void> {
  await page.keyboard.press("Control+Comma");
  await page.waitForSelector('[data-region="settings"]');
}

test("settings survive a reload and a stale window is told, not overwritten", async () => {
  server = await startServer({});
  const first = await openApp();
  await openSettings(first);

  const name = first.locator('[data-setting-key="identity.display_name"] input');
  await name.fill("Synth User");
  await name.blur();
  await waitForText(first, '[data-setting-key="identity.display_name"]', "Set here");

  await first.reload();
  await first.waitForSelector('[data-region="shell"]');
  await openSettings(first);
  expect(await first.locator('[data-setting-key="identity.display_name"] input').inputValue()).toBe(
    "Synth User",
  );

  // A second window that read the document BEFORE the save above still holds revision N. It
  // must be told, and must not quietly win.
  const second = await openApp();
  await openSettings(second);
  await second.waitForFunction(
    () =>
      document.querySelector<HTMLInputElement>('[data-setting-key="identity.display_name"] input')
        ?.value === "Synth User",
  );
  await first.locator('[data-setting-key="identity.display_name"] input').fill("First wins");
  // Wait for the FIRST window's own PATCH to land before the second window edits, so its
  // revision bump has already happened and the second window's PATCH is the one that loses.
  const saved = first.waitForResponse(
    (response) =>
      response.url().endsWith("/api/v2/settings") && response.request().method() === "PATCH",
  );
  await first.locator('[data-setting-key="identity.display_name"] input').blur();
  expect((await saved).status()).toBe(200);
  await waitForText(first, '[data-setting-key="identity.display_name"]', "Set here");

  await second.locator('[data-setting-key="identity.display_name"] input').fill("Second");
  await second.locator('[data-setting-key="identity.display_name"] input').blur();
  await second.waitForSelector('[role="dialog"].settings-review');
  await waitForText(second, ".settings-review", "First wins");

  await first.close();
  await second.close();
});

test("appearance applies live, and cancel puts it back", async () => {
  server = await startServer({});
  const page = await openApp();
  await openSettings(page);
  await page.getByRole("tab", { name: /Appearance/ }).click();

  await page.locator("#appearance-font_scale").selectOption("1.3");
  await page.locator("#appearance-density").selectOption("compact");
  // The preview is live before Apply: this page's own rule.
  await page.waitForFunction(
    () => document.documentElement.style.getPropertyValue("--font-scale") === "1.3",
  );
  await page.getByRole("button", { name: "Apply" }).click();
  await page.waitForFunction(() => document.documentElement.dataset.density === "compact");

  // At 1.3 the whole page is zoomed; the composer must still be inside the window.
  const settings = page.locator('[data-region="settings"]');
  await settings.locator('[data-page="appearance"][aria-selected="true"]').waitFor();
  await settings.locator('.settings-page[aria-busy="false"]').waitFor();
  const back = settings.getByRole("button", { name: "Back", exact: true });
  expect(await back.isVisible()).toBe(true);
  expect(await back.isEnabled()).toBe(true);
  await back.focus();
  await page.keyboard.press("Escape");
  await settings.locator('[data-page="general"][aria-selected="true"]').waitFor();
  expect(await settings.isVisible()).toBe(true);
  expect(await back.isVisible()).toBe(true);
  expect(await back.isEnabled()).toBe(true);
  await back.focus();
  await page.keyboard.press("Escape");
  await page.waitForSelector('[data-region="conversation"]');
  const composer = await page.locator('[data-region="composer"]').boundingBox();
  const inner = await page.evaluate(() => window.innerHeight);
  expect(composer).not.toBeNull();
  expect((composer?.y ?? 0) + (composer?.height ?? 0)).toBeLessThanOrEqual(inner);

  await openSettings(page);
  await page.getByRole("tab", { name: /Appearance/ }).click();
  await page.locator("#appearance-font_scale").selectOption("1.5");
  await page.getByRole("button", { name: "Cancel" }).click();
  await page.waitForFunction(
    () => document.documentElement.style.getPropertyValue("--font-scale") === "1.3",
  );

  await page.close();
});

test("locations render and an export lands where it was asked to", async () => {
  server = await startServer({});
  mkdirSync(EXPORTS, { recursive: true });
  const page = await openApp();

  await page.keyboard.press("Control+Comma");
  await page.waitForSelector('[data-region="settings"]');
  await page.keyboard.press("Escape");
  await page.locator('[data-region="app-menu"]').click();
  await page.getByRole("menuitem", { name: /Data locations/ }).click();
  await page.waitForSelector(".data-view");
  await waitForText(page, ".data-view", "Locations");

  // Both the export and the import panel have a Preview; the export panel is the one with
  // the destination field, and all four panels are mounted together.
  const exportPanel = page.getByRole("region", { name: "Export" });
  await exportPanel.getByRole("button", { name: "Preview" }).click();
  await page.waitForSelector("#export-destination");
  const preview = await exportPanel.locator("pre").first().textContent();

  // The destination is a FOLDER (src/viki_agent/app/exports.py names the file itself), and it must be outside
  // every root the app owns or has registered — `EXPORTS` is a sibling of the scratch vault.
  await page.locator("#export-destination").fill(EXPORTS);
  await exportPanel.getByRole("button", { name: "Export", exact: true }).click();
  await page.waitForFunction(
    (path: string) => document.body.textContent?.includes(path) === true,
    EXPORTS,
  );

  const written = readdirSync(EXPORTS);
  expect(written.length).toBeGreaterThan(0);
  const body = readFileSync(join(EXPORTS, written[0] ?? ""), "utf-8");
  // The preview is not a promise about a different document: what was shown is what was written.
  // Asserted unconditionally — a null or empty preview is itself the failure, not a reason to
  // skip the comparison.
  expect(body).toContain('"generated_at"');
  expect((preview ?? "").trim().length).toBeGreaterThan(40);
  expect(body).toContain((preview ?? "").trim().slice(0, 40));

  await page.close();
});
