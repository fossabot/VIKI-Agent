/**
 * The composer at 400 CSS pixels and at 200% zoom, against the real server and the fake CLI.
 * Actual App, Edge and Python APIs; synthetic scratch only, no native provider turns.
 *
 * e2e/scenarios/shell/narrow-layout.test.ts measures the same window against mocked REST with the socket down, which
 * can show the pill at rest but never while a turn runs: the Stop pill, the disabled submit and
 * the model chip's real label only exist once a session has answered. The label is the reason
 * this file is worth its server — its width is the CLI's to choose, not ours, and the fixture's
 * initialize reply names "Default (recommended)", which with the effort and the caret is about
 * 31 characters where the chip is allowed 110 pixels.
 *
 * interrupt-mid-stream.jsonl holds the turn open until Stop is pressed (its own script waits for
 * the interrupt control frame before sending a result), unlike text-two-turns.jsonl, which the
 * real server finishes so fast that the running-state assertions below never catch the pill up.
 *
 * Binds 8765, so it is part of the serialized end-to-end run, never of a parallel one:
 *   npx vitest run --config vitest.e2e.config.ts e2e/scenarios/shell/narrow-app.test.ts
 */
import type { Browser, Page } from "playwright-core";
import { afterAll, beforeAll, expect, test } from "vitest";
import type { SettingsView } from "../../../src/clients/app/shellApi";
import { appUrl, launchEdge, send, waitForCount } from "../../harness";
import { ORIGIN, PORT, type Server, startServer } from "../../server";
import { newSidebarSession } from "../../sidebar-navigation";

let server: Server | undefined;
let browser: Browser | undefined;

beforeAll(async () => {
  server = await startServer({ fixture: "tests/fixtures/claude/interrupt-mid-stream.jsonl" });
  browser = await launchEdge();
});

afterAll(async () => {
  await browser?.close();
  await server?.stop();
});

interface Size {
  width: number;
  height: number;
  deviceScaleFactor?: number;
  fontScale?: number;
}

async function open(size: Size): Promise<Page> {
  if (!browser) throw new Error("Edge did not launch");
  const { width, height, deviceScaleFactor, fontScale = 1 } = size;
  const page = await browser.newPage({
    viewport: { width, height },
    ...(deviceScaleFactor === undefined ? {} : { deviceScaleFactor }),
  });
  // The App applies `accessibility.font_scale` from its own settings read, a frame or more after
  // the response, so a scale written onto the root by hand gets written back. The
  // read answers with the wanted scale instead, and the wait below holds only once it is applied.
  if (fontScale !== 1) {
    await page.route(`${ORIGIN}/api/v2/settings`, async (route) => {
      if (route.request().method() !== "GET") {
        await route.continue();
        return;
      }
      const response = await route.fetch();
      const settings = (await response.json()) as SettingsView;
      await route.fulfill({
        response,
        json: {
          ...settings,
          effective: { ...settings.effective, "accessibility.font_scale": fontScale },
        } satisfies SettingsView,
      });
    });
  }
  const settings = page.waitForResponse(
    (response) => new URL(response.url()).pathname === "/api/v2/settings",
  );
  await page.goto(appUrl(PORT));
  await page.locator('[data-region="shell"]').waitFor({ timeout: 15_000 });
  await settings;
  await page.waitForFunction(
    (wanted) => document.documentElement.style.getPropertyValue("--font-scale") === wanted,
    String(fontScale),
  );
  expect(await page.evaluate(() => getComputedStyle(document.documentElement).zoom)).toBe(
    String(fontScale),
  );
  await page.evaluate(async () => {
    await document.fonts.ready;
    await new Promise<void>((resolve) =>
      requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
    );
  });
  // These are first-turn cases; a previous viewport may have saved a chat in layout.panes.
  // The explicit action owns the selection and prevents a late settings adoption retargeting it.
  await newSidebarSession(page);
  await page.waitForFunction(
    () => document.querySelector(".draft-status")?.textContent === "Draft saved",
  );
  return page;
}

async function box(page: Page, selector: string) {
  const found = await page.locator(selector).first().boundingBox();
  if (!found) throw new Error(`${selector} is not rendered`);
  return found;
}

/** The pill holds everything it draws, and `child` is inside it. */
async function expectInsideComposer(page: Page, child: string): Promise<void> {
  const overflowing = await page
    .locator(".composer")
    .evaluate((el) => el.scrollWidth > el.clientWidth + 1);
  expect(overflowing).toBe(false);
  const composer = await box(page, '[data-region="composer"]');
  const inner = await box(page, child);
  expect(inner.x).toBeGreaterThanOrEqual(composer.x);
  expect(inner.x + inner.width).toBeLessThanOrEqual(composer.x + composer.width);
  expect(inner.y).toBeGreaterThanOrEqual(composer.y);
  expect(inner.y + inner.height).toBeLessThanOrEqual(composer.y + composer.height);
}

const sizes: [string, Size][] = [
  ["400x900", { width: 400, height: 900 }],
  ["400x900 at font scale 1.5", { width: 400, height: 900, fontScale: 1.5 }],
  ["720x450 at 2x (1440x900 zoomed to 200%)", { width: 720, height: 450, deviceScaleFactor: 2 }],
];

for (const [name, size] of sizes) {
  test(`the pill holds its controls at ${name}, running and at rest`, async () => {
    const page = await open(size);
    try {
      await send(page, "hello");

      // While the turn runs: Stop replaces Send, and both it and the textarea fit the pill.
      await page.locator(".composer-stop").waitFor({ timeout: 30_000 });
      await expectInsideComposer(page, ".composer-stop");
      await expectInsideComposer(page, ".composer-box");
      // The Esc hint is the one thing the pill drops at this width; the key still works.
      expect(await page.locator(".composer-stop .kbd").isVisible()).toBe(false);

      // The fixture's turn only ends once it sees our interrupt, same as e2e/scenarios/chat/stop.test.ts.
      await page.getByLabel("Stop").click();
      await waitForCount(page, ".composer-stop", 0);

      // After it: the model chip is back, truncated but complete to a screen reader, and Send
      // is inside the pill with it.
      await page.waitForFunction(
        () => document.querySelector(".composer-stop") === null,
        undefined,
        {
          timeout: 30_000,
        },
      );
      await expectInsideComposer(page, ".composer-submit");
      await expectInsideComposer(page, '.model-chip[aria-haspopup="menu"]');
      await expectInsideComposer(page, ".composer-box");
      const message = await box(page, ".composer-box");
      expect(message.width).toBeGreaterThanOrEqual(120 * (size.fontScale ?? 1));
      const chip = await box(page, '.model-chip[aria-haspopup="menu"]');
      expect(chip.width).toBeLessThanOrEqual(110 * (size.fontScale ?? 1));
      const label = (await page.locator('.model-chip[aria-haspopup="menu"]').textContent()) ?? "";
      // Truncation is the browser's ellipsis over the whole string, never a shorter string.
      expect(label.length).toBeGreaterThan(20);
      expect(label).toContain("·");

      // And the window itself still holds everything.
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
        size.width,
      );
    } finally {
      await page.close();
    }
  }, 120_000);
}
