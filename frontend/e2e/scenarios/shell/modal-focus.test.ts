/** Actual App, Edge and Python APIs; synthetic scratch only, no native provider turns. */
import type { Browser } from "playwright-core";
import { afterAll, beforeAll, expect, test } from "vitest";
import { appUrl, launchEdge } from "../../harness";
import { PORT, type Server, startServer } from "../../server";

let server: Server | undefined;
let browser: Browser | undefined;
beforeAll(async () => {
  server = await startServer({ fixture: "tests/fixtures/claude/text-two-turns.jsonl" });
  browser = await launchEdge();
});
afterAll(async () => {
  await browser?.close();
  await server?.stop();
});

test.each([1440, 400])(
  "search contains focus and restores its opener at %i CSS pixels",
  async (width) => {
    if (!browser) throw new Error("missing browser");
    const page = await browser.newPage({ viewport: { width, height: 900 } });
    try {
      await page.goto(appUrl(PORT));
      const box = page.getByLabel("Message", { exact: true });
      await box.focus();
      await page.keyboard.press("Control+k");
      const dialog = page.getByRole("dialog", { name: "Search sessions" });
      await dialog.waitFor();
      const input = page.getByPlaceholder("Search sessions");
      expect(await input.evaluate((e) => e === document.activeElement)).toBe(true);
      await page.keyboard.press("Shift+Tab");
      expect(await input.evaluate((e) => e === document.activeElement)).toBe(true);
      await page.keyboard.press("Tab");
      expect(await input.evaluate((e) => e === document.activeElement)).toBe(true);
      await box.evaluate((e) => e.focus());
      expect(await input.evaluate((e) => e === document.activeElement)).toBe(true);
      expect(
        await page
          .locator('.topbar button[aria-label="App menu"]')
          .evaluate((e) => e.closest("[inert]") !== null),
      ).toBe(true);
      await input.dispatchEvent("keydown", { key: "Escape", isComposing: true });
      expect(await dialog.count()).toBe(1);
      const bounds = await dialog.boundingBox();
      if (!bounds) throw new Error("missing modal bounds");
      expect(bounds.x).toBeGreaterThanOrEqual(0);
      expect(bounds.x + bounds.width).toBeLessThanOrEqual(width);
      const searched = page.waitForResponse((r) => r.url().includes("/api/search?"));
      await input.fill("synthetic");
      expect((await searched).status()).toBe(200);
      await page.keyboard.press("Escape");
      await dialog.waitFor({ state: "detached" });
      expect(await box.evaluate((e) => e === document.activeElement)).toBe(true);
      expect(
        await page
          .locator('.topbar button[aria-label="App menu"]')
          .evaluate((e) => e.closest("[inert]") === null),
      ).toBe(true);
    } finally {
      await page.close();
    }
  },
);
