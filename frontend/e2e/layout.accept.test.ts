/**
 * Acceptance (frozen): the shell reproduces the design's box metrics in real Edge. Regions are
 * found by data-region.
 */
import type { Browser, Page } from "playwright-core";
import { createServer, type ViteDevServer } from "vite";
import { afterAll, beforeAll, describe, expect, test } from "vitest";
import { launchEdge } from "./harness";

const PORT = 5199;
const ORIGIN = `http://127.0.0.1:${PORT}`;
let server: ViteDevServer | undefined;
let browser: Browser | undefined;

beforeAll(async () => {
  server = await createServer({
    server: { host: "127.0.0.1", port: PORT, strictPort: true },
    logLevel: "silent",
  });
  await server.listen();
  browser = await launchEdge();
});

afterAll(async () => {
  await browser?.close();
  await server?.close();
});

async function open(width: number, height = 900): Promise<{ page: Page; requests: string[] }> {
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({ viewport: { width, height } });
  const requests: string[] = [];
  page.on("request", (request) => requests.push(request.url()));
  await page.goto(`${ORIGIN}/`);
  await page.locator('[data-region="shell"]').waitFor({ timeout: 10_000 });
  await page.evaluate(() => document.fonts.ready);
  return { page, requests };
}

async function box(page: Page, region: string) {
  const found = await page.locator(`[data-region="${region}"]`).boundingBox();
  if (!found) throw new Error(`region ${region} is not rendered`);
  return found;
}

async function style(page: Page, region: string, property: string): Promise<string> {
  return page
    .locator(`[data-region="${region}"]`)
    .evaluate((el, prop) => getComputedStyle(el).getPropertyValue(prop), property);
}

describe("the mock's 1440x900 layout", () => {
  test("regions have the mock's box metrics", async () => {
    const { page } = await open(1440);
    expect(await box(page, "shell")).toEqual({ x: 0, y: 0, width: 1440, height: 900 });
    // The mock sized its bars content-box, so each bar is its declared height plus the
    // hairline: 48+1, 52+1, 26+1.
    expect((await box(page, "topbar")).height).toBe(49);
    expect((await box(page, "statusbar")).height).toBe(27);

    const left = await box(page, "left-rail");
    expect(left).toMatchObject({ x: 0, y: 49, width: 272, height: 824 });
    const right = await box(page, "right-rail");
    expect(right).toMatchObject({ x: 1120, y: 49, width: 320, height: 824 });
    const conversation = await box(page, "conversation");
    expect(conversation).toMatchObject({ x: 272, width: 848 });

    expect((await box(page, "conversation-header")).height).toBe(53);
    const column = await box(page, "message-column");
    expect(column.width).toBe(720);
    expect(column.x).toBe(272 + (848 - 720) / 2);
    const composer = await box(page, "composer");
    expect(composer.width).toBe(760);
    expect(composer.height).toBe(56);
    expect(composer.x).toBe(272 + (848 - 760) / 2);
  });

  test("colours and type use the approved monochrome brand tokens", async () => {
    const { page } = await open(1440);
    expect(await style(page, "shell", "background-color")).toBe("rgb(11, 11, 11)");
    expect(await style(page, "conversation", "background-color")).toBe("rgb(13, 13, 13)");
    expect(await style(page, "new-session", "background-color")).toBe("rgb(248, 248, 255)");
    expect(await page.locator('[data-region="wordmark"] svg.tile-wordmark').count()).toBe(1);
    expect(await style(page, "shell", "font-family")).toContain("IBM Plex Sans");
    // Each family must have a face that loads (from the app origin; see the next test).
    const facesLoaded = await page.evaluate(async () => {
      const specs: [string, string][] = [
        ['400 14px "IBM Plex Sans"', "Chat"],
        ['400 14px "IBM Plex Sans JP"', "日本語"],
        ['400 12px "IBM Plex Mono"', "main"],
      ];
      const counts: number[] = [];
      for (const [font, text] of specs) counts.push((await document.fonts.load(font, text)).length);
      return counts.map((n) => n > 0);
    });
    expect(facesLoaded).toEqual([true, true, true]);
  });

  test("nothing is fetched from outside the app origin", async () => {
    const { requests } = await open(1440);
    const foreign = requests.filter((url) => !url.startsWith(ORIGIN) && !url.startsWith("data:"));
    expect(foreign).toEqual([]);
  });
});

describe("narrower windows", () => {
  test("the right rail hides below 1280px; the column keeps 720px", async () => {
    const { page } = await open(1279);
    expect(await page.locator('[data-region="right-rail"]').isVisible()).toBe(false);
    expect((await box(page, "conversation")).width).toBe(1279 - 272);
    expect((await box(page, "message-column")).width).toBe(720);
    expect((await box(page, "composer")).width).toBe(760);
  });

  test("at 900px the column and composer shrink, keeping 24px and 16px side gutters", async () => {
    const { page } = await open(900);
    const conversation = await box(page, "conversation");
    expect(conversation.width).toBe(900 - 272);
    expect((await box(page, "message-column")).width).toBe(conversation.width - 2 * 24);
    expect((await box(page, "composer")).width).toBe(conversation.width - 2 * 16);
  });
});
