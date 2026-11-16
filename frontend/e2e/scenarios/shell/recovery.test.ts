/**
 * The recovery launch, against the real `create_recovery_app`:
 * `startServer({ damagedStore: true })` leaves bytes no SQLite database starts with where the
 * app store goes, so the harness takes the same fork the launcher does.
 *
 * That matters because a recovery window is defined by what it does NOT serve: no /ws, no
 * /api/sessions, no /api/rails, no drafts, no export. Only a real one can prove the page stops
 * asking for them — a mocked capabilities route would leave every other route answering.
 *
 * Run from frontend/ (binds 8765; check it is free first):
 *   npx vitest run -c vitest.e2e.config.ts e2e/scenarios/shell/recovery.test.ts
 */
import type { Browser, Page } from "playwright-core";
import { afterAll, beforeAll, expect, test } from "vitest";
import { appUrl, launchEdge, waitForText } from "../../harness";
import { type Server, startServer } from "../../server";

let browser: Browser | undefined;
let server: Server | undefined;

beforeAll(async () => {
  browser = await launchEdge();
  server = await startServer({ damagedStore: true });
}, 90_000);

afterAll(async () => {
  await server?.stop();
  await browser?.close();
});

async function openApp(): Promise<{ page: Page; asked: string[] }> {
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, locale: "en-US" });
  const asked: string[] = [];
  page.on("request", (request) => {
    asked.push(new URL(request.url()).pathname);
  });
  page.on("websocket", (socket) => {
    asked.push(new URL(socket.url()).pathname);
  });
  await page.goto(appUrl());
  await page.waitForSelector('[data-region="shell"]');
  return { page, asked };
}

test("a damaged store serves the recovery view and nothing that needs the store", async () => {
  const { page, asked } = await openApp();

  await page.waitForSelector(".recovery-view");
  // A recovery window serves no settings document; its copy is the English catalog all the same.
  await waitForText(page, ".recovery-view", "recovery mode");
  // Export is stated as unavailable, not rendered disabled: the route does not exist.
  await waitForText(page, ".recovery-view", "Exports are unavailable");
  expect(await page.getByRole("button", { name: /Export/ }).count()).toBe(0);

  // No chat: no composer, no rails, no conversation.
  expect(await page.locator('[data-region="conversation"]').count()).toBe(0);
  expect(await page.locator('[data-region="composer"]').count()).toBe(0);

  // The app menu offers diagnostics and nothing else that could work here.
  await page.locator('[data-region="app-menu"]').click();
  const settings = page.getByRole("menuitem", { name: /^Settings/ });
  expect(await settings.getAttribute("aria-disabled")).toBe("true");
  await page.keyboard.press("Escape");

  // The socket connects before this window can know it is in recovery — capabilities are a
  // request of their own — so the first dial is unavoidable. What must not happen is a second
  // one, and that is what this window can prove in three seconds: the reconnect backoff is
  // [500, 1000, 2000, 5000] ms (state/socket.ts), so a /ws that was not closed for good would
  // have been dialled at least three more times by now.
  //
  // Deliberately NOT asserted here: /api/sessions, /api/rails and /api/v2/drafts. The rest store
  // polls every 60 s and the editor key does not change, so those stay at one attempt in a
  // three-second window whether or not `restStore.stop()` and the `durableSuspended` guard
  // exist — a counter that cannot fail proves nothing.
  //
  // What is pinned elsewhere, precisely: `restStore.stop()` by src/app-tests/shell/onboardingSettingsRecovery.unit.test.tsx ("recovery is
  // the only view and stops what cannot succeed"), which fakes the 60 s interval and moves the
  // clock past two ticks — four polls appear there the moment the call is dropped. The
  // `durableSuspended` guard is pinned by NOTHING, and no test can pin it: a recovery window
  // renders no rail and no composer, so the editor key never changes and the guard has no
  // reachable case. The one draft read a recovery window makes is the mount-time one, which
  // leaves before capabilities report recovery; that unit test asserts that single read as a
  // fact, rather than a delta between two moments that cannot differ.
  await page.waitForTimeout(3_000);
  expect(asked.filter((seen) => seen === "/ws").length).toBe(1);
  // Exports are never asked for at all: a recovery app mounts no such route.
  expect(asked.filter((seen) => seen.startsWith("/api/v2/exports"))).toEqual([]);

  await page.close();
});

test("New session is absent, and the recovery view's alert names the store as the reason", async () => {
  const { page } = await openApp();
  await page.waitForSelector(".recovery-view");

  const button = page.getByRole("button", { name: "New session" });
  expect(await button.count()).toBe(0);
  // `create_recovery_app`'s capabilities always answer `can_start: false` (src/viki_agent/app/diagnostics.py),
  // and RecoveryView renders that reason as its one role="alert" paragraph (dataErrorText).
  await waitForText(page, '.recovery-view [role="alert"]', "damaged");

  await page.close();
});
