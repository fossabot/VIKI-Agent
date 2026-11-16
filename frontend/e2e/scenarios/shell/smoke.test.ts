/**
 * The end-to-end harness itself, and nothing else: that playwright-core finds the machine's real
 * Microsoft Edge, opens it at the design's viewport, and measures a box the way the layout tests
 * depend on. It imports nothing from `../src`, loads no bundle and starts no server, so it stays
 * green whatever the app does. That is what a canary is for, and it is also why it must not be
 * read as coverage of the product.
 *
 * The end-to-end suite covers different layers:
 *
 *   Scratch backend and real bundle — `scripts/e2e_server.py --scratch` with fixtures driving
 *      the fake CLI, including stream, stop, cards, reload and approval scenarios.
 *   Real bundle with no backend — layout.accept and narrow-layout against a bare Vite dev
 *      server, plus e2e/scenarios/vaults/hostileContent.test.ts, whose REST routes playwright answers itself.
 *   This browser canary — no backend, no bundle, no app.
 *
 * `vitest.e2e.config.ts` takes every `.test.ts` under `e2e/`, so each test's name and header
 * identify which layer it checks. (`vitest.live.config.ts` and `vitest.shots.config.ts` are the
 * house pattern for a run deliberately outside scripts/check.py; this canary belongs in it.)
 */
import type { Browser } from "playwright-core";
import { afterAll, beforeAll, expect, test } from "vitest";
import { launchEdge } from "../../harness";

let browser: Browser | undefined;

beforeAll(async () => {
  browser = await launchEdge();
});

afterAll(async () => {
  await browser?.close();
});

test("harness canary (no app): Edge opens at the design's 1440x900 and reports box metrics", async () => {
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.setContent('<main id="m" style="width:760px;height:40px">VIKI</main>');
  const box = await page.locator("#m").boundingBox();
  expect(box?.width).toBe(760);
  expect(await page.evaluate(() => window.innerWidth)).toBe(1440);
});
